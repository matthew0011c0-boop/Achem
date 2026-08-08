#!/usr/bin/env bash
# Entrypoint for running the TEMPO Colorado downloader as an AWS Batch job.
#
# Required env vars:
#   EARTHDATA_SECRET_ID  - Secrets Manager secret id/ARN holding
#                           {"username": "...", "password": "..."}
#   S3_BUCKET            - bucket to persist data/ and manifest.json to
#
# Optional env vars:
#   S3_PREFIX     (default: tempo_no2_co)
#   START_DATE    (default: script's own default, i.e. mission start)
#   END_DATE      (default: today)
#   SHORT_NAME    (default: TEMPO_NO2_L2)
#   WORKERS       (default: 4)
#   SYNC_INTERVAL_SECONDS (default: 300) - how often to push progress to S3
#                         while the job runs, so a spot interruption doesn't
#                         lose completed months.
set -euo pipefail

: "${EARTHDATA_SECRET_ID:?EARTHDATA_SECRET_ID env var is required}"
: "${S3_BUCKET:?S3_BUCKET env var is required}"

S3_PREFIX="${S3_PREFIX:-tempo_no2_co}"
S3_URI="s3://${S3_BUCKET}/${S3_PREFIX}/"
OUT_DIR="data/tempo_no2_co"
SYNC_INTERVAL_SECONDS="${SYNC_INTERVAL_SECONDS:-300}"

echo "Fetching Earthdata credentials from Secrets Manager (${EARTHDATA_SECRET_ID})..."
python3 - "$EARTHDATA_SECRET_ID" <<'PY'
import json
import sys
import boto3

secret_id = sys.argv[1]
client = boto3.client("secretsmanager")
secret = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])

with open("/root/.netrc", "w") as f:
    f.write(
        f"machine urs.earthdata.nasa.gov login {secret['username']} "
        f"password {secret['password']}\n"
    )
PY
chmod 600 /root/.netrc

mkdir -p "$OUT_DIR"
echo "Pulling any existing progress from ${S3_URI}..."
aws s3 sync "$S3_URI" "$OUT_DIR" --no-progress || echo "Nothing to resume from yet."

# Periodically push progress to S3 so a killed/interrupted job (e.g. spot
# reclaim) doesn't lose already-completed months. The download script's own
# manifest.json is what makes re-running idempotent.
sync_loop() {
    while true; do
        sleep "$SYNC_INTERVAL_SECONDS"
        aws s3 sync "$OUT_DIR" "$S3_URI" --no-progress || true
    done
}
sync_loop &
SYNC_PID=$!

final_sync() {
    kill "$SYNC_PID" 2>/dev/null || true
    echo "Final sync to ${S3_URI}..."
    aws s3 sync "$OUT_DIR" "$S3_URI" --no-progress || true
}
trap final_sync EXIT

ARGS=(--out-dir "$OUT_DIR" --workers "${WORKERS:-4}")
[ -n "${START_DATE:-}" ] && ARGS+=(--start "$START_DATE")
[ -n "${END_DATE:-}" ] && ARGS+=(--end "$END_DATE")
[ -n "${SHORT_NAME:-}" ] && ARGS+=(--short-name "$SHORT_NAME")

echo "Running: python3 scripts/download_tempo_no2_co.py ${ARGS[*]}"
python3 scripts/download_tempo_no2_co.py "${ARGS[@]}"
