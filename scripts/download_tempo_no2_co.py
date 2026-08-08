#!/usr/bin/env python3
"""Download TEMPO NO2 L2 data over Colorado + the DJ Basin, fast.

Strategy: instead of pulling full continent-wide L2 granules (multi-TB for
the full mission), we ask NASA's Harmony service to spatially subset each
granule to a regional bounding box *before* it leaves NASA's servers. That
cuts per-file size by ~95%+, which is what makes "download the whole TEMPO
NO2 archive for this region in a few hours" realistic.

The date range is split into one Harmony job per calendar month and jobs
are processed concurrently (submit + wait + download), so we're not
waiting on one giant serial job. Each downloaded granule is then regridded
onto a fixed 1km x 1km GeoTIFF (NO2 troposphere/stratosphere, QC flag,
cloud fraction - see regrid_to_geotiff.py) and both files are uploaded to
S3. Before doing any of this for a given month, the S3 bucket is checked
first - if that month is already there, the whole month is skipped. That
bucket check (not just the local manifest) is what makes the pipeline safe
to stop and restart from any machine.

Usage:
    python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07

    # Resume/re-run: already-completed months are skipped automatically.
    python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07

    # Tune concurrency (default 4 concurrent Harmony jobs):
    python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07 --workers 6
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
import earthaccess
from harmony import BBox, Client, Collection, Environment, Request

from regrid_to_geotiff import regrid_file
from tempo_common import (
    CO_BBOX,
    DEFAULT_BUCKET,
    DEFAULT_BUCKET_PREFIX,
    DEFAULT_SHORT_NAME,
    MISSION_DATA_START,
    bucket_has_month,
    month_chunks,
    resolve_concept_id,
    upload_month_to_bucket,
)

MANIFEST_LOCK = threading.Lock()

# Harmony auto-pauses jobs above a certain size as a safety check. The
# harmony-py client's own wait_for_processing() treats "paused" as a stop
# condition - it just prints a message and returns, leaving whatever
# fraction of the job ran before the pause as if it were the final result.
# For a month-sized request that silently truncates a month's worth of
# granules down to whatever trickled through before the pause. Poll and
# resume ourselves instead so we actually wait for real completion.
MAX_RESUMES = 20


def wait_for_processing_resuming(client: Client, job_id: str) -> None:
    resumes = 0
    progress = 0
    while progress < 100:
        progress, status, message = client.progress(job_id)
        if status == "failed":
            raise RuntimeError(f"Harmony job {job_id} failed: {message}")
        if status == "canceled":
            raise RuntimeError(f"Harmony job {job_id} was canceled: {message}")
        if status in ("successful", "complete_with_errors"):
            return
        if status == "paused":
            resumes += 1
            if resumes > MAX_RESUMES:
                raise RuntimeError(
                    f"Harmony job {job_id} paused {resumes} times without completing - "
                    f"giving up rather than looping forever."
                )
            client.resume(job_id)
            continue
        time.sleep(client.check_interval)


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


def process_chunk(
    client: Client,
    collection: Collection,
    chunk_start: dt.date,
    chunk_end: dt.date,
    out_dir: Path,
    manifest_path: Path,
    manifest: dict,
    s3_client=None,
    bucket: str | None = None,
    bucket_prefix: str = DEFAULT_BUCKET_PREFIX,
    max_retries: int = 3,
) -> str:
    key = chunk_start.isoformat()
    entry = manifest.setdefault(key, {"status": "pending"})

    if entry.get("status") == "done":
        return f"{key}: already done ({len(entry.get('files', []))} files), skipping"

    # Bucket-first check: this is what lets the pipeline be safely stopped
    # ("off") and resumed ("on") later, even from a machine with no local
    # disk state - the bucket, not the local manifest, is the source of
    # truth for what's already downloaded.
    if s3_client is not None and bucket:
        existing_keys = bucket_has_month(s3_client, bucket, bucket_prefix, chunk_start)
        if existing_keys:
            entry["status"] = "done"
            entry["s3_keys"] = existing_keys
            entry.pop("error", None)
            save_manifest(manifest_path, manifest)
            return f"{key}: already in s3://{bucket}/ ({len(existing_keys)} file(s)), skipping download"

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
        try:
            job_id = client.submit(request)
            entry["status"] = "submitted"
            entry["job_id"] = job_id
            save_manifest(manifest_path, manifest)

            wait_for_processing_resuming(client, job_id)

            files = []
            for future in client.download_all(job_id, directory=str(dest), overwrite=False):
                files.append(str(future.result()))

            # Defense in depth against a repeat of the Harmony-pause bug (or any other
            # failure mode that returns a partial result without erroring): cross-check
            # the download count against CMR's own count for the identical collection +
            # bbox + date window before ever calling this month "done". A silent partial
            # month would otherwise look complete forever, since the bucket-first check
            # only asks "does anything exist here", not "is everything here".
            expected = (
                earthaccess.DataGranules()
                .concept_id(collection.id)
                .bounding_box(*CO_BBOX)
                .temporal(chunk_start.isoformat(), chunk_end.isoformat())
                .hits()
            )
            if expected > 0 and len(files) < expected * 0.95:
                raise RuntimeError(
                    f"Got {len(files)} file(s) but CMR reports {expected} granule(s) exist "
                    f"for this window - Harmony likely returned a partial result (e.g. an "
                    f"auto-paused job). Not marking this month done."
                )

            tif_files = []
            for nc_file in files:
                if Path(nc_file).suffix.lower() in (".nc", ".nc4"):
                    tif_files.append(str(regrid_file(Path(nc_file))))

            entry["status"] = "done"
            entry["files"] = files
            entry["tif_files"] = tif_files
            entry.pop("error", None)

            upload_note = ""
            if s3_client is not None and bucket:
                s3_keys = upload_month_to_bucket(s3_client, bucket, bucket_prefix, chunk_start, files + tif_files)
                entry["s3_keys"] = s3_keys
                upload_note = f", uploaded to s3://{bucket}/"

            save_manifest(manifest_path, manifest)
            return (f"{key}: downloaded {len(files)} file(s), regridded {len(tif_files)} to 1km GeoTIFF "
                    f"-> {dest}{upload_note}")
        except Exception as exc:  # noqa: BLE001 - retry loop, want to catch broadly
            last_err = exc
            entry["status"] = "retrying"
            entry["error"] = str(exc)
            save_manifest(manifest_path, manifest)
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
    parser.add_argument("--out-dir", type=Path, default=Path("data/tempo_no2_co"), help="Output directory")
    parser.add_argument("--manifest", type=Path, default=None,
                         help="Manifest path (default: <out-dir>/manifest.json)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent Harmony jobs (default: 4)")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET,
                         help=f"S3 bucket checked first for already-downloaded months, and that "
                              f"finished months are uploaded to (default: {DEFAULT_BUCKET})")
    parser.add_argument("--bucket-prefix", default=DEFAULT_BUCKET_PREFIX,
                         help=f"Key prefix within the bucket (default: {DEFAULT_BUCKET_PREFIX})")
    parser.add_argument("--no-bucket", action="store_true",
                         help="Disable S3 entirely; fall back to the local manifest only "
                              "(this turns off the on/off resume-from-bucket behavior).")
    args = parser.parse_args()

    if args.end < args.start:
        parser.error("--end must be >= --start")

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest or (out_dir / "manifest.json")

    print("Checking Earthdata authentication...")
    auth = earthaccess.login(strategy="netrc")
    if not auth.authenticated:
        print("Not authenticated. Run scripts/setup_earthdata_auth.py first.", file=sys.stderr)
        return 1

    concept_id = resolve_concept_id(args.short_name)
    collection = Collection(id=concept_id)
    client = Client(env=Environment.PROD)

    s3_client = None if args.no_bucket else boto3.client("s3")

    manifest = load_manifest(manifest_path)
    chunks = list(month_chunks(args.start, args.end))
    print(f"Processing {len(chunks)} month(s) from {args.start} to {args.end} "
          f"with {args.workers} concurrent Harmony job(s).")
    print(f"Colorado + DJ Basin bbox: {CO_BBOX}")
    print(f"Output dir: {out_dir}")
    print(f"Manifest: {manifest_path}")
    if s3_client is not None:
        print(f"Bucket (checked first, resumed from): s3://{args.bucket}/{args.bucket_prefix}/")
    else:
        print("Bucket checks disabled (--no-bucket); resuming from local manifest only.")

    failures = 0
    total = len(chunks)
    completed = 0
    run_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                process_chunk, client, collection, cs, ce, out_dir, manifest_path, manifest,
                s3_client, args.bucket, args.bucket_prefix,
            ): cs
            for cs, ce in chunks
        }
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            if "FAILED" in result or "INVALID" in result:
                failures += 1

            elapsed = time.monotonic() - run_start
            remaining = total - completed
            # Average pace over completed months, projected onto what's left. Only
            # meaningful once at least one month has finished - before that there's
            # nothing to extrapolate from, so ETA is left out rather than shown as 0:00.
            eta = ""
            if completed > 0 and remaining > 0:
                avg_per_month = elapsed / completed
                eta_seconds = avg_per_month * remaining
                eta = f", ETA {dt.timedelta(seconds=round(eta_seconds))}"
            print(f"[{completed}/{total} done, {remaining} left{eta}] {result}")

    print(f"\nDone. {len(chunks) - failures}/{len(chunks)} month(s) succeeded.")
    if failures:
        print(f"{failures} month(s) failed - re-run the same command to retry just those "
              f"(completed months are skipped via {manifest_path}).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
