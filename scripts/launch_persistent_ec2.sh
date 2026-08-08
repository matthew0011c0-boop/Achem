#!/bin/bash
# Launches a long-lived EC2 instance running the achem-tempo container
# detached (docker run -d --restart unless-stopped), so the download run
# keeps going after you close CloudShell/your browser. Unlike
# docs/AWS_DEPLOY.md's self-terminating instance, this one stays up until
# you explicitly terminate it - reconnect anytime with SSM Session
# Manager (no SSH key needed):
#
#   aws ssm start-session --target <instance-id> --region us-west-2
#   sudo docker logs -f --tail 100 achem-tempo   # inside the session
#
# Requires: the image already pushed to ECR (run scripts/ecr_push.sh
# first) and an Earthdata secret already created in Secrets Manager
# (see docs/AWS_DEPLOY.md step 2) - only its "password" field is used; that
# one password is shared across every account in ACCOUNTS.
#
# ACCOUNTS defaults to all three known Earthdata logins, all run
# concurrently inside this one container (see
# scripts/download_tempo_no2_co_multi.py) - set ACCOUNTS=matthew0011c0 for
# the old single-account behavior.
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
REPO_NAME="${REPO_NAME:-achem-tempo}"
SECRET_ID="${SECRET_ID:-achem/earthdata/matthew0011c0}"
ACCOUNTS="${ACCOUNTS:-matthew0011c0,matthew0011c1,matthew0011c2}"
START="${START:-2023-08-01}"
END="${END:-$(date +%F)}"
WORKERS="${WORKERS:-4}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"
ROLE_NAME="achem-tempo-role"
PROFILE_NAME="achem-tempo-profile"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "Ensuring IAM role/instance profile exist..."
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]
  }' >/dev/null
fi

aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name achem-tempo-policy --policy-document "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::matt-achem-bucket2",
     "Condition": {"StringLike": {"s3:prefix": "tempo_no2_co/*"}}},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::matt-achem-bucket2/tempo_no2_co/*"},
    {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:achem/earthdata/*"},
    {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
     "Resource": "arn:aws:ecr:${REGION}:${ACCOUNT_ID}:repository/${REPO_NAME}"}
  ]
}
POLICY
)" >/dev/null

# SSM Session Manager access - this is what lets you reconnect to the instance
# from any terminal without an SSH key or open inbound port.
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null

if ! aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE_NAME" --role-name "$ROLE_NAME"
  echo "Waiting for the new instance profile to propagate..."
  sleep 10
fi

VPC_ID="$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
SUBNET_ID="$(aws ec2 describe-subnets --region "$REGION" --filters Name=vpc-id,Values="$VPC_ID" --query 'Subnets[0].SubnetId' --output text)"
SG_ID="$(aws ec2 describe-security-groups --region "$REGION" --filters Name=vpc-id,Values="$VPC_ID" Name=group-name,Values=default --query 'SecurityGroups[0].GroupId' --output text)"
AMI_ID="$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)"

USER_DATA=$(cat <<EOF
#!/bin/bash
set -eu
exec > /var/log/achem-download.log 2>&1
dnf install -y docker
systemctl enable --now docker

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

CREDS=\$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --region "$REGION" --query SecretString --output text)
EARTHDATA_PASSWORD=\$(python3 -c "import json,sys;print(json.load(sys.stdin)['password'])" <<< "\$CREDS")

docker run -d --name achem-tempo --restart unless-stopped \
  -e EARTHDATA_ACCOUNTS="$ACCOUNTS" -e EARTHDATA_PASSWORD="\$EARTHDATA_PASSWORD" \
  "$REGISTRY/$REPO_NAME:latest" --start "$START" --end "$END" --workers "$WORKERS"
EOF
)

echo "Launching persistent instance..."
INSTANCE_ID="$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --iam-instance-profile Name="$PROFILE_NAME" \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
  --user-data "$USER_DATA" \
  --security-group-ids "$SG_ID" --subnet-id "$SUBNET_ID" \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=achem-tempo-persistent}]' \
  --query 'Instances[0].InstanceId' --output text)"

echo ""
echo "Instance: $INSTANCE_ID (region $REGION)"
echo "It stays running (and the container restarts if it crashes) until you terminate it:"
echo "  aws ec2 terminate-instances --region $REGION --instance-ids $INSTANCE_ID"
echo ""
echo "Reconnect anytime, from CloudShell or any terminal with AWS creds, to watch progress:"
echo "  aws ssm start-session --target $INSTANCE_ID --region $REGION"
echo "  # then inside the session:"
echo "  sudo docker logs -f --tail 100 achem-tempo"
