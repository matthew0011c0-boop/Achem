"""Shared config/helpers for TEMPO Colorado download scripts."""
from __future__ import annotations

import datetime as dt

import boto3
import earthaccess
from botocore.exceptions import ClientError, NoCredentialsError

# Colorado + Denver-Julesburg (DJ/Julesburg) Basin bounding box
# (west, south, east, north), in degrees.
#
# Colorado's own border is roughly (-109.05, 37.0, -102.05, 41.0); the DJ
# Basin extends beyond it to the north (into SE Wyoming) and east (into W
# Nebraska / NW Kansas), so the box is widened on those two edges. South and
# west stay at Colorado's border since the basin doesn't extend past it
# there. All edges carry a small pad so edge pixels aren't clipped.
CO_BBOX = (-109.15, 36.95, -100.5, 43.05)

# TEMPO L2 NO2 tropospheric column product.
DEFAULT_SHORT_NAME = "TEMPO_NO2_L2"

# Public data availability start (TEMPO L2 NO2 V03 science data).
MISSION_DATA_START = dt.date(2023, 8, 1)

# Default S3 bucket used as the source of truth for what's already been
# downloaded. Lets the download script be stopped/restarted (or moved to a
# fresh machine with an empty local disk) without redoing finished months.
DEFAULT_BUCKET = "matt-achem-bucket2"
DEFAULT_BUCKET_PREFIX = "tempo_no2_co"


def resolve_concept_id(short_name: str = DEFAULT_SHORT_NAME) -> str:
    """Look up the current CMR concept-id for a TEMPO short_name.

    Concept-ids can change across provider/version updates, so we resolve
    this dynamically at run time rather than hardcoding it.
    """
    results = earthaccess.search_datasets(short_name=short_name)
    if not results:
        raise RuntimeError(f"No CMR collection found for short_name={short_name!r}")
    # Prefer the most recent version if multiple are returned.
    results.sort(key=lambda r: r["umm"].get("Version", ""), reverse=True)
    concept_id = results[0]["meta"]["concept-id"]
    version = results[0]["umm"].get("Version", "?")
    print(f"Resolved {short_name} v{version} -> {concept_id}")
    return concept_id


def month_chunks(start: dt.date, end: dt.date):
    """Yield (chunk_start, chunk_end) date pairs spanning one calendar month each."""
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        if cur.month == 12:
            nxt = dt.date(cur.year + 1, 1, 1)
        else:
            nxt = dt.date(cur.year, cur.month + 1, 1)
        chunk_start = max(cur, start)
        chunk_end = min(nxt - dt.timedelta(days=1), end)
        yield chunk_start, chunk_end
        cur = nxt


def month_prefix(bucket_prefix: str, chunk_start: dt.date) -> str:
    """S3 key prefix a given month's files are stored under."""
    return f"{bucket_prefix.rstrip('/')}/{chunk_start.year:04d}/{chunk_start.month:02d}/"


def bucket_has_month(s3_client, bucket: str, bucket_prefix: str, chunk_start: dt.date) -> list[str]:
    """Return the S3 keys already stored for this month, or [] if none.

    This is the "check the bucket first" step: it lets a stopped/restarted
    run (even on a fresh machine with no local disk state) recognize work
    that's already done without re-hitting Harmony.
    """
    prefix = month_prefix(bucket_prefix, chunk_start)
    try:
        resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except (ClientError, NoCredentialsError) as exc:
        raise RuntimeError(
            f"Could not list s3://{bucket}/{prefix} - check AWS credentials/permissions "
            f"(or pass --no-bucket to disable bucket checks): {exc}"
        ) from exc
    return [obj["Key"] for obj in resp.get("Contents", [])]


def upload_month_to_bucket(
    s3_client, bucket: str, bucket_prefix: str, chunk_start: dt.date, local_files: list[str]
) -> list[str]:
    """Upload downloaded files for a month to S3 so the bucket becomes the durable record."""
    prefix = month_prefix(bucket_prefix, chunk_start)
    keys = []
    for local_path in local_files:
        key = prefix + local_path.rsplit("/", 1)[-1]
        try:
            s3_client.upload_file(local_path, bucket, key)
        except (ClientError, NoCredentialsError) as exc:
            raise RuntimeError(f"Failed to upload {local_path} to s3://{bucket}/{key}: {exc}") from exc
        keys.append(key)
    return keys
