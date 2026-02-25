"""
Report Viewer - Lists and displays S3 certification reports.
Run locally with AWS credentials (aws sso login --profile bedrock).
"""
import os
from decimal import Decimal
from flask import Flask, render_template, jsonify, request, abort
import boto3
from botocore.exceptions import ClientError

app = Flask(__name__)
app.config["S3_BUCKET"] = os.environ.get("S3_BUCKET", "griboullie-bedrock-demo")
app.config["S3_REPORTS_PREFIX"] = os.environ.get("S3_REPORTS_PREFIX", "bedrock-demo/reports/")
app.config["AWS_REGION"] = os.environ.get("AWS_REGION", "us-west-2")
app.config["AWS_PROFILE"] = os.environ.get("AWS_PROFILE", "bedrock")
app.config["DYNAMODB_DATA_TABLE"] = os.environ.get("DYNAMODB_DATA_TABLE", "bedrock-demo-loans")
app.config["DYNAMODB_CONFIG_TABLE"] = os.environ.get("DYNAMODB_CONFIG_TABLE", "bedrock-demo-configs")

# Only include .html files (certification and exception reports)
REPORT_EXTENSIONS = (".html",)


def get_s3_client():
    session = boto3.Session(profile_name=app.config["AWS_PROFILE"], region_name=app.config["AWS_REGION"])
    return session.client("s3")


def get_dynamodb_resource():
    session = boto3.Session(profile_name=app.config["AWS_PROFILE"], region_name=app.config["AWS_REGION"])
    return session.resource("dynamodb")


def _serialize_item(item):
    """Convert DynamoDB item to JSON-serializable dict (Decimal -> float/int)."""
    if item is None:
        return None
    result = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            try:
                result[k] = int(v) if v % 1 == 0 else float(v)
            except (ValueError, ArithmeticError):
                result[k] = str(v)
        elif isinstance(v, list):
            result[k] = []
            for x in v:
                if isinstance(x, dict):
                    result[k].append(_serialize_item(x))
                elif isinstance(x, Decimal):
                    result[k].append(int(x) if x % 1 == 0 else float(x))
                else:
                    result[k].append(x)
        elif isinstance(v, dict):
            result[k] = _serialize_item(v)
        else:
            result[k] = v
    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/reports")
def list_reports():
    """List report objects in S3, newest first."""
    try:
        s3 = get_s3_client()
        bucket = app.config["S3_BUCKET"]
        prefix = app.config["S3_REPORTS_PREFIX"]
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except ClientError as e:
        return jsonify({"error": f"AWS error: {e.response.get('Error', {}).get('Message', str(e))}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    items = resp.get("Contents", [])
    reports = []
    for obj in items:
        key = obj["Key"]
        name = key.split("/")[-1]
        if name.lower().endswith(REPORT_EXTENSIONS):
            reports.append({
                "key": key,
                "name": name,
                "lastModified": obj.get("LastModified").isoformat() if obj.get("LastModified") else None,
                "size": obj.get("Size", 0),
            })

    # Newest first
    reports.sort(key=lambda r: r["lastModified"] or "", reverse=True)
    return jsonify({"reports": reports})


@app.route("/api/report")
def get_report():
    """Return presigned URL for a report, or fetch content for iframe embedding."""
    key = request.args.get("key")
    if not key:
        abort(400, "Missing key parameter")

    s3 = get_s3_client()
    bucket = app.config["S3_BUCKET"]

    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=3600,
        )
        return jsonify({"url": url})
    except ClientError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/dynamodb/data")
def get_dynamodb_data():
    """Scan and return items from the loans (data) DynamoDB table."""
    try:
        dynamo = get_dynamodb_resource()
        table = dynamo.Table(app.config["DYNAMODB_DATA_TABLE"])
        resp = table.scan()
        items = [_serialize_item(item) for item in resp.get("Items", [])]
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend([_serialize_item(item) for item in resp.get("Items", [])])
        return jsonify({"items": items})
    except ClientError as e:
        return jsonify({"error": f"AWS error: {e.response.get('Error', {}).get('Message', str(e))}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dynamodb/config")
def get_dynamodb_config():
    """Scan and return items from the configs DynamoDB table."""
    try:
        dynamo = get_dynamodb_resource()
        table = dynamo.Table(app.config["DYNAMODB_CONFIG_TABLE"])
        resp = table.scan()
        items = [_serialize_item(item) for item in resp.get("Items", [])]
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend([_serialize_item(item) for item in resp.get("Items", [])])
        return jsonify({"items": items})
    except ClientError as e:
        return jsonify({"error": f"AWS error: {e.response.get('Error', {}).get('Message', str(e))}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=True, host="0.0.0.0", port=port)
