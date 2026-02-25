import base64
import json
import os
import re
import boto3
from datetime import datetime
from decimal import Decimal
from urllib.parse import unquote_plus

import fitz

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime", region_name="us-west-2")
dynamodb = boto3.resource("dynamodb")

TABLE_NAME = "bedrock-demo-loans"
CONFIG_TABLE_NAME = "bedrock-demo-configs"
MODEL_ID = "anthropic.claude-3-sonnet-20240229-v1:0"
SEQUENCE_KEY = "_sequence"  # reserved loan_number for auto-increment counter

# Update this if your logo has a direct image URL
LOGO_URL = "https://gribouille-le.com/wp-content/uploads/logo.png"

# Aliases that map to a single canonical key (avoids duplicate fields like vin vs vehicle_identification_number)
FIELD_ALIASES_TO_CANONICAL = {
    "vehicle_identification_number": "vin",
    "vehicle_id": "vin",
    "vin_number": "vin",
    "app_no": "app_number",
    "application_number": "app_number",
}


def extract_config_prefix_from_key(key):
    """
    Extract config prefix from S3 object key. Expected filename format: {config_id}_{rest}.pdf
    Returns (config_id, True) if prefix found, or (None, False) if no identifiable prefix.
    """
    filename = os.path.basename(key)
    stem, _ = os.path.splitext(filename)
    parts = stem.split("_", 1)
    if len(parts) < 2 or not parts[0]:
        return None, False
    return parts[0].lower(), True


def get_config(config_id):
    """Load extraction config from DynamoDB by config_id. Returns config dict or None."""
    table = dynamodb.Table(CONFIG_TABLE_NAME)
    resp = table.get_item(Key={"config_id": config_id})
    if "Item" not in resp:
        return None
    item = resp["Item"]
    fields = item.get("extraction_fields")
    if not fields:
        return None
    # DynamoDB returns list of {"S": "field_name"} or similar
    extraction_fields = [f["S"] if isinstance(f, dict) and "S" in f else str(f) for f in fields]
    return {"extraction_fields": extraction_fields}


def handle_exception(bucket, key, reason, document_bytes=None):
    """
    Copy PDF to exceptions folder and write an exception report.
    reason: human-readable string (e.g., "No identifiable config prefix in filename")
    """
    exception_key = key.replace("inbound/", "exceptions/")
    report_key = exception_key.replace(".pdf", "-exception.html")

    html = generate_exception_report(key, reason)

    # Copy PDF to exceptions
    if document_bytes is not None:
        s3.put_object(Bucket=bucket, Key=exception_key, Body=document_bytes, ContentType="application/pdf")
    else:
        s3.copy_object(
            CopySource={"Bucket": bucket, "Key": key},
            Bucket=bucket,
            Key=exception_key,
            ContentType="application/pdf",
        )

    s3.put_object(Bucket=bucket, Key=report_key, Body=html, ContentType="text/html")
    print("EXCEPTION:", reason, "->", report_key)


CURRENCY_PREMIUM_FIELDS = frozenset([
    "physical_damage_premium", "collision_premium", "comprehensive_premium",
    "fire_theft_premium", "other_physical_damage_premium", "towing_labor_premium",
    "rental_reimbursement_premium", "gap_premium", "invol_unemployment_premium",
    "liability_premium", "credit_life_one_buyer_premium", "credit_disability_one_buyer_premium",
    "credit_life_both_premium", "credit_disability_both_premium",
])


def _sanitize_currency_fields(data):
    """If a currency/premium field contains no digits (e.g. a name), clear it."""
    if not data:
        return data
    for key in CURRENCY_PREMIUM_FIELDS:
        val = data.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if s and not re.search(r"\d", s):
            data[key] = None
    return data


def _has_value(val):
    """Return True if field has a meaningful value (used for signature trigger logic)."""
    if val is None:
        return False
    if isinstance(val, (list, dict)):
        return len(val) > 0
    s = str(val).strip().lower()
    if s in ("", "none", "null", "n/a", "na", "0", "0.0"):
        return False
    return True


