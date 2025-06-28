#!/bin/bash
set -euo pipefail

LAMBDA_NAME="voicenest_serverless"
LAMBDA_FUNCTION_NAME="${LAMBDA_NAME}-lambda"
SRC_DIR="./lambdas/$LAMBDA_NAME"
DIST_DIR="./dist"
ZIP_NAME="voicenest_lambda.zip"
ZIP_PATH="$DIST_DIR/$ZIP_NAME"
LAMBDA_BUCKET="voicenest-serverless-lambda-deploy"

echo "[*] Cleaning dist folder..."
mkdir -p "$DIST_DIR"
rm -f "$ZIP_PATH"

echo "[*] Installing Lambda dependencies..."
rm -rf "$SRC_DIR/python"
pip install -r "$SRC_DIR/requirements.txt" -t "$SRC_DIR/python"

echo "[*] Creating deployment package..."
cd "$SRC_DIR"
zip -r "../../$ZIP_PATH" handler.py python/
cd - > /dev/null

echo "[*] Uploading Lambda zip to S3..."
aws s3 cp "$ZIP_PATH" "s3://${LAMBDA_BUCKET}/${ZIP_NAME}"

echo "[*] Updating Lambda function..."
if aws lambda update-function-code \
  --function-name "$LAMBDA_FUNCTION_NAME" \
  --s3-bucket "$LAMBDA_BUCKET" \
  --s3-key "$ZIP_NAME"; then
  echo "[✓] Lambda function updated."
else
  echo "[!] Lambda function update failed."
fi

echo "[*] Generating Lambda environment variables from SSM..."
rm -rf scripts/package
pip install -r scripts/requirements.txt --target scripts/package
PYTHONPATH=scripts/package python3 scripts/generate_lambda_env_vars_from_ssm.py > infra/lambda_env_vars.tf.json

echo "[*] Cleaning up temporary directories..."
rm -rf "$SRC_DIR/python"
rm -rf scripts/package

echo "[✓] Lambda packaged, uploaded to S3, and env vars written to infra/lambda_env_vars.tf.json"
