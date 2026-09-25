# Presentation service only (backend/ + frontend/). Does NOT build or
# contain any part of MCM_streaming -- see docs/ARCHITECTURE.md. For
# bridge mode, bind-mount a real MCM_streaming/serving/runtime directory
# at AIS_MODEL_DATA_DIR; this image never runs model inference itself.
FROM python:3.11-slim AS base

WORKDIR /srv

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY demo/ demo/

RUN python3 demo/generate_demo_data.py

ENV AIS_HOST=0.0.0.0 \
    AIS_PORT=8080 \
    AIS_DATA_MODE=demo

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2)" || exit 1

CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8080"]
