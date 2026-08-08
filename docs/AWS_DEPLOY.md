# Deploying to AWS

This is a **run-to-completion backfill**, not a long-lived service: it
processes a fixed date range and stops. Combined with the bucket-first
resume logic, that means the cheapest and simplest way to run it is
throwaway compute - a container that starts, works through its date range
(skipping anything already in the bucket), uploads results, and
terminates. No cluster, no scheduler, no always-on server.

**Region: use `us-west-2`.** NASA's Earthdata Cloud and the Harmony
subsetting service both run there; running your compute (and ideally the
S3 bucket) in the same region cuts request latency noticeably versus
running from elsewhere.

## 0. Prerequisites

- An AWS account with permission to create IAM roles, EC2 instances, an
  ECR repo, and Secrets Manager secrets.
- Docker installed wherever you build the image.
- At least one NASA Earthdata Login account (urs.earthdata.nasa.gov). Three
  are available here - `matthew0011c0`, `matthew0011c1`, `matthew0011c2` -
  see [Splitting across multiple Earthdata accounts](#splitting-across-multiple-earthdata-accounts)
  below for whether it's actually worth using more than one.

Substitute your own values for `<ACCOUNT_ID>` and adjust `us-west-2` if
you deploy elsewhere.

**IAM user for automation**: if you're running these steps via a
dedicated IAM user (access key + secret, not your root/console login),
that user needs permissions attached before any of this works - an IAM
user with no policy attached can authenticate (`sts:get-caller-identity`
always succeeds) but every real action will be `AccessDenied`. See
[`claude-login-policy.json`](claude-login-policy.json) for the exact
least-privilege policy this project needs (S3 scoped to
`matt-achem-bucket2/tempo_no2_co/*`, IAM scoped to the two
`achem-tempo-*` resources this guide creates, ECR scoped to the
`achem-tempo` repo, Secrets Manager scoped to `achem/earthdata/*`; EC2
and the initial ECR/SSM lookups can't be scoped to not-yet-existing
resources so those stay account-wide). Attach it with:

```bash
aws iam put-user-policy --user-name <IAM_USER_NAME> \
  --policy-name achem-tempo-deploy \
  --policy-document file://docs/claude-login-policy.json
```

This has to be run by someone with IAM admin rights on the account - an
IAM user cannot grant itself permissions it doesn't already have.

## 1. Build and push the image to ECR

```bash
aws ecr create-repository --repository-name achem-tempo --region us-west-2

aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com

docker build -t achem-tempo .
docker tag achem-tempo:latest <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/achem-tempo:latest
docker push <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/achem-tempo:latest
```

## 2. Store Earthdata credentials in Secrets Manager

The container's entrypoint (`docker-entrypoint.sh`) writes `~/.netrc` from
`EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD` env vars at startup - nothing
NASA-related is baked into the image.

Three Earthdata Login accounts are in play: `matthew0011c0`,
`matthew0011c1`, `matthew0011c2`. One secret each, never typed into chat -
run these yourself with the real passwords filled in:

```bash
for user in matthew0011c0 matthew0011c1 matthew0011c2; do
  aws secretsmanager create-secret --region us-west-2 \
    --name "achem/earthdata/${user}" \
    --secret-string "{\"username\":\"${user}\",\"password\":\"REPLACE_ME\"}"
done
```

## 3. IAM role for the instance

Trust policy (`trust-policy.json`):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
```

Permissions policy (`achem-tempo-policy.json`) - scoped to just this
bucket's prefix, this one secret, and pulling this one ECR repo:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::matt-achem-bucket2",
      "Condition": {"StringLike": {"s3:prefix": "tempo_no2_co/*"}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::matt-achem-bucket2/tempo_no2_co/*"
    },
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:us-west-2:<ACCOUNT_ID>:secret:achem/earthdata/*"
    },
    {
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
      "Resource": "arn:aws:ecr:us-west-2:<ACCOUNT_ID>:repository/achem-tempo"
    }
  ]
}
```

```bash
aws iam create-role --role-name achem-tempo-role \
  --assume-role-policy-document file://trust-policy.json

aws iam put-role-policy --role-name achem-tempo-role \
  --policy-name achem-tempo-policy --policy-document file://achem-tempo-policy.json

aws iam create-instance-profile --instance-profile-name achem-tempo-profile
aws iam add-role-to-instance-profile \
  --instance-profile-name achem-tempo-profile --role-name achem-tempo-role
```

## 4. Run it: a self-terminating EC2 instance

`user-data.sh`:

```bash
#!/bin/bash
set -eu
exec > /var/log/achem-download.log 2>&1

dnf install -y docker
systemctl start docker

REGION=us-west-2
ACCOUNT_ID=<ACCOUNT_ID>
SECRET_ID=achem/earthdata/matthew0011c0
START=2023-08-01
END=2026-08-07

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

CREDS=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --region "$REGION" \
  --query SecretString --output text)
export EARTHDATA_USERNAME=$(python3 -c "import json,sys;print(json.load(sys.stdin)['username'])" <<< "$CREDS")
export EARTHDATA_PASSWORD=$(python3 -c "import json,sys;print(json.load(sys.stdin)['password'])" <<< "$CREDS")

docker run --rm -e EARTHDATA_USERNAME -e EARTHDATA_PASSWORD \
  "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/achem-tempo:latest" \
  --start "$START" --end "$END" --workers 4

shutdown -h now
```

**IMDSv2 gotcha**: `boto3` inside the container picks up S3 permissions
from the instance's IAM role via the EC2 instance metadata service, but a
container is an extra network hop and the default metadata hop limit is
1 - it'll fail silently into `NoCredentialsError` unless you raise it.
That's the `--metadata-options` flag below.

```bash
AMI_ID=$(aws ssm get-parameter --region us-west-2 \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)

aws ec2 run-instances --region us-west-2 \
  --image-id "$AMI_ID" \
  --instance-type t3.medium \
  --iam-instance-profile Name=achem-tempo-profile \
  --instance-initiated-shutdown-behavior terminate \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
  --user-data file://user-data.sh \
  --security-group-ids <SG_ID> --subnet-id <SUBNET_ID> \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=achem-tempo-a}]'
```

The security group only needs outbound 443 (NASA Earthdata/Harmony, ECR,
Secrets Manager, S3); no inbound rules are needed at all. The instance
terminates itself when the script exits (success or failure), so you only
pay for the run.

**Progress/monitoring**: the S3 bucket is the ground truth, so
`aws s3 ls s3://matt-achem-bucket2/tempo_no2_co/ --recursive | wc -l` from
anywhere tells you how far it's gotten. `/var/log/achem-download.log` on
the instance (via SSM Session Manager, or a mounted EBS volume you inspect
after termination) has the full per-month output. If a run dies partway
through, just launch it again with the same `--start`/`--end` - the bucket
check picks up exactly where it left off.

## Splitting across multiple Earthdata accounts

Whether this actually helps depends on what's limiting you:

- If you're only ever running `--workers 4-6` on one account, you're
  unlikely to hit any per-account Harmony throttling in the first place -
  try one account before adding this complexity.
- Harmony's concurrent-job limits are enforced **per Earthdata account**,
  so running under 3 accounts genuinely raises your concurrent-job ceiling
  by ~3x. It does *not* necessarily give you 3x wall-clock throughput,
  though - Harmony's backend compute is shared infrastructure, so if
  *that's* the bottleneck (rather than a per-account cap), more accounts
  won't help.
- Keep total load reasonable regardless - this is NASA's free public
  service, and stacking accounts specifically to push past a fair-use
  limit is worth avoiding regardless of whether it's technically possible.

If you do want to split it: run three of the instances above in parallel,
each pointed at a **different secret** and a **non-overlapping date
range** - not the same range racing each other. Since ranges don't
overlap, there's no risk of two accounts submitting duplicate Harmony jobs
for the same month:

| Instance | Secret                              | `--start`    | `--end`      |
|----------|--------------------------------------|--------------|--------------|
| A        | `achem/earthdata/matthew0011c0`     | 2023-08-01   | 2024-07-31   |
| B        | `achem/earthdata/matthew0011c1`     | 2024-08-01   | 2025-07-31   |
| C        | `achem/earthdata/matthew0011c2`     | 2025-08-01   | 2026-08-07   |

All three upload into the same bucket/prefix, so the result is identical
to one long sequential run - just three independent `user-data.sh` copies
with `SECRET_ID`/`START`/`END` changed, and three `run-instances` calls.

### Actually measuring "is it faster" before committing to the full split

Don't run the full ~3-year backfill three times over just to time it -
run a small, fair, apples-to-apples comparison first:

1. **Baseline** - one account, three recent months, sequential (normal
   single-account behavior): `SECRET_ID=achem/earthdata/matthew0011c0`,
   `--start 2025-06-01 --end 2025-08-31 --workers 4`. Note the wall-clock
   time from launch to instance self-termination.
2. **3-way split** - the *same three months*, one per account, launched
   at the same time: instance A does `matthew0011c0` /
   `--start 2025-06-01 --end 2025-06-30`, instance B does `matthew0011c1` /
   `--start 2025-07-01 --end 2025-07-31`, instance C does `matthew0011c2` /
   `--start 2025-08-01 --end 2025-08-31`. Note the wall-clock time until
   the *last* of the three terminates.
3. Compare. If (2) finishes in roughly a third of (1)'s time, the
   per-account cap was your bottleneck and the full 3-way split (table
   above) is worth doing. If (2) isn't much faster than (1), Harmony's
   shared backend is the limiter and more accounts won't help - fall back
   to a single account for the full run.

This costs about the same as one month's worth of downloading either way
(same 3 months of data get pulled once, just arranged differently), so
it's a cheap real answer instead of a guess.

**Gotcha if you shrink the windows below a full month** (e.g. testing with
single days instead of whole months, to run a cheaper/faster benchmark):
`bucket_has_month()` checks the bucket at **month** granularity
(`tempo_no2_co/<year>/<month>/`), not per-day. If two of your test windows
fall in the *same* calendar month - even on different days, even in
different jobs - whichever one uploads first will make every other
same-month job see "this month already has files" and skip without doing
any real work, silently invalidating the comparison. Confirmed this the
hard way: a 9-job test using 9 different days *within the same month*
finished in 10 seconds because job 1's upload made jobs 2-9 immediately
short-circuit. Fix is the one already used above - give every window
(baseline included) its own distinct calendar month.

## Alternative: AWS Batch

For something more managed (automatic retry on failure, no manual instance
bookkeeping), define a Fargate compute environment + job queue + job
definition referencing the same ECR image, pass `EARTHDATA_USERNAME`/
`EARTHDATA_PASSWORD` via the job definition's `secrets` field (pointing at
the same Secrets Manager secrets) instead of user-data, and `submit-job`
once per date-range/account split instead of `run-instances`. The
container and IAM policy above are unchanged either way - only how it gets
launched differs. Worth doing if this becomes a recurring job rather than
a one-time backfill; overkill for a single run.
