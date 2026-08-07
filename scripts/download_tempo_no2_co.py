#!/usr/bin/env python3
"""Download TEMPO NO2 L2 data over Colorado, fast.

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

import earthaccess
from harmony import BBox, Client, Collection, Environment, Request

from tempo_common import CO_BBOX, DEFAULT_SHORT_NAME, MISSION_DATA_START, month_chunks, resolve_concept_id

MANIFEST_LOCK = threading.Lock()


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
    max_retries: int = 3,
) -> str:
    key = chunk_start.isoformat()
    entry = manifest.setdefault(key, {"status": "pending"})

    if entry.get("status") == "done":
        return f"{key}: already done ({len(entry.get('files', []))} files), skipping"

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

            client.wait_for_processing(job_id, show_progress=False)

            files = []
            for future in client.download_all(job_id, directory=str(dest), overwrite=False):
                files.append(str(future.result()))

            entry["status"] = "done"
            entry["files"] = files
            entry.pop("error", None)
            save_manifest(manifest_path, manifest)
            return f"{key}: downloaded {len(files)} file(s) -> {dest}"
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

    manifest = load_manifest(manifest_path)
    chunks = list(month_chunks(args.start, args.end))
    print(f"Processing {len(chunks)} month(s) from {args.start} to {args.end} "
          f"with {args.workers} concurrent Harmony job(s).")
    print(f"Colorado bbox: {CO_BBOX}")
    print(f"Output dir: {out_dir}")
    print(f"Manifest: {manifest_path}")

    failures = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_chunk, client, collection, cs, ce, out_dir, manifest_path, manifest): cs
            for cs, ce in chunks
        }
        for future in as_completed(futures):
            result = future.result()
            print(result)
            if "FAILED" in result or "INVALID" in result:
                failures += 1

    print(f"\nDone. {len(chunks) - failures}/{len(chunks)} month(s) succeeded.")
    if failures:
        print(f"{failures} month(s) failed - re-run the same command to retry just those "
              f"(completed months are skipped via {manifest_path}).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
