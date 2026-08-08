#!/usr/bin/env python3
"""Run download_tempo_no2_co.py for multiple Earthdata accounts at once, in
one process/container, each covering a disjoint slice of the date range.

Each account runs as its own subprocess with EARTHDATA_USERNAME set to that
account and EARTHDATA_PASSWORD set to the one shared password - no netrc
file, so the accounts don't collide with each other. All accounts still
check/upload against the same S3 bucket, so this looks identical to one
long single-account run from the bucket's point of view - just faster,
since Harmony's per-account concurrent-job cap applies separately to each.

Usage:
    EARTHDATA_PASSWORD=hunter2 python scripts/download_tempo_no2_co_multi.py \
        --accounts matthew0011c0,matthew0011c1,matthew0011c2 \
        --start 2023-08-01 --end 2026-08-08
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import threading
from pathlib import Path

from tempo_common import (
    DEFAULT_BUCKET,
    DEFAULT_BUCKET_PREFIX,
    DEFAULT_SHORT_NAME,
    MISSION_DATA_START,
    split_range_by_month,
)

SCRIPT_DIR = Path(__file__).resolve().parent


def stream_output(proc: subprocess.Popen, label: str) -> None:
    for line in proc.stdout:
        print(f"[{label}] {line}", end="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="matthew0011c0,matthew0011c1,matthew0011c2",
                         help="Comma-separated Earthdata usernames, all sharing EARTHDATA_PASSWORD")
    parser.add_argument("--start", type=dt.date.fromisoformat, default=MISSION_DATA_START,
                         help="Start date YYYY-MM-DD (default: TEMPO mission data start)")
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today(),
                         help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--short-name", default=DEFAULT_SHORT_NAME, help="CMR short_name of the TEMPO product")
    parser.add_argument("--out-dir", type=Path, default=Path("data/tempo_no2_co"), help="Output directory")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent Harmony jobs PER account")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--bucket-prefix", default=DEFAULT_BUCKET_PREFIX)
    args = parser.parse_args()

    if args.end < args.start:
        parser.error("--end must be >= --start")

    password = os.environ.get("EARTHDATA_PASSWORD")
    if not password:
        print("EARTHDATA_PASSWORD must be set (shared across all --accounts).", file=sys.stderr)
        return 1

    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    if not accounts:
        print("No accounts given via --accounts.", file=sys.stderr)
        return 1

    ranges = split_range_by_month(args.start, args.end, len(accounts))
    if len(ranges) < len(accounts):
        print(f"Only {len(ranges)} month(s) in range for {len(accounts)} account(s); "
              f"{len(accounts) - len(ranges)} account(s) have nothing to do.")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    procs = []
    threads = []
    for account, (sub_start, sub_end) in zip(accounts, ranges):
        env = os.environ.copy()
        env["EARTHDATA_USERNAME"] = account
        env["EARTHDATA_PASSWORD"] = password
        manifest_path = args.out_dir / f"manifest-{account}.json"
        cmd = [
            sys.executable, str(SCRIPT_DIR / "download_tempo_no2_co.py"),
            "--start", sub_start.isoformat(), "--end", sub_end.isoformat(),
            "--short-name", args.short_name,
            "--out-dir", str(args.out_dir),
            "--manifest", str(manifest_path),
            "--workers", str(args.workers),
            "--bucket", args.bucket,
            "--bucket-prefix", args.bucket_prefix,
        ]
        print(f"[{account}] assigned {sub_start} -> {sub_end}")
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
        procs.append((account, proc))
        t = threading.Thread(target=stream_output, args=(proc, account), daemon=True)
        t.start()
        threads.append(t)

    failures = 0
    for account, proc in procs:
        code = proc.wait()
        if code != 0:
            failures += 1
            print(f"[{account}] exited with code {code}")
    for t in threads:
        t.join(timeout=5)

    if failures:
        print(f"\n{failures}/{len(procs)} account(s) failed - re-run the same command to retry "
              f"(each account's manifest is preserved, so finished months are skipped).")
        return 1
    print(f"\nAll {len(procs)} account(s) completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
