#!/bin/bash
# Update sone config with all fields from 200606750-3 form
# Run: aws sso login --profile bedrock first

set -e
cd "$(dirname "$0")"

FIELDS='app_number loan_number contract_date buyer seller_creditor buyer_address seller_address buyer_city buyer_state buyer_zip seller_city seller_state seller_zip buyer_phone seller_phone stock_no year make model vin license_number use_for_which_purchased trade_in_year trade_in_make trade_in_model trade_in_vin trade_in_license_no annual_percentage_rate finance_charge amount_financed total_of_payments total_sale_price down_payment number_of_payments amount_of_payments when_payments_due cash_price contract_amount first_payment_date gross_trade_in payoff_by_seller net_trade_in cash_down mfrs_rebate total_downpayment unpaid_balance_cash_price net_trade_in_payoff cost_physical_damage_insurance cost_optional_coverages_physical_damage cost_optional_credit_insurance cost_other_insurance official_fees dealer_inventory_tax sales_tax documentary_fee govt_license_fees govt_title_fee govt_inspection_fees deputy_service_fee collision_premium collision_term comprehensive_premium comprehensive_term fire_theft_premium fire_theft_term other_physical_damage_premium towing_labor_premium rental_reimbursement_premium physical_damage_buyer_signature_present physical_damage_signature_date gap_premium gap_term invol_unemployment_premium liability_premium liability_property_damage liability_per_person liability_per_accident optional_coverages_buyer_signature_present optional_coverages_signature_date credit_life_one_buyer_premium credit_disability_one_buyer_premium credit_life_both_premium credit_disability_both_premium credit_life_term credit_disability_term optional_credit_life_buyer_signature_present optional_credit_life_signature_date optional_credit_life_co_buyer_signature_present optional_credit_life_co_buyer_date liability_insurance_option liability_buyer_signature_present main_buyer_signature_present main_buyer_signature_date main_seller_signature_present main_seller_signature_date main_co_buyer_signature_present main_co_buyer_signature_date borrower_name dealer_name state insurance_company effective_date insurance_coverages insurance_term physical_damage_premium'

# Build DynamoDB L attribute
ITEM_L=""
for f in $FIELDS; do
  ITEM_L="${ITEM_L}{\"S\": \"$f\"},"
done
ITEM_L="[${ITEM_L%,}]"

aws dynamodb put-item \
  --table-name bedrock-demo-configs \
  --item "{\"config_id\": {\"S\": \"sone\"}, \"extraction_fields\": {\"L\": $ITEM_L}}" \
  --region us-west-2 \
  --profile bedrock

echo "Updated sone config"
