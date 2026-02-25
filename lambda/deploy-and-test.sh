#!/bin/bash
# Run after: aws sso login --profile bedrock

set -e
cd "$(dirname "$0")"

echo "1. Updating Lambda function..."
aws lambda update-function-code \
  --function-name bedrock-demo-processor \
  --zip-file fileb://function.zip \
  --region us-west-2 \
  --profile bedrock

echo ""
echo "2. Clearing DynamoDB table (bedrock-demo-loans)..."
aws dynamodb scan --table-name bedrock-demo-loans --projection-expression "loan_number" \
  --region us-west-2 --profile bedrock --output json | \
  jq -r '.Items[].loan_number.S' | while read -r ln; do
  echo "  Deleting loan_number=$ln"
  aws dynamodb delete-item --table-name bedrock-demo-loans --key "{\"loan_number\": {\"S\": \"$ln\"}}" \
    --region us-west-2 --profile bedrock
done

echo ""
echo "3. Uploading PDF..."
aws s3 cp ../bedrock_demo_contract_test_v2.pdf \
  "s3://griboullie-bedrock-demo/bedrock-demo/inbound/bedrock_demo_contract_test_v2.pdf" \
  --region us-west-2 --profile bedrock

echo ""
echo "Done. Check reports: s3://griboullie-bedrock-demo/bedrock-demo/reports/"
echo "Tail logs: aws logs tail /aws/lambda/bedrock-demo-processor --profile bedrock --region us-west-2"
