#!/usr/bin/env bash
# Local start. Demo mode needs no MCM_streaming checkout, GPU or live feed;
# set AIS_DATA_MODE=bridge + AIS_MODEL_DATA_DIR for real data
# (docs/DEPLOYMENT.md).
set -euo pipefail
cd "$(dirname "$0")/.."

export AIS_DATA_MODE="${AIS_DATA_MODE:-demo}"
export AIS_HOST="${AIS_HOST:-0.0.0.0}"
export AIS_PORT="${AIS_PORT:-8090}"

if [ "$AIS_DATA_MODE" = "bridge" ]; then
  python3 -m pip install -q -r backend/requirements-bridge.txt
else
  python3 -m pip install -q -r backend/requirements.txt
  python3 demo/generate_demo_data.py
fi

echo "Starting AIS Streaming Testing Platform on http://${AIS_HOST}:${AIS_PORT} (mode=${AIS_DATA_MODE})"
exec python3 -m uvicorn app.main:app --app-dir backend --host "$AIS_HOST" --port "$AIS_PORT"
