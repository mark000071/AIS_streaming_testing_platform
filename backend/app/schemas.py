"""Response models for the presentation API.

These mirror (a subset of) the prediction record written by MCM_streaming's
``serving/aisstream/predict/server.py`` -- see docs/DATA_CONTRACT.md for the
full field-by-field mapping. Keeping the shapes aligned is what lets this
service render either the bundled demo dataset or a real model-side feed
with the same frontend code.
"""

from __future__ import annotations

from pydantic import BaseModel


class TrackPoint(BaseModel):
    lat: float
    lon: float


class HypothesisBlock(BaseModel):
    lat: list[float]
    lon: list[float]


class ModelBlock(BaseModel):
    model_version: str
    top1_index: int
    hypotheses: list[HypothesisBlock]  # up to K candidate futures
    scorer: HypothesisBlock | None = None
    routed: HypothesisBlock | None = None
    routed_mode: str | None = None
    mcmnet_latency_s: float | None = None


class VesselSummary(BaseModel):
    mmsi: int
    source: str  # 'digitraffic' | 'kystdatahuset'
    ship_class: str | None = None
    last_lat: float
    last_lon: float
    last_sog_kn: float | None = None
    last_seen_ts: float
    has_model: bool  # False when the model side runs baselines only


class PredictionDetail(BaseModel):
    job_id: str
    mmsi: int
    source: str
    issued_ts: float
    anchor: TrackPoint
    history: list[TrackPoint]
    cv: HypothesisBlock
    kalman: HypothesisBlock
    model: ModelBlock | None = None
    e2e_latency_s: float | None = None
    worker_latency_s: float | None = None


class BaselineDelta(BaseModel):
    label: str
    n: int
    mean_delta_m: float
    description: str


class MetricsSummary(BaseModel):
    data_mode: str
    model_version: str | None
    n_predictions: int  # forecasts loaded (bridge: latest per vessel + queue)
    n_scored: int  # reconciled rows the deltas are averaged over
    n_vessels: int
    served_vs_baselines: list[BaselineDelta]
    median_e2e_latency_s: float | None
    median_mcmnet_latency_s: float | None


class HealthStatus(BaseModel):
    status: str
    data_mode: str
    source_dir: str
    n_records_loaded: int
