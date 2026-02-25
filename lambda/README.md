# Contract Processor Lambda

This Lambda function processes contract PDFs uploaded to S3: it extracts key fields using **Amazon Bedrock (Claude vision)** for OCR and structured extraction, compares them to records in **DynamoDB**, and writes an HTML certification report back to S3. If no loan exists in the database, it assigns a new loan number and inserts the extracted data.

## Trigger

- **Event:** S3 `ObjectCreated:Put` on the inbound prefix.
- **Bucket/path:** `s3://<bucket>/bedrock-demo/inbound/*.pdf`
- When a PDF is uploaded there, the function runs automatically.

## Filename Convention & Configuration

Each PDF must have a **config prefix** in its filename to identify which extraction configuration to use:

- **Format:** `{config_id}_{filename}.pdf` (e.g. `sone_RV_Retail_Contract.pdf`, `bedrock_test.pdf`)
- The part before the first underscore is the `config_id`, used to look up extraction fields from the `bedrock-demo-configs` DynamoDB table.
- **Exceptions:** If the filename has no prefix (no underscore) or the config is not found, the document is copied to `bedrock-demo/exceptions/` and an exception report is written there. The main processing pipeline is skipped.

## Processing Flow

1. **Read document**  
   The function reads the PDF from S3 (using the object key from the event, URL-decoded so keys with spaces work).

2. **OCR and structured extraction (Bedrock)**  
   Converts the PDF to images (PyMuPDF) and sends them to Claude via Bedrock with a vision prompt. Claude reads the document and returns **strict JSON** with:
   - `loan_number`
   - `borrower_name`
   - `dealer_name`
   - `contract_amount`
   - `first_payment_date`
   - `state`

4. **DynamoDB lookup or insert**
   - **No loan number in document**  
     Gets the next loan number from the auto-increment counter, builds a new item from the extracted data, inserts it, and generates a **“NEW INSERT”** report (no comparison).
   - **Loan number present but not in table**  
     Same: next loan number, insert, then **“NEW INSERT”** report.
   - **Loan found in table**  
     Loads the existing item and continues to comparison.

5. **Comparison (existing loans only)**  
   For existing loans, Bedrock compares extracted JSON to the DB row. The prompt tells the model to treat as the same value when only formatting differs, e.g.:
   - **Numbers/currency:** `$24,500.00`, `24500`, `24,500` → same.
   - **Dates:** `2025-01-01`, `01-01-2025`, `01-JAN-2025` → same.
   - **Strings:** whitespace, case, empty/N/A placeholders, phone digits-only.

6. **Report**  
   Generates an HTML report and uploads it to the same bucket under the reports prefix, e.g.  
   `bedrock-demo/reports/<original-filename>-report.html`.

## Report Types

- **Exception**  
  Yellow status: document copied to `exceptions/` with `-exception.html` report. Occurs when the filename has no config prefix or the config is not found in DynamoDB.

- **Certification (match)**  
  Green status: “CERTIFIED - MATCH”. Table shows extracted vs DB and ✔/✖ per field.

- **Certification (mismatch)**  
  Red status: “EXCEPTION - REVIEW REQUIRED”. Table highlights differing fields.

- **New insert**  
  Blue status: “NEW INSERT - No comparison made”. Shows extracted data and the inserted row (with assigned loan number). Footer explains that the record was not in the database and was inserted; no comparison was performed.

## Auto-Increment (New Loans)

- The function uses a **reserved item** in the same DynamoDB table for the sequence:
  - **Partition key:** `loan_number = "_sequence"`.
  - **Attribute:** `next_loan_number` (number), incremented with `UpdateItem`.
- Do not use `_sequence` as a real loan number. On first use, if that item is missing, the function creates it and uses `1` as the first loan number.

## AWS Resources

| Resource        | Purpose |
|----------------|--------|
| S3 (bucket)    | Inbound PDFs; output HTML reports; `exceptions/` for unprocessable documents |
| Bedrock (Claude) | Vision OCR, JSON extraction, and comparison |
| DynamoDB (bedrock-demo-loans) | Loan records; partition key `loan_number`; also `_sequence` for counter |
| DynamoDB (bedrock-demo-configs) | Extraction configs; partition key `config_id`; `extraction_fields` (list of field names) |

## Configuration

- **Loans table:** `TABLE_NAME = "bedrock-demo-loans"` (DynamoDB).
- **Config table:** `CONFIG_TABLE_NAME = "bedrock-demo-configs"` – stores extraction field lists per `config_id`.
- **Model:** `MODEL_ID` (Bedrock Claude model used for extraction and comparison).
- **Logo:** `LOGO_URL` (optional; used in the HTML report).

To create the config table and seed default configs (`sone`, `bedrock`):

```bash
./create-config-table.sh
```

To add a new config (e.g. `acme` with custom fields):

```bash
aws dynamodb put-item --table-name bedrock-demo-configs \
  --item '{"config_id":{"S":"acme"},"extraction_fields":{"L":[{"S":"loan_number"},{"S":"borrower_name"},{"S":"contract_amount"}]}}' \
  --region us-west-2 --profile bedrock
```

Filenames must then use the prefix: `acme_MyContract.pdf`.

## Deployment

The function uses **PyMuPDF** for PDF-to-image conversion. Install dependencies and bundle them with the handler:

```bash
cd lambda
pip install -r requirements.txt -t .
zip -r function.zip lambda_function.py fitz pymupdf* fitz/*
aws lambda update-function-code \
  --function-name bedrock-demo-processor \
  --zip-file fileb://function.zip \
  --region us-west-2 \
  --profile <your-profile>
```

Or, for a simpler layout:

```bash
cd lambda
pip install pymupdf -t .
zip -r function.zip .
aws lambda update-function-code --function-name bedrock-demo-processor \
  --zip-file fileb://function.zip --region us-west-2 --profile <your-profile>
```

## IAM (Lambda Role)

The role needs at least:

- **S3:** `GetObject`, `PutObject`, `ListBucket` on the bucket/prefix.
- **Bedrock:** `InvokeModel` (or equivalent) for the configured model.
- **DynamoDB:** `GetItem`, `PutItem`, `UpdateItem` on the loans table; `GetItem` on the config table.
