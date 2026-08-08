#!/bin/sh
# Provisions ~/.netrc from EARTHDATA_USERNAME/EARTHDATA_PASSWORD env vars
# (e.g. injected from AWS Secrets Manager into the container) before running
# the downloader. If those env vars aren't set, falls through and relies on
# whatever ~/.netrc already exists (e.g. a local dev container with a
# mounted netrc) - both earthaccess and harmony-py read it the same way
# either way, so no Python code needs to know which case it's in.
set -eu

if [ -n "${EARTHDATA_USERNAME:-}" ] && [ -n "${EARTHDATA_PASSWORD:-}" ]; then
  netrc="${HOME:-/root}/.netrc"
  cat > "$netrc" <<EOF
machine urs.earthdata.nasa.gov
  login ${EARTHDATA_USERNAME}
  password ${EARTHDATA_PASSWORD}
EOF
  chmod 600 "$netrc"
fi

exec python scripts/download_tempo_no2_co.py "$@"
