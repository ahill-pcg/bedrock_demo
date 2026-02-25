#!/bin/bash
# Creates the bedrock-demo-configs DynamoDB table and seeds default configs.
# Run after: aws sso login --profile bedrock

set -e
cd "$(dirname "$0")"

TABLE_NAME="bedrock-demo-configs"
REGION="us-west-2"
PROFILE="bedrock"

echo "1. Creating DynamoDB table: $TABLE_NAME..."
aws dynamodb create-table \
  --table-name "$TABLE_NAME" \
  --attribute-definitions AttributeName=config_id,AttributeType=S \
  --key-schema AttributeName=config_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION" \
  --profile "$PROFILE" \
  2>/dev/null || echo "  (Table may already exist)"

echo ""
echo "2. Seeding configs..."

# Solution One (sone) - Full field list from 200606750-3 Sample Unfilled (Texas Motor Vehicle RIC)
# For full 105-field config, run: python3 update-sone-config.py (requires boto3) or ./update-sone-config.sh
aws dynamodb put-item \
  --table-name "$TABLE_NAME" \
  --item '{
    "config_id": {"S": "sone"},
    "extraction_fields": {
      "L": [
        {"S": "app_number"},
        {"S": "loan_number"},
        {"S": "borrower_name"},
        {"S": "dealer_name"},
        {"S": "contract_amount"},
        {"S": "first_payment_date"},
        {"S": "state"},
        {"S": "vin"},
        {"S": "license_number"},
        {"S": "insurance_company"},
        {"S": "effective_date"},
        {"S": "insurance_coverages"},
        {"S": "insurance_term"},
        {"S": "physical_damage_premium"},
        {"S": "trade_in_year"},
        {"S": "trade_in_make"},
        {"S": "trade_in_model"},
        {"S": "documentary_fee"},
        {"S": "cash_price"}
      ]
    }
  }' \
  --region "$REGION" \
  --profile "$PROFILE"

# Bedrock demo default (bedrock) - for existing test files
aws dynamodb put-item \
  --table-name "$TABLE_NAME" \
  --item '{
    "config_id": {"S": "bedrock"},
    "extraction_fields": {
      "L": [
        {"S": "loan_number"},
        {"S": "borrower_name"},
        {"S": "dealer_name"},
        {"S": "contract_amount"},
        {"S": "first_payment_date"},
        {"S": "state"}
      ]
    }
  }' \
  --region "$REGION" \
  --profile "$PROFILE"

echo ""
echo "Done. Table: $TABLE_NAME"
echo "Add Lambda permission to read from this table (dynamodb:GetItem)."
