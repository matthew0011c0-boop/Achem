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
- At least one NASA Earthdata Login account (urs.earthdata.nasa.gov). See
  [Splitting across multiple Earthdata accounts](#splitting-across-multiple-earthdata-accounts)
  below for whether you actually need more than one.

Substitute your own values for `<ACCOUNT_ID>` and adjust `us-west-2` if
you deploy elsewhere.

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

```bash
aws secretsmanager create-secret --region us-west-2 \
  --name achem/earthdata/account-a \
  --secret-string '{"username":"YOUR_EDL_USERNAME","password":"YOUR_EDL_PASSWORD"}'
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
SECRET_ID=achem/earthdata/account-a
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
each pointed at a **different secret** (`achem/earthdata/account-a/b/c`)
and a **non-overlapping date range** - not the same range racing each
other. Since ranges don't overlap, there's no risk of two accounts
submitting duplicate Harmony jobs for the same month:

| Instance | Secret                        | `--start`    | `--end`      |
|----------|--------------------------------|--------------|--------------|
| A        | `achem/earthdata/account-a`   | 2023-08-01   | 2024-07-31   |
| B        | `achem/earthdata/account-b`   | 2024-08-01   | 2025-07-31   |
| C        | `achem/earthdata/account-c`   | 2025-08-01   | 2026-08-07   |

All three upload into the same bucket/prefix, so the result is identical
to one long sequential run - just three independent `user-data.sh` copies
with `SECRET_ID`/`START`/`END` changed, and three `run-instances` calls.

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
