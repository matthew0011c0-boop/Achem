#!/usr/bin/env python3
"""Switch which Earthdata account the pipeline authenticates as.

Reads secrets/earthdata_accounts.json (gitignored - never commit real
credentials; copy secrets/earthdata_accounts.json.example to get the
expected format) and writes ~/.netrc for the chosen username, so
earthaccess/harmony-py pick it up the same way they always do.

Usage:
    python scripts/use_earthdata_account.py matthew0011c1
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ACCOUNTS_FILE = Path(__file__).resolve().parent.parent / "secrets" / "earthdata_accounts.json"


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <username>", file=sys.stderr)
        return 1
    username = sys.argv[1]

    if not ACCOUNTS_FILE.exists():
        print(f"{ACCOUNTS_FILE} not found - copy "
              f"secrets/earthdata_accounts.json.example to that path and fill in real "
              f"credentials (the real file is gitignored, never commit it).", file=sys.stderr)
        return 1

    accounts = json.loads(ACCOUNTS_FILE.read_text())
    if username not in accounts:
        print(f"{username!r} not in {ACCOUNTS_FILE} (have: {list(accounts)})", file=sys.stderr)
        return 1

    netrc_path = Path.home() / ".netrc"
    netrc_path.write_text(
        f"machine urs.earthdata.nasa.gov\n  login {username}\n  password {accounts[username]}\n"
    )
    netrc_path.chmod(0o600)
    print(f"Wrote {netrc_path} for {username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