def compute_sone_signature_validation(extracted_data):
    """
    Compute signature validation for sone (Texas RIC 200606750-3). Returns list of
    {section, required, present, date_required, date_present, note}. Missing required = red in report.
    """
    def get(k):
        return extracted_data.get(k)

    def present(k):
        v = get(k)
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("", "none", "null", "false", "no", "0", "n/a", "na"):
                return False
            if s in ("true", "yes", "1", "x", "✓", "✔"):
                return True
            return bool(s) and any(c.isalpha() for c in s)
        return bool(v)

    items = []

    # 1. Optional Credit Life: if any checkbox/premium filled, buyer sig + date required; co-buyer if "both"
    credit_life_trigger = any(_has_value(get(k)) for k in [
        "credit_life_one_buyer_premium", "credit_disability_one_buyer_premium",
        "credit_life_both_premium", "credit_disability_both_premium"
    ])
    if credit_life_trigger:
        date_val = get("optional_credit_life_signature_date")
        items.append({
            "section": "Model Clause – Optional Credit Life",
            "required": True,
            "present": present("optional_credit_life_buyer_signature_present"),
            "date_required": True,
            "date_present": _has_value(date_val),
            "note": "Buyer signature and date required when any credit life/disability box is checked",
        })
        if _has_value(get("credit_life_both_premium")) or _has_value(get("credit_disability_both_premium")):
            items.append({
                "section": "Model Clause – Optional Credit Life (Co-Buyer)",
                "required": True,
                "present": present("optional_credit_life_co_buyer_signature_present"),
                "date_required": True,
                "date_present": _has_value(get("optional_credit_life_co_buyer_date")),
                "note": "Co-buyer signature and date required when 'both buyers' is selected",
            })

    # 2. Liability Insurance: buyer signature always required
    items.append({
        "section": "Liability Insurance",
        "required": True,
        "present": present("liability_buyer_signature_present"),
        "date_required": False,
        "date_present": None,
        "note": "Buyer signature required",
    })

    # 3. Physical Damage: if any info filled, buyer sig + date required
    pd_trigger = any(_has_value(get(k)) for k in [
        "collision_premium", "comprehensive_premium", "fire_theft_premium",
        "other_physical_damage_premium", "towing_labor_premium", "rental_reimbursement_premium"
    ])
    if pd_trigger:
        items.append({
            "section": "Model Clause – Physical Damage Insurance",
            "required": True,
            "present": present("physical_damage_buyer_signature_present"),
            "date_required": True,
            "date_present": _has_value(get("physical_damage_signature_date")),
            "note": "Buyer signature and date required when physical damage info is filled",
        })

    # 4. Optional Coverages (GAP, etc): if any info filled, same rules
    oc_trigger = any(_has_value(get(k)) for k in [
        "gap_premium", "invol_unemployment_premium", "liability_premium",
        "liability_property_damage", "liability_per_person", "liability_per_accident"
    ])
    if oc_trigger:
        items.append({
            "section": "Model Clause – Optional Insurance Coverages",
            "required": True,
            "present": present("optional_coverages_buyer_signature_present"),
            "date_required": True,
            "date_present": _has_value(get("optional_coverages_signature_date")),
            "note": "Buyer signature and date required when optional coverage info is filled",
        })

    return items


