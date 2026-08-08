#!/usr/bin/env python3
"""Download TEMPO NO2 L2 data over Colorado, convert to GeoTIFF, done.

Strategy: instead of pulling full continent-wide L2 granules (multi-TB for
the full mission), we ask NASA's Harmony service to spatially subset each
granule to a Colorado bounding box *before* it leaves NASA's servers. That
cuts per-file size by ~95%+, which is what makes "download the whole TEMPO
NO2 archive for Colorado in a few hours" realistic.

The date range is split into one Harmony job per calendar month and jobs
are processed concurrently (submit + wait + download), so we're not
waiting on one giant serial job. Progress is checkpointed to a manifest
file so the script can be killed and re-run without redoing finished
months.

Each downloaded granule (.nc) is immediately resampled onto a uniform
Colorado grid and written out as a multi-band GeoTIFF (NO2 tropospheric/
stratospheric/total columns + quality flags - see tempo_no2_to_geotiff.py).

By default the source .nc is then deleted, keeping local disk usage
bounded to one month's worth of granules at a time instead of the whole
archive. Pass --s3-bucket to instead upload both the .nc and .tif to S3
after conversion and delete the local copies once the upload succeeds -
this keeps disk usage bounded the same way while actually retaining every
file (durably, in S3) rather than throwing the .nc away.

Usage:
    python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07

    # Resume/re-run: already-completed months are skipped automatically.
    python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07

    # Tune concurrency (default 4 concurrent Harmony jobs):
    python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07 --workers 6

    # Keep the raw .nc files on local disk instead of deleting them post-conversion:
    python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07 --keep-nc

    # Upload every finished .nc/.tif to S3 instead of keeping them locally:
    python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07 \\
        --s3-bucket my-tempo-bucket --s3-prefix tempo_no2_co
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
import earthaccess
from harmony import BBox, Client, Collection, Environment, Request

from tempo_common import CO_BBOX, DEFAULT_SHORT_NAME, DEFAULT_VERSION, MISSION_DATA_START, month_chunks, resolve_concept_id
from tempo_no2_to_geotiff import DEFAULT_RADIUS_OF_INFLUENCE_M, DEFAULT_RESOLUTION_DEG, convert_granule

MANIFEST_LOCK = threading.Lock()
log = logging.getLogger("tempo_no2")


def setup_logging(log_path: Path) -> None:
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    log.addHandler(console)
    log.addHandler(file_handler)


def load_manifest(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_manifest(path: Path, manifest: dict) -> None:
    with MANIFEST_LOCK:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".manifest-", suffix=".tmp")
        try:
            with open(fd, "w") as f:
                json.dump(manifest, f, indent=2, default=str)
            Path(tmp_name).replace(path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


def upload_to_s3(s3_client, local_path: Path, bucket: str, key: str) -> str:
    """Upload local_path to s3://bucket/key and return that URI."""
    s3_client.upload_file(str(local_path), bucket, key)
    return f"s3://{bucket}/{key}"


def convert_and_cleanup(
    files: list[str],
    keep_nc: bool,
    resolution_deg: float,
    radius_of_influence_m: float,
    s3_client=None,
    s3_bucket: str | None = None,
    s3_prefix: str = "",
) -> tuple[list[str], list[str], int, int]:
    """Convert each downloaded .nc to GeoTIFF, then either upload both to S3
    (deleting the local copies once each upload succeeds) or, without S3,
    keep the .tif locally and delete the .nc unless --keep-nc.

    Returns (tif_locations, nc_locations, n_empty, n_failed). Locations are
    s3:// URIs when s3_bucket is set, otherwise local filesystem paths.
    """
    tif_locations = []
    nc_locations = []
    n_empty = n_failed = 0
    for nc_file in files:
        nc_path = Path(nc_file)
        tif_path = nc_path.with_suffix(".tif")
        try:
            result = convert_granule(nc_path, tif_path, resolution_deg=resolution_deg,
                                      radius_of_influence_m=radius_of_influence_m)
            wrote_tif = result == "written"
            if not wrote_tif:
                n_empty += 1

            if s3_bucket:
                nc_key = f"{s3_prefix}/{nc_path.name}"
                nc_locations.append(upload_to_s3(s3_client, nc_path, s3_bucket, nc_key))
                nc_path.unlink(missing_ok=True)
                if wrote_tif:
                    tif_key = f"{s3_prefix}/{tif_path.name}"
                    tif_locations.append(upload_to_s3(s3_client, tif_path, s3_bucket, tif_key))
                    tif_path.unlink(missing_ok=True)
            else:
                if wrote_tif:
                    tif_locations.append(str(tif_path))
                if keep_nc:
                    nc_locations.append(str(nc_path))
                else:
                    nc_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - keep going on per-file failures
            n_failed += 1
            log.error("%s: conversion/upload FAILED - %s (keeping local file(s))", nc_path.name, exc)
    return tif_locations, nc_locations, n_empty, n_failed


