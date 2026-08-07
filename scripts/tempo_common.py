"""Shared config/helpers for TEMPO Colorado download scripts."""
from __future__ import annotations

import datetime as dt

import earthaccess

# Colorado bounding box (west, south, east, north), in degrees.
# Slightly padded beyond the state border so edge pixels aren't clipped.
CO_BBOX = (-109.15, 36.95, -101.95, 41.05)

# TEMPO L2 NO2 tropospheric column product.
DEFAULT_SHORT_NAME = "TEMPO_NO2_L2"

# Public data availability start (TEMPO L2 NO2 V03 science data).
MISSION_DATA_START = dt.date(2023, 8, 1)


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
