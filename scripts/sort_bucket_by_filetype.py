#!/usr/bin/env python3
"""One-off migration: split an existing flat <prefix>/<year>/<month>/ bucket
layout into separate <prefix>/nc/<year>/<month>/ and <prefix>/tiffs/<year>/<month>/
folders, by file extension. Year/month structure is preserved inside each.

S3 has no rename, so each object is copied to its new key and the old key is
deleted. Objects already under <prefix>/nc/ or <prefix>/tiffs/ are skipped
(safe to re-run).

Usage:
    python scripts/sort_bucket_by_filetype.py --dry-run   # preview only
    python scripts/sort_bucket_by_filetype.py              # actually move objects
"""
from __future__ import annotations

import argparse
import re
import sys

import boto3

from tempo_common import DEFAULT_BUCKET, DEFAULT_BUCKET_PREFIX, NC_SUBDIR, TIF_SUBDIR, _kind_for

# Matches "<prefix>/<year>/<month>/<filename>" (the old flat layout), e.g.
# "tempo_no2_co/2024/06/foo.nc". Anything not shaped like this (including
# already-migrated "<prefix>/nc/..." / "<prefix>/tiffs/..." keys) is skipped.
FLAT_KEY_RE = re.compile(r"^(?P<prefix>.+)/(?P<year>\d{4})/(?P<month>\d{2})/(?P<filename>[^/]+)$")


def plan_moves(keys: list[str], bucket_prefix: str) -> list[tuple[str, str]]:
    moves = []
    prefix = bucket_prefix.rstrip("/")
    skip_dirs = {f"{prefix}/{NC_SUBDIR}", f"{prefix}/{TIF_SUBDIR}"}
    for key in keys:
        if any(key.startswith(d + "/") for d in skip_dirs):
            continue
        m = FLAT_KEY_RE.match(key)
        if not m or m.group("prefix") != prefix:
            continue
        try:
            kind = _kind_for(m.group("filename"))
        except ValueError:
            continue
        new_key = f"{prefix}/{kind}/{m.group('year')}/{m.group('month')}/{m.group('filename')}"
        moves.append((key, new_key))
    return moves


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help=f"S3 bucket (default: {DEFAULT_BUCKET})")
    parser.add_argument("--bucket-prefix", default=DEFAULT_BUCKET_PREFIX,
                         help=f"Key prefix to sort within (default: {DEFAULT_BUCKET_PREFIX})")
    parser.add_argument("--dry-run", action="store_true", help="List what would move without changing anything")
    args = parser.parse_args()

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=args.bucket, Prefix=f"{args.bucket_prefix.rstrip('/')}/"):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))

    moves = plan_moves(keys, args.bucket_prefix)
    if not moves:
        print(f"Nothing to sort under s3://{args.bucket}/{args.bucket_prefix}/ "
              f"(already sorted, or no matching .nc/.nc4/.tif/.tiff files).")
        return 0

    print(f"{len(moves)} object(s) to move under s3://{args.bucket}/{args.bucket_prefix}/:")
    for old_key, new_key in moves:
        print(f"  {old_key}  ->  {new_key}")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return 0

    failures = 0
    for old_key, new_key in moves:
        try:
            s3.copy_object(Bucket=args.bucket, CopySource={"Bucket": args.bucket, "Key": old_key}, Key=new_key)
            s3.delete_object(Bucket=args.bucket, Key=old_key)
        except Exception as exc:  # noqa: BLE001 - report and continue with the rest of the batch
            print(f"FAILED: {old_key} -> {new_key}: {exc}", file=sys.stderr)
            failures += 1

    moved = len(moves) - failures
    print(f"\nDone. {moved}/{len(moves)} object(s) moved.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