def generate_exception_report(original_key, reason):
    """Generate HTML report for documents that could not be processed."""
    filename = original_key.split("/")[-1] if "/" in original_key else original_key
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""
    <html>
    <head>
        <title>Processing Exception</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f6f8; }}
            .summary {{ padding: 20px; background-color: #fff3cd; border-left: 6px solid #ffc107; margin-bottom: 30px; }}
            .logo {{ margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class="logo"><img src="{LOGO_URL}" height="60"></div>
        <h1>Processing Exception</h1>
        <div class="summary">
            <strong>File:</strong> {filename}<br>
            <strong>Reason:</strong> {reason}<br>
            <strong>Timestamp:</strong> {now}
        </div>
        <p>This document was not processed. Ensure the filename has a valid config prefix (e.g. <code>sone_</code> for Solution One).</p>
    </body>
    </html>
    """


def normalize_extracted_keys(extracted_data):
    """Merge synonymous keys into the canonical key (e.g. vehicle_identification_number → vin)."""
    if not extracted_data:
        return extracted_data
    result = {}
    for key, value in extracted_data.items():
        canonical = FIELD_ALIASES_TO_CANONICAL.get(key, key)
        if canonical not in result:
            result[canonical] = value
        elif value not in (None, "") and result[canonical] in (None, ""):
            result[canonical] = value
    return result


def get_next_loan_number(table):
    """Atomically get next loan number. Ensures counter item exists on first use."""
    try:
        resp = table.update_item(
            Key={"loan_number": SEQUENCE_KEY},
            UpdateExpression="SET next_loan_number = if_not_exists(next_loan_number, :start) + :inc",
            ExpressionAttributeValues={":start": 0, ":inc": 1},
            ReturnValues="UPDATED_NEW",
        )
        return str(resp["Attributes"]["next_loan_number"])
    except Exception:
        # Counter item may not exist; create it and use 1
        table.put_item(Item={"loan_number": SEQUENCE_KEY, "next_loan_number": 1})
        return "1"


def _normalize_value_for_dynamodb(key, val):
    """Convert extracted value to a DynamoDB-safe type (no None, use Decimal for numbers)."""
    if val is None:
        return "" if key != "contract_amount" else Decimal("0")
    if key == "contract_amount":
        if isinstance(val, str):
            try:
                return Decimal(val.replace(",", "").replace("$", "").strip()) or Decimal("0")
            except (ValueError, AttributeError):
                return Decimal("0")
        if isinstance(val, (int, float)):
            return Decimal(str(val))
        return Decimal("0")
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    return str(val) if val != "" else ""


def build_item_for_insert(extracted_data, new_loan_number, known_fields):
    """Build DynamoDB item from extracted data with assigned loan_number. Includes all extracted keys (dynamic schema)."""
    item = {"loan_number": new_loan_number}
    for key, val in extracted_data.items():
        if key == "loan_number":
            continue
        normalized = _normalize_value_for_dynamodb(key, val)
        if key in known_fields:
            item[key] = normalized
        elif normalized != "" and normalized is not None:
            item[key] = normalized
    return item


def lambda_handler(event, context):

    print("EVENT:", json.dumps(event))

    bucket = event["Records"][0]["s3"]["bucket"]["name"]
    key = unquote_plus(event["Records"][0]["s3"]["object"]["key"])

    print("Processing file:", bucket, key)

    # Extract config prefix from filename (e.g. sone_RV_Retail.pdf -> sone)
    config_id, has_prefix = extract_config_prefix_from_key(key)
    if not has_prefix or not config_id:
        handle_exception(
            bucket, key,
            "No identifiable config prefix in filename. Expected format: {prefix}_{filename}.pdf (e.g. sone_RV_Contract.pdf)"
        )
        return {"statusCode": 200, "body": "Exception: no config prefix"}

    config = get_config(config_id)
    if not config:
        handle_exception(
            bucket, key,
            f"Configuration not found for prefix '{config_id}'. Add a config with config_id='{config_id}' to the bedrock-demo-configs table."
        )
        return {"statusCode": 200, "body": "Exception: config not found"}

    extraction_fields = config["extraction_fields"]
    known_fields = set(extraction_fields)
    print("Using config:", config_id, "fields:", extraction_fields)

    document = s3.get_object(Bucket=bucket, Key=key)
    document_bytes = document["Body"].read()

    image_bytes_list = pdf_pages_to_png_bytes(document_bytes)
    print("PDF converted to", len(image_bytes_list), "page(s)")

    fields_str = ", ".join(extraction_fields)
    app_number_guidance = ""
    if "app_number" in known_fields:
        app_number_guidance = """
For app_number: the document may label it as "app#", "App No", "App No.", "Application Number", "Application #", or similar—especially when handwritten. Extract the numeric or alphanumeric identifier only (e.g. if you see "app# 33434" or "App No: 33434", extract "33434"). Be context-aware and infer the application number from any of these label variants.
"""

    sone_signature_guidance = ""
    if config_id == "sone":
        sone_signature_guidance = """
For signature fields ending in _signature_present: use true if a signature (handwritten, initials, or mark) is present in that section; false or null otherwise.
For date fields ending in _signature_date or _date: extract the date if present (any format).
For Model Clause – Optional Credit Life: checkboxes for Credit Life/D Disability (one buyer / both buyers). If checked or premium filled, extract the value.
For Model Clause – Physical Damage Insurance: collision, comprehensive, fire/theft premiums—if any are filled, extract values.
For Model Clause – Optional Insurance Coverages (GAP, etc): if premiums or coverage info filled, extract.
For Liability Insurance section: always note if buyer signature is present.

CRITICAL: physical_damage_premium must be a CURRENCY/DOLLAR value only (e.g. $165.00, 165). If the form has a name (e.g. co-borrower, insurance agent) next to a premium field, extract ONLY the dollar amount to the right of the name. Do NOT extract the person's name into premium fields.
"""
    extraction_prompt = f"""
Read the document image(s) and extract the following fields. Return STRICT JSON with these keys (use null if not found):
{fields_str}
{app_number_guidance}
{sone_signature_guidance}
Also extract any other relevant fields visible on the form. Use lowercase_with_underscores. Use exactly ONE canonical key per concept—equivalent labels are the same field (e.g. "vehicle identification number", "VIN", "Vehicle ID" → use only "vin"; "contract amount", "purchase price" → use only "contract_amount"). Do not duplicate the same value under different keys.

Return only the JSON object, no other text.
"""

    extraction_response = invoke_bedrock_with_images(extraction_prompt, image_bytes_list)
    print("RAW EXTRACTION RESPONSE:", extraction_response)

    extracted_data = safe_json_parse(extraction_response)
    extracted_data = normalize_extracted_keys(extracted_data)
    if config_id == "sone":
        extracted_data = _sanitize_currency_fields(extracted_data)
    print("PARSED EXTRACTED DATA:", extracted_data)

    signature_validation = compute_sone_signature_validation(extracted_data) if config_id == "sone" else []

    table = dynamodb.Table(TABLE_NAME)
    # When app_number is present, construct loan_number as {config_id}_{app_number}
    app_num = extracted_data.get("app_number")
    if app_num is not None and str(app_num).strip():
        app_num_clean = str(app_num).strip().replace(" ", "")
        loan_number = f"{config_id}_{app_num_clean}"
        extracted_data["loan_number"] = loan_number
    else:
        loan_number = extracted_data.get("loan_number") if isinstance(extracted_data.get("loan_number"), str) else None
        loan_number = loan_number.strip() if loan_number else None

    if not loan_number:
        # No loan number in document: assign new id and insert
        new_loan_number = get_next_loan_number(table)
        item = build_item_for_insert(extracted_data, new_loan_number, known_fields)
        table.put_item(Item=item)
        print("NEW INSERT (no loan number in doc):", new_loan_number, item)
        db_data = item
        comparison_data = {"match": True, "differences": []}
        original_pdf_url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=604800)
        report_html = generate_certification_report(extracted_data, db_data, comparison_data, is_new_insert=True, signature_validation=signature_validation, original_pdf_url=original_pdf_url)
        write_report(bucket, key, report_html)
        return {"statusCode": 200, "body": "Report generated (new insert)"}

    db_response = table.get_item(Key={"loan_number": loan_number})
    print("DB RESPONSE:", db_response)

    if "Item" not in db_response:
        # Loan number on form but not in database: insert using form's loan number
        item = build_item_for_insert(extracted_data, loan_number, known_fields)
        table.put_item(Item=item)
        print("NEW INSERT (loan not in DB, using form loan number):", loan_number, item)
        db_data = item
        comparison_data = {"match": True, "differences": []}
        original_pdf_url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=604800)
        report_html = generate_certification_report(extracted_data, db_data, comparison_data, is_new_insert=True, signature_validation=signature_validation, original_pdf_url=original_pdf_url)
        write_report(bucket, key, report_html)
        return {"statusCode": 200, "body": "Report generated (new insert)"}

    db_data = db_response["Item"]
    # Merge extracted data into existing record so new fields (e.g. vin) are added
    merged = dict(db_data)
    for k, val in extracted_data.items():
        if k == "loan_number":
            continue
        normalized = _normalize_value_for_dynamodb(k, val)
        if k in known_fields:
            merged[k] = normalized
        elif normalized not in (None, ""):
            merged[k] = normalized
    table.put_item(Item=merged)
    print("DUPLICATE: merged extracted fields into existing record", list(merged.keys()))
    db_data = merged
    # Run comparison and report
    db_data_for_prompt = {k: float(v) if isinstance(v, Decimal) else v for k, v in db_data.items()}
    comparison_prompt = f"""
Compare these two JSON objects and determine if they match.

Treat values as ALIKE (count as matching) when they represent the same value in different formats:
- Currency/numeric: $24,500.00, 24500, 24500.00, $24500, and 24,500 are all the same value.
- Ignore currency symbols ($), thousands separators (,), and trailing zeros after the decimal (e.g. 24500.00 = 24500).
- Compare the underlying numeric value, not the string formatting.
- Dates: treat as the same date regardless of format. Examples that are equivalent: 2025-01-01, 01-01-2025, 01-JAN-2025, January 1 2025, 1/1/2025. Consider year-month-day equivalence across YYYY-MM-DD, MM-DD-YYYY, DD-MM-YYYY, DD-MON-YYYY, and other common date formats.
- Whitespace: treat as the same if the only difference is leading/trailing spaces, multiple spaces vs single space, or tabs/newlines vs spaces.
- Case: treat as the same when the only difference is letter case (e.g. "John Smith" vs "JOHN SMITH", "ABC" vs "abc").
- Empty / missing placeholders: treat as equivalent: empty string, single space, "N/A", "n/a", "N.A.", "NA", "-", "—", and the string "null".
- Phone numbers: compare by digits only; (555) 123-4567, 555-123-4567, and 5551234567 are the same.

Return STRICT JSON with:
match (true/false),
differences (array of field names that differ—only list fields where values are truly different, not just formatted differently)

Extracted:
{json.dumps(extracted_data)}

Database:
{json.dumps(db_data_for_prompt)}
"""
    comparison_response = invoke_bedrock(comparison_prompt)
    print("RAW COMPARISON RESPONSE (duplicate):", comparison_response)
    comparison_data = safe_json_parse(comparison_response)
    print("PARSED COMPARISON DATA (duplicate):", comparison_data)
    original_pdf_url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=604800)
    report_html = generate_certification_report(
        extracted_data,
        db_data,
        comparison_data,
        is_new_insert=False,
        is_duplicate=True,
        signature_validation=signature_validation,
        original_pdf_url=original_pdf_url,
    )
    write_report(bucket, key, report_html)
    return {"statusCode": 200, "body": "Report generated (duplicate contract)"}


def pdf_pages_to_png_bytes(pdf_bytes):
    """Convert PDF bytes to list of PNG image bytes (one per page)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=150)
        images.append(pix.tobytes("png"))
    doc.close()
    return images


def invoke_bedrock(prompt):
    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt}
                    ]
                }
            ]
        }),
        contentType="application/json",
        accept="application/json"
    )
    response_body = json.loads(response["body"].read())
    return response_body["content"][0]["text"]


