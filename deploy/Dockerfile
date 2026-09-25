# Presentation service only (backend/ + frontend/). Does NOT build or
# contain any part of MCM_streaming -- see docs/ARCHITECTURE.md. For
# bridge mode, bind-mount a real MCM_streaming/serving/runtime directory
# read-only at AIS_MODEL_DATA_DIR; this image never runs model inference.
FROM python:3.11-slim

WORKDIR /srv

# requirements-bridge.txt = requirements.txt + pyarrow, so one image serves
# both demo and bridge mode.
COPY backend/requirements.txt backend/requirements-bridge.txt backend/
RUN pip install --no-cache-dir -r backend/requirements-bridge.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY demo/ demo/
RUN python3 demo/generate_demo_data.py \
 && useradd --system --no-create-home app

USER app

ENV AIS_DATA_MODE=demo \
    AIS_HOST=0.0.0.0 \
    AIS_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ['AIS_PORT'], timeout=2)" || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --app-dir backend --host \"$AIS_HOST\" --port \"$AIS_PORT\""]
