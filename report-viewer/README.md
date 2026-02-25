# Report Viewer

Web app to browse and view certification reports from S3.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
# Activate venv if not already
source venv/bin/activate

# Ensure AWS credentials are available (e.g. aws sso login --profile bedrock)
export AWS_PROFILE=bedrock
python app.py
```

Then open http://localhost:5000 (or http://127.0.0.1:5000).

If localhost is denied (e.g. in a remote dev environment): the app binds to `0.0.0.0`, so you can use your machine's IP, or Cursor's port forwarding if available.

## Configuration

| Env var | Default |
|---------|---------|
| `S3_BUCKET` | `griboullie-bedrock-demo` |
| `S3_REPORTS_PREFIX` | `bedrock-demo/reports/` |
| `AWS_REGION` | `us-west-2` |
| `AWS_PROFILE` | `bedrock` |
