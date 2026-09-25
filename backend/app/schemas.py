"""Response models for the presentation API.

These mirror (a subset of) the prediction record written by MCM_streaming's
``serving/aisstream/predict/server.py`` -- see docs/DATA_CONTRACT.md for the
full field-by-field mapping. Keeping the shapes aligned is what lets this
service render either the bundled demo dataset or a real model-side feed
with the same frontend code.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class TrackPoint(BaseModel):
    lat: float
    lon: float


class HypothesisBlock(BaseModel):
    lat: List[float]
    lon: List[float]


class ModelBlock(BaseModel):
    model_version: str
    top1_index: int
    hypotheses: List[HypothesisBlock]      # up to K candidate futures
    scorer: Optional[HypothesisBlock] = None
    routed: Optional[HypothesisBlock] = None
    routed_mode: Optional[str] = None
    mcmnet_latency_s: Optional[float] = None


class VesselSummary(BaseModel):
    mmsi: int
    source: str                             # 'digitraffic' | 'kystdatahuset'
    ship_class: Optional[str] = None
    last_lat: float
    last_lon: float
    last_sog_kn: Optional[float] = None
    last_seen_ts: float
    has_model: bool                         # False when the model side runs baselines only


class PredictionDetail(BaseModel):
    job_id: str
    mmsi: int
    source: str
    issued_ts: float
    anchor: TrackPoint
    history: List[TrackPoint]
    cv: HypothesisBlock
    kalman: HypothesisBlock
    model: Optional[ModelBlock] = None
    e2e_latency_s: Optional[float] = None
    worker_latency_s: Optional[float] = None


class BaselineDelta(BaseModel):
    label: str
    n: int
    mean_delta_m: float
    description: str


class MetricsSummary(BaseModel):
    data_mode: str
    model_version: Optional[str]
    n_predictions: int                      # forecasts loaded (bridge: latest per vessel + queue)
    n_scored: int                           # reconciled rows the deltas are averaged over
    n_vessels: int
    served_vs_baselines: List[BaselineDelta]
    median_e2e_latency_s: Optional[float]
    median_mcmnet_latency_s: Optional[float]


class HealthStatus(BaseModel):
    status: str
    data_mode: str
    source_dir: str
    n_records_loaded: int
