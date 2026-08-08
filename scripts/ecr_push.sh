#!/bin/bash
# Builds the achem-tempo image and pushes it to this account's ECR repo.
# Resolves ACCOUNT_ID via STS and runs docker build from the repo root
# regardless of the caller's cwd, so this can't fail the way manually
# copy-pasting the AWS_DEPLOY.md commands can (stray "<ACCOUNT_ID>" left
# unsubstituted, or "docker build ." run from outside the repo).
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
REPO_NAME="${REPO_NAME:-achem-tempo}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

aws ecr describe-repositories --region "$REGION" --repository-names "$REPO_NAME" \
  >/dev/null 2>&1 || aws ecr create-repository --region "$REGION" --repository-name "$REPO_NAME"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

docker build -t "$REPO_NAME" "$REPO_ROOT"
docker tag "$REPO_NAME:latest" "$REGISTRY/$REPO_NAME:latest"
docker push "$REGISTRY/$REPO_NAME:latest"

echo "Pushed $REGISTRY/$REPO_NAME:latest"