def invoke_bedrock_with_images(prompt, image_bytes_list):
    """Invoke Bedrock with one or more PNG images and a text prompt (vision OCR)."""
    content = []
    for img_bytes in image_bytes_list:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(img_bytes).decode("utf-8")
            }
        })
    content.append({"type": "text", "text": prompt})

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": content}]
        }),
        contentType="application/json",
        accept="application/json"
    )
    response_body = json.loads(response["body"].read())
    return response_body["content"][0]["text"]


def safe_json_parse(text):
    try:
        return json.loads(text)
    except:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except:
            pass

    print("FAILED TO PARSE JSON")
    return {}


def generate_certification_report(extracted_data, db_data, comparison_data, is_new_insert=False, is_duplicate=False, signature_validation=None, original_pdf_url=None):

    if is_new_insert:
        status_color = "#17a2b8"
        status_text = "NEW INSERT - No comparison made"
    elif is_duplicate:
        status_color = "#dc3545"
        status_text = "DUPLICATE CONTRACT"
    else:
        status_color = "#28a745" if comparison_data.get("match") else "#dc3545"
        status_text = "CERTIFIED - MATCH" if comparison_data.get("match") else "EXCEPTION - REVIEW REQUIRED"

    # Use db_data for table; for new insert it's the inserted row (same as extracted with new loan_number)
    differences_list = comparison_data.get("differences") or []
    all_keys = list(extracted_data.keys()) if extracted_data else list(db_data.keys()) if db_data else []
    rows = ""
    for key in all_keys:
        extracted_value = extracted_data.get(key, "")
        db_value = db_data.get(key, "")
        if is_new_insert:
            match_cell = "-"
            row_color = "#e7f5f9"
        elif is_duplicate:
            field_matches = key not in differences_list
            match_cell = "✔" if field_matches else "✖"
            row_color = "#f8f9fa" if field_matches else "#ffe6e6"
        else:
            match_cell = "✔" if extracted_value == db_value else "✖"
            row_color = "#f8f9fa" if extracted_value == db_value else "#ffe6e6"
        rows += f"""
        <tr style="background-color:{row_color};">
            <td>{key}</td>
            <td>{extracted_value}</td>
            <td>{db_value}</td>
            <td style="text-align:center;">{match_cell}</td>
        </tr>
        """

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    loan_display = db_data.get("loan_number", extracted_data.get("loan_number", "-"))
    if is_duplicate:
        match = comparison_data.get("match")
        diffs = differences_list
        if match:
            duplicate_note = "Data comparison: the PDF data matches the existing record."
        else:
            duplicate_note = "Data comparison: the PDF data does not match the existing record. Differences: " + (", ".join(diffs) if diffs else "see table above") + "."
    else:
        duplicate_note = ""

    sig_rows = ""
    if signature_validation:
        for s in signature_validation:
            sig_ok = s["present"] and (not s["date_required"] or s["date_present"])
            status_text = "Present" if sig_ok else ("Missing" if s["required"] else "N/A")
            status_style = "" if sig_ok else "color:red; font-weight:bold;" if s["required"] else ""
            date_text = ("Yes" if s["date_present"] else "No") if s["date_required"] else "-"
            sig_rows += f"""
        <tr>
            <td>{s["section"]}</td>
            <td>{s["note"]}</td>
            <td style="{status_style}">{status_text}</td>
            <td>{date_text}</td>
        </tr>
        """
    sig_section = ""
    if sig_rows:
        sig_section = f"""
        <h2>Signature Validation</h2>
        <table>
            <tr>
                <th>Section</th>
                <th>Requirement</th>
                <th>Signature</th>
                <th>Date</th>
            </tr>
            {sig_rows}
        </table>
        """
    sig_section = sig_section or ""

    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Loan Data Certification Report</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 40px;
                background-color: #f4f6f8;
            }}
            .logo {{
                margin-bottom: 20px;
            }}
            h1 {{
                color: #2c3e50;
            }}
            .summary {{
                padding: 20px;
                background-color: #ffffff;
                border-left: 6px solid {status_color};
                margin-bottom: 30px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background-color: white;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 10px;
            }}
            th {{
                background-color: #343a40;
                color: white;
            }}
            .footer {{
                margin-top: 40px;
                font-size: 12px;
                color: #555;
            }}
        </style>
    </head>
    <body>

        <div class="logo">
            <img src="{LOGO_URL}" height="60">
        </div>

        <h1>Loan Data Certification Report</h1>

        <div class="summary">
            <strong>Loan Number:</strong> {loan_display}<br>
            <strong>Status:</strong> <span style="color:{status_color}; font-weight:bold;">{status_text}</span><br>
            {("<strong>Comparison:</strong> " + duplicate_note + "<br>" if is_duplicate and duplicate_note else "")}
            <strong>Certification Timestamp:</strong> {now}
        </div>

        <h2>Field-Level Validation</h2>

        <table>
            <tr>
                <th>Field</th>
                <th>Contract Extracted Value</th>
                <th>Data Tape Value</th>
                <th>Match</th>
            </tr>
            {rows}
        </table>
        {sig_section}
        {f'<p><a href="{original_pdf_url}" target="_blank" rel="noopener" style="color:#58a6ff;">View original uploaded document (PDF)</a></p>' if original_pdf_url else ""}

        <div class="footer">
            {("The extracted data was inserted. No comparison was performed." if is_new_insert else "A record with this loan number already exists in the database. This contract is a duplicate and was not inserted." if is_duplicate else "This certification confirms that the contract document was processed using AI-based extraction and validated against backup servicer system-of-record data. Any discrepancies are flagged above for review.")}
        </div>

    </body>
    </html>
    """


def generate_not_found_report(extracted_data):
    return f"""
    <html>
    <body>
        <h1>Loan Not Found</h1>
        <p>Loan Number: {extracted_data.get("loan_number")}</p>
    </body>
    </html>
    """


def write_report(bucket, original_key, html):

    report_key = original_key.replace("inbound/", "reports/").replace(".pdf", "-report.html")

    print("WRITING REPORT TO:", report_key)

    s3.put_object(
        Bucket=bucket,
        Key=report_key,
        Body=html,
        ContentType="text/html"
    )