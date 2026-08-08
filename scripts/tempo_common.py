"""Shared config/helpers for TEMPO Colorado download scripts."""
from __future__ import annotations

import datetime as dt

import earthaccess

# Colorado bounding box (west, south, east, north), in degrees.
# Slightly padded beyond the state border so edge pixels aren't clipped.
CO_BBOX = (-109.15, 36.95, -101.95, 41.05)

# TEMPO L2 NO2 tropospheric column product.
DEFAULT_SHORT_NAME = "TEMPO_NO2_L2"

# Pinned explicitly (rather than "whatever's newest") so a future V05 release
# doesn't silently change what gets downloaded.
DEFAULT_VERSION = "V04"

# Public data availability start (TEMPO L2 NO2 V03 science data).
MISSION_DATA_START = dt.date(2023, 8, 1)


def resolve_concept_id(short_name: str = DEFAULT_SHORT_NAME, version: str = DEFAULT_VERSION) -> str:
    """Look up the CMR concept-id for a specific TEMPO short_name + version.

    Concept-ids can change across provider/version updates, so we resolve
    this dynamically at run time rather than hardcoding it.
    """
    results = earthaccess.search_datasets(short_name=short_name)
    if not results:
        raise RuntimeError(f"No CMR collection found for short_name={short_name!r}")
    matches = [r for r in results if r["umm"].get("Version", "").upper() == version.upper()]
    if not matches:
        available = sorted({r["umm"].get("Version", "?") for r in results})
        raise RuntimeError(
            f"No CMR collection found for short_name={short_name!r} version={version!r}. "
            f"Available versions: {available}"
        )
    concept_id = matches[0]["meta"]["concept-id"]
    print(f"Resolved {short_name} {version} -> {concept_id}")
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
