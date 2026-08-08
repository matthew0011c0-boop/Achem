#!/bin/bash
# Local credential setup for the Achem TEMPO pipeline on a new workstation.
#
# This file is NOT part of the git repo and should never be committed or
# pushed anywhere - copy it to the other machine directly (USB, scp, AirDrop,
# a password manager's file attachment, etc.), not through git.
#
# Usage: run this from inside your local clone of the Achem repo:
#   cd /path/to/Achem
#   bash /path/to/setup_credentials.sh
#
# It writes:
#   ~/.aws/credentials      - AWS profile "claude-login" (S3 access)
#   ./secrets/earthdata_accounts.json - the 3 Earthdata Login accounts
# Both destinations are gitignored already, so nothing here risks landing
# in a commit even if you run this from inside the repo.

set -eu

if [ ! -f "requirements.txt" ] || [ ! -d "scripts" ]; then
  echo "Run this from the root of your Achem repo clone (requirements.txt and scripts/ not found here)." >&2
  exit 1
fi

umask 077
mkdir -p ~/.aws
if ! grep -q '^\[claude-login\]' ~/.aws/credentials 2>/dev/null; then
  cat >> ~/.aws/credentials <<'AWSEOF'

[claude-login]
aws_access_key_id = AKIA6HFRA6JPY7TQXPLN
aws_secret_access_key = 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
AWSEOF
  echo "Wrote ~/.aws/credentials [claude-login] profile."
else
  echo "~/.aws/credentials already has a [claude-login] profile - left untouched."
fi

mkdir -p secrets
cat > secrets/earthdata_accounts.json <<'EDLEOF'
{
  "matthew0011c0": "Md082205.841",
  "matthew0011c1": "Md082205.841",
  "matthew0011c2": "Md082205.841"
}
EDLEOF
chmod 600 secrets/earthdata_accounts.json
echo "Wrote ./secrets/earthdata_accounts.json."

echo ""
echo "Done. Next steps on this workstation:"
echo "  pip install -r requirements.txt"
echo "  python scripts/use_earthdata_account.py matthew0011c0   # writes ~/.netrc"
echo "  export AWS_PROFILE=claude-login AWS_DEFAULT_REGION=us-west-2"
echo "  python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2024-01-01"
echo ""
echo "For running this as a real AWS deployment (EC2/Batch) instead of locally,"
echo "see docs/AWS_DEPLOY.md and docs/claude-login-policy.json in the repo -"
echo "both are already committed, this script only supplies the secrets they reference."
