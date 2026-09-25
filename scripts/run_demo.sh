#!/usr/bin/env bash
# Quick local start in demo mode: no MCM_streaming checkout, no GPU, no
# live feed required. See docs/DEPLOYMENT.md for bridge mode.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m pip install -q -r backend/requirements.txt
python3 demo/generate_demo_data.py

export AIS_DATA_MODE="${AIS_DATA_MODE:-demo}"
export AIS_HOST="${AIS_HOST:-0.0.0.0}"
export AIS_PORT="${AIS_PORT:-8080}"

echo "Starting AIS Streaming Testing Platform on http://${AIS_HOST}:${AIS_PORT} (mode=${AIS_DATA_MODE})"
exec python3 -m uvicorn app.main:app --app-dir backend --host "$AIS_HOST" --port "$AIS_PORT"
