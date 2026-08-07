#!/usr/bin/env python3
"""Interactive one-time setup for NASA Earthdata Login credentials.

Creates/updates ~/.netrc via earthaccess so later scripts (and Harmony)
can authenticate without prompting again. Run this once:

    python scripts/setup_earthdata_auth.py
"""
import sys

import earthaccess


def main() -> int:
    print("Logging in to NASA Earthdata (urs.earthdata.nasa.gov)...")
    auth = earthaccess.login(strategy="interactive", persist=True)
    if not auth.authenticated:
        print("Authentication failed. Check your username/password and try again.", file=sys.stderr)
        return 1

    print("Authenticated and credentials saved to ~/.netrc")

    # Sanity check: confirm we can query CMR for a known TEMPO collection.
    results = earthaccess.search_datasets(short_name="TEMPO_NO2_L2")
    if not results:
        print("Warning: could not find TEMPO_NO2_L2 in CMR with these credentials.", file=sys.stderr)
        return 1

    print(f"Confirmed CMR access: found collection '{results[0]['umm']['ShortName']}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
