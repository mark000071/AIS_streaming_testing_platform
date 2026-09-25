"""Turns raw prediction/score records (see data_source.py) into API shapes."""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from .schemas import (
    BaselineDelta,
    HypothesisBlock,
    MetricsSummary,
    ModelBlock,
    PredictionDetail,
    TrackPoint,
    VesselSummary,
)


def _hyp_block(block: Optional[Dict[str, Any]]) -> Optional[HypothesisBlock]:
    if not block:
        return None
    return HypothesisBlock(lat=block["lat"], lon=block["lon"])


def latest_per_vessel(predictions: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    latest: Dict[int, Dict[str, Any]] = {}
    for rec in predictions:
        meta = rec.get("meta", {})
        mmsi = meta.get("mmsi")
        if mmsi is None:
            continue
        prev = latest.get(mmsi)
        if prev is None or meta.get("anchor_ts", 0) >= prev.get("meta", {}).get("anchor_ts", 0):
            latest[mmsi] = rec
    return latest


def vessel_summaries(predictions: List[Dict[str, Any]]) -> List[VesselSummary]:
    out = []
    for mmsi, rec in sorted(latest_per_vessel(predictions).items()):
        meta = rec.get("meta", {})
        anchor_lat, anchor_lon = meta.get("anchor_lat"), meta.get("anchor_lon")
        if anchor_lat is None or anchor_lon is None:
            hist = rec.get("history")
            if hist and hist.get("lat"):
                anchor_lat, anchor_lon = hist["lat"][-1], hist["lon"][-1]
        if anchor_lat is None or anchor_lon is None:
            continue
        out.append(VesselSummary(
            mmsi=mmsi,
            source=meta.get("source", "unknown"),
            ship_class=meta.get("unified_class"),
            last_lat=anchor_lat,
            last_lon=anchor_lon,
            last_sog_kn=meta.get("anchor_sog_kn"),
            last_seen_ts=meta.get("anchor_ts", 0.0),
            has_prediction="mcmnet" in rec or "cv" in rec,
        ))
    return out


def prediction_for_vessel(predictions: List[Dict[str, Any]], mmsi: int) -> Optional[Dict[str, Any]]:
    latest = latest_per_vessel(predictions)
    return latest.get(mmsi)


def to_detail(rec: Dict[str, Any]) -> PredictionDetail:
    meta = rec.get("meta", {})
    hist = rec.get("history")
    if hist and hist.get("lat"):
        history = [TrackPoint(lat=la, lon=lo) for la, lo in zip(hist["lat"], hist["lon"])]
    else:
        history = [TrackPoint(lat=meta.get("anchor_lat", 0.0), lon=meta.get("anchor_lon", 0.0))]

    model_block = None
    mc = rec.get("mcmnet")
    if mc:
        hyps = [HypothesisBlock(lat=la, lon=lo) for la, lo in zip(mc.get("lat", []), mc.get("lon", []))]
        routed = mc.get("routed") or {}
        model_block = ModelBlock(
            model_version=rec.get("model_version", "unknown"),
            top1_index=int(mc.get("top1_index", 0)),
            hypotheses=hyps,
            scorer=_hyp_block(mc.get("scorer")),
            routed=_hyp_block(routed if routed.get("lat") else None),
            routed_mode=routed.get("mode"),
            mcmnet_latency_s=rec.get("mcmnet_latency_s"),
        )

    return PredictionDetail(
        job_id=rec.get("job_id", ""),
        mmsi=meta.get("mmsi", 0),
        source=meta.get("source", "unknown"),
        issued_ts=rec.get("issued_ts", 0.0),
        anchor=TrackPoint(lat=meta.get("anchor_lat", 0.0), lon=meta.get("anchor_lon", 0.0)),
        history=history,
        cv=_hyp_block(rec.get("cv")) or HypothesisBlock(lat=[], lon=[]),
        kalman=_hyp_block(rec.get("kalman")) or HypothesisBlock(lat=[], lon=[]),
        model=model_block,
        e2e_latency_s=rec.get("e2e_latency_s"),
        worker_latency_s=rec.get("worker_latency_s"),
        raw=rec,
    )


_DELTA_FIELDS = [
    ("delta_ade_routed_minus_cv", "Served rule vs. Constant-Velocity",
     "Mean top-1 ADE delta (m); negative = served rule is closer to ground truth."),
    ("delta_ade_routed_minus_kalman", "Served rule vs. Kalman",
     "Mean top-1 ADE delta (m); negative = served rule is closer to ground truth."),
    ("delta_ade_mcmnet_minus_cv", "MCM-Net top-1 vs. Constant-Velocity",
     "Isolates the deep model's own candidate before routing/scoring blends it with CV."),
]


def metrics_summary(predictions: List[Dict[str, Any]], scores: List[Dict[str, Any]],
                    data_mode: str) -> MetricsSummary:
    deltas = []
    for field, label, desc in _DELTA_FIELDS:
        vals = [s[field] for s in scores if s.get(field) is not None]
        if vals:
            deltas.append(BaselineDelta(label=label, n=len(vals),
                                        mean_delta_m=round(statistics.mean(vals), 2),
                                        description=desc))

    e2e = [r["e2e_latency_s"] for r in predictions if r.get("e2e_latency_s") is not None]
    mnet = [r["mcmnet_latency_s"] for r in predictions if r.get("mcmnet_latency_s") is not None]
    model_version = None
    if predictions:
        model_version = predictions[-1].get("model_version")

    return MetricsSummary(
        data_mode=data_mode,
        model_version=model_version,
        n_predictions=len(predictions),
        n_vessels=len({r.get("meta", {}).get("mmsi") for r in predictions if r.get("meta")}),
        served_vs_baselines=deltas,
        median_e2e_latency_s=round(statistics.median(e2e), 3) if e2e else None,
        median_mcmnet_latency_s=round(statistics.median(mnet), 3) if mnet else None,
    )
