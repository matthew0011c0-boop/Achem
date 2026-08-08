#!/bin/sh
# Two modes, both authenticating straight from env vars (no ~/.netrc file,
# since a shared file would race between concurrent multi-account runs):
#
# - EARTHDATA_ACCOUNTS set (comma-separated usernames) + EARTHDATA_PASSWORD
#   (one password shared by all of them): runs download_tempo_no2_co_multi.py,
#   which fans out one subprocess per account, each covering a slice of the
#   date range, all in this one container.
# - Otherwise, EARTHDATA_USERNAME + EARTHDATA_PASSWORD (single account):
#   runs download_tempo_no2_co.py directly.
set -eu

if [ -n "${EARTHDATA_ACCOUNTS:-}" ]; then
  exec python scripts/download_tempo_no2_co_multi.py --accounts "$EARTHDATA_ACCOUNTS" "$@"
fi

exec python scripts/download_tempo_no2_co.py "$@"
