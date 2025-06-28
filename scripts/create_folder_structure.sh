#!/bin/bash

echo "Setting up initial folder structure for Lambda-centric serverless architecture..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT" || exit 1

mkdir -p dist infra lambdas/voicenest_serverless

touch \
  lambdas/voicenest_serverless/handler.py \
  lambdas/voicenest_serverless/requirements.txt \
  infra/main.tf \
  buildspec.yml \
  scripts/package_lambdas.sh \
  scripts/generate_lambda_env_vars_from_ssm.py \
  scripts/requirements.txt

echo "boto3" >> scripts/requirements.txt

echo "Folder structure created successfully at $PROJECT_ROOT"
