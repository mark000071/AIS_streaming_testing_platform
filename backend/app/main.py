"""Presentation-layer API: serves vessel/prediction/metrics data to the
Leaflet frontend in frontend/, sourced either from the bundled demo dataset
or a live MCM_streaming serving runtime (see config.py / data_source.py).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import metrics as metrics_mod
from .config import Settings, load_settings
from .data_source import Store
from .schemas import HealthStatus, MetricsSummary, PredictionDetail, VesselSummary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ais_platform")

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"

settings: Settings = load_settings()
store = Store(settings)

app = FastAPI(
    title="AIS Streaming Testing Platform",
    description="Presentation layer for MCM-Net live vessel-trajectory predictions.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/health", response_model=HealthStatus)
def health() -> HealthStatus:
    return HealthStatus(
        status="ok",
        data_mode=settings.data_mode,
        source_dir=store.source_description,
        n_records_loaded=len(store.predictions),
    )


@app.post("/api/reload")
def reload_data() -> dict:
    """Re-scans the data source. Cheap enough to call from a UI refresh
    button; the demo dataset is generated once and cached, the bridge
    reader re-reads the queue/metrics.sqlite on every call."""
    store.reload()
    return {"reloaded": True, "n_predictions": len(store.predictions)}


@app.get("/api/vessels", response_model=List[VesselSummary])
def list_vessels() -> List[VesselSummary]:
    return metrics_mod.vessel_summaries(store.predictions)[: settings.max_vessels]


@app.get("/api/vessels/{mmsi}/prediction", response_model=PredictionDetail)
def vessel_prediction(mmsi: int) -> PredictionDetail:
    rec = metrics_mod.prediction_for_vessel(store.predictions, mmsi)
    if rec is None:
        raise HTTPException(status_code=404, detail="no prediction for mmsi {}".format(mmsi))
    return metrics_mod.to_detail(rec)


@app.get("/api/metrics/summary", response_model=MetricsSummary)
def metrics_summary() -> MetricsSummary:
    return metrics_mod.metrics_summary(store.predictions, store.scores, settings.data_mode)


if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
