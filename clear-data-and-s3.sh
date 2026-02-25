#!/bin/bash
# Clear bedrock-demo-loans DynamoDB table and remove bedrock-demo files from S3.
# Run after: aws sso login --profile bedrock

set -e
cd "$(dirname "$0")"

PROFILE="${AWS_PROFILE:-bedrock}"
REGION="${AWS_REGION:-us-west-2}"
BUCKET="griboullie-bedrock-demo"
PREFIX="bedrock-demo"

echo "1. Clearing DynamoDB table (bedrock-demo-loans)..."
SCAN_OUT=$(aws dynamodb scan --table-name bedrock-demo-loans --projection-expression "loan_number" \
  --region "$REGION" --profile "$PROFILE" --output json) || { echo "  AWS error (run: aws sso login --profile $PROFILE)"; exit 1; }
for ln in $(echo "$SCAN_OUT" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print('\n'.join(i.get('loan_number',{}).get('S','') for i in d.get('Items',[]) if i.get('loan_number',{}).get('S')))
except json.JSONDecodeError:
    sys.exit(1)
"); do
  [ -n "$ln" ] || continue
  echo "  Deleting loan_number=$ln"
  aws dynamodb delete-item --table-name bedrock-demo-loans \
    --key "{\"loan_number\": {\"S\": \"$ln\"}}" \
    --region "$REGION" --profile "$PROFILE"
done
echo "  Done."

echo ""
echo "2. Removing S3 files under s3://$BUCKET/$PREFIX/..."
aws s3 rm "s3://$BUCKET/$PREFIX/" --recursive --region "$REGION" --profile "$PROFILE"
echo "  Done."

echo ""
echo "Data table cleared and S3 files removed."