def process_chunk(
    client: Client,
    collection: Collection,
    chunk_start: dt.date,
    chunk_end: dt.date,
    out_dir: Path,
    manifest_path: Path,
    manifest: dict,
    max_retries: int = 3,
    keep_nc: bool = False,
    resolution_deg: float = DEFAULT_RESOLUTION_DEG,
    radius_of_influence_m: float = DEFAULT_RADIUS_OF_INFLUENCE_M,
    s3_client=None,
    s3_bucket: str | None = None,
    s3_prefix: str = "",
) -> str:
    key = chunk_start.isoformat()
    entry = manifest.setdefault(key, {"status": "pending"})

    if entry.get("status") == "done":
        return f"{key}: already done ({len(entry.get('tif_files', entry.get('files', [])))} GeoTIFF(s)), skipping"

    dest = out_dir / f"{chunk_start.year:04d}" / f"{chunk_start.month:02d}"
    dest.mkdir(parents=True, exist_ok=True)

    west, south, east, north = CO_BBOX
    request = Request(
        collection=collection,
        spatial=BBox(west, south, east, north),
        temporal={
            "start": dt.datetime.combine(chunk_start, dt.time.min),
            "stop": dt.datetime.combine(chunk_end, dt.time.max),
        },
    )
    if not request.is_valid():
        entry["status"] = "failed"
        entry["error"] = "; ".join(request.error_messages())
        save_manifest(manifest_path, manifest)
        return f"{key}: INVALID REQUEST - {entry['error']}"

    last_err = None
    for attempt in range(1, max_retries + 1):
        chunk_started = time.monotonic()
        try:
            log.info("%s: submitting Harmony job (attempt %d/%d)", key, attempt, max_retries)
            job_id = client.submit(request)
            entry["status"] = "submitted"
            entry["job_id"] = job_id
            save_manifest(manifest_path, manifest)

            client.wait_for_processing(job_id, show_progress=False)
            submit_to_ready_s = time.monotonic() - chunk_started
            log.info("%s: job %s ready after %.1fs, downloading", key, job_id, submit_to_ready_s)

            download_started = time.monotonic()
            files = []
            for future in client.download_all(job_id, directory=str(dest), overwrite=False):
                files.append(str(future.result()))
            download_s = time.monotonic() - download_started
            log.info("%s: downloaded %d granule(s) in %.1fs", key, len(files), download_s)

            convert_started = time.monotonic()
            chunk_s3_prefix = f"{s3_prefix}/{chunk_start.year:04d}/{chunk_start.month:02d}"
            tif_files, nc_files, n_empty, n_failed = convert_and_cleanup(
                files, keep_nc, resolution_deg, radius_of_influence_m,
                s3_client=s3_client, s3_bucket=s3_bucket, s3_prefix=chunk_s3_prefix,
            )
            convert_s = time.monotonic() - convert_started
            total_s = time.monotonic() - chunk_started

            entry["status"] = "done"
            entry["tif_files"] = tif_files
            entry["nc_files"] = nc_files
            entry["n_empty"] = n_empty
            entry["n_conversion_failed"] = n_failed
            entry["timing_s"] = {
                "submit_to_ready": round(submit_to_ready_s, 1),
                "download": round(download_s, 1),
                "convert_and_upload": round(convert_s, 1),
                "total": round(total_s, 1),
            }
            entry.pop("error", None)
            save_manifest(manifest_path, manifest)
            summary = f"{len(tif_files)} GeoTIFF(s)"
            if n_empty:
                summary += f", {n_empty} empty"
            if n_failed:
                summary += f", {n_failed} conversion FAILED"
            return (f"{key}: downloaded {len(files)} granule(s) -> {summary} in {dest} "
                    f"[{total_s:.1f}s total: {submit_to_ready_s:.1f}s wait, {download_s:.1f}s download, "
                    f"{convert_s:.1f}s convert+upload]")
        except Exception as exc:  # noqa: BLE001 - retry loop, want to catch broadly
            last_err = exc
            entry["status"] = "retrying"
            entry["error"] = str(exc)
            save_manifest(manifest_path, manifest)
            log.warning("%s: attempt %d/%d failed - %s", key, attempt, max_retries, exc)
            if attempt < max_retries:
                backoff = 2 ** attempt
                time.sleep(backoff)

    entry["status"] = "failed"
    entry["error"] = str(last_err)
    save_manifest(manifest_path, manifest)
    return f"{key}: FAILED after {max_retries} attempts - {last_err}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=dt.date.fromisoformat, default=MISSION_DATA_START,
                         help="Start date YYYY-MM-DD (default: TEMPO mission data start)")
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today(),
                         help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--short-name", default=DEFAULT_SHORT_NAME, help="CMR short_name of the TEMPO product")
    parser.add_argument("--version", default=DEFAULT_VERSION, help=f"CMR product version (default: {DEFAULT_VERSION})")
    parser.add_argument("--out-dir", type=Path, default=Path("data/tempo_no2_co"), help="Output directory")
    parser.add_argument("--manifest", type=Path, default=None,
                         help="Manifest path (default: <out-dir>/manifest.json)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent Harmony jobs (default: 4)")
    parser.add_argument("--keep-nc", action="store_true",
                         help="Keep source .nc files after GeoTIFF conversion (default: delete them)")
    parser.add_argument("--resolution-deg", type=float, default=DEFAULT_RESOLUTION_DEG,
                         help=f"GeoTIFF grid resolution in degrees (default: {DEFAULT_RESOLUTION_DEG})")
    parser.add_argument("--radius-of-influence-m", type=float, default=DEFAULT_RADIUS_OF_INFLUENCE_M,
                         help=f"GeoTIFF resampling search radius in meters (default: {DEFAULT_RADIUS_OF_INFLUENCE_M})")
    parser.add_argument("--s3-bucket", default=os.environ.get("TEMPO_S3_BUCKET"),
                         help="If set, upload every finished .nc and .tif to this S3 bucket and delete the "
                              "local copies once each upload succeeds, instead of keeping files on local disk "
                              "(env: TEMPO_S3_BUCKET). Uses boto3's default credential chain "
                              "(AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN, or an instance/task role).")
    parser.add_argument("--s3-prefix", default=os.environ.get("TEMPO_S3_PREFIX", "tempo_no2_co"),
                         help="S3 key prefix for uploaded files (default: tempo_no2_co, env: TEMPO_S3_PREFIX)")
    parser.add_argument("--log-file", type=Path, default=None,
                         help="Log file path (default: <out-dir>/pipeline.log)")
    args = parser.parse_args()

    if args.end < args.start:
        parser.error("--end must be >= --start")

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest or (out_dir / "manifest.json")
    log_path = args.log_file or (out_dir / "pipeline.log")
    setup_logging(log_path)
    log.info("Logging to %s", log_path)

    log.info("Checking Earthdata authentication...")
    # "all" tries, in order: EARTHDATA_USERNAME/EARTHDATA_PASSWORD env vars,
    # then ~/.netrc, then an interactive prompt - so this works both in
    # non-interactive environments (env vars set) and on a local machine
    # that's already run setup_earthdata_auth.py.
    auth = earthaccess.login(strategy="all", persist=True)
    if not auth.authenticated:
        log.error("Not authenticated. Run scripts/setup_earthdata_auth.py, or set "
                   "EARTHDATA_USERNAME/EARTHDATA_PASSWORD env vars, first.")
        return 1

    # harmony-py (used below for the actual granule downloads) reads its own
    # EDL_USERNAME/EDL_PASSWORD env vars rather than earthaccess's, and
    # earthaccess's "environment" login strategy doesn't write ~/.netrc - so
    # without this, EARTHDATA_USERNAME/PASSWORD alone passes the check above
    # but then fails inside Harmony with an opaque non-JSON-response error.
    if os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD"):
        os.environ.setdefault("EDL_USERNAME", os.environ["EARTHDATA_USERNAME"])
        os.environ.setdefault("EDL_PASSWORD", os.environ["EARTHDATA_PASSWORD"])

    concept_id = resolve_concept_id(args.short_name, args.version)
    collection = Collection(id=concept_id)
    client = Client(env=Environment.PROD)

    s3_client = boto3.client("s3") if args.s3_bucket else None

    manifest = load_manifest(manifest_path)
    chunks = list(month_chunks(args.start, args.end))
    log.info("Processing %d month(s) from %s to %s with %d concurrent Harmony job(s).",
              len(chunks), args.start, args.end, args.workers)
    log.info("Colorado bbox: %s", CO_BBOX)
    if args.s3_bucket:
        log.info("Output: s3://%s/%s/ (local disk used only as scratch space)", args.s3_bucket, args.s3_prefix)
    else:
        log.info("Output dir: %s", out_dir)
    log.info("Manifest: %s", manifest_path)

    run_started = time.monotonic()
    failures = 0
    total_granules = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_chunk, client, collection, cs, ce, out_dir, manifest_path, manifest,
                        keep_nc=args.keep_nc, resolution_deg=args.resolution_deg,
                        radius_of_influence_m=args.radius_of_influence_m,
                        s3_client=s3_client, s3_bucket=args.s3_bucket, s3_prefix=args.s3_prefix): cs
            for cs, ce in chunks
        }
        for future in as_completed(futures):
            result = future.result()
            log.info(result)
            if "FAILED" in result or "INVALID" in result:
                failures += 1

    run_s = time.monotonic() - run_started
    for cs, _ in chunks:
        entry = manifest.get(cs.isoformat(), {})
        total_granules += len(entry.get("tif_files", [])) + entry.get("n_empty", 0)

    log.info("Done. %d/%d month(s) succeeded.", len(chunks) - failures, len(chunks))
    if total_granules:
        log.info("Run took %.1fs total for %d granule(s) this run -> %.2fs/granule average "
                  "(wall-clock, %d worker(s)).", run_s, total_granules, run_s / total_granules, args.workers)
    if failures:
        log.error("%d month(s) failed - re-run the same command to retry just those "
                   "(completed months are skipped via %s).", failures, manifest_path)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
