#!/bin/bash
# Run the report viewer. Uses venv if present.
cd "$(dirname "$0")"
if [ -d venv ]; then
  source venv/bin/activate
else
  echo "Creating venv..."
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
fi
export AWS_PROFILE="${AWS_PROFILE:-bedrock}"
echo "Starting at http://localhost:5000 (Ctrl+C to stop)"
python app.py
