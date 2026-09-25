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


def _anchor_ts(rec: Dict[str, Any]) -> float:
    return rec.get("meta", {}).get("anchor_ts") or 0.0


def latest_per_vessel(predictions: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Newest forecast per MMSI, ignoring records without an anchor position
    (nothing sensible to draw for them)."""
    latest: Dict[int, Dict[str, Any]] = {}
    for rec in predictions:
        meta = rec.get("meta", {})
        mmsi = meta.get("mmsi")
        if mmsi is None or meta.get("anchor_lat") is None or meta.get("anchor_lon") is None:
            continue
        prev = latest.get(mmsi)
        if prev is None or _anchor_ts(rec) >= _anchor_ts(prev):
            latest[mmsi] = rec
    return latest


def vessel_summaries(predictions: List[Dict[str, Any]]) -> List[VesselSummary]:
    out = []
    # newest first, so the endpoint's max_vessels cap drops stale vessels
    for mmsi, rec in sorted(latest_per_vessel(predictions).items(),
                            key=lambda kv: _anchor_ts(kv[1]), reverse=True):
        meta = rec["meta"]
        out.append(VesselSummary(
            mmsi=mmsi,
            source=meta.get("source", "unknown"),
            ship_class=meta.get("unified_class"),
            last_lat=meta["anchor_lat"],
            last_lon=meta["anchor_lon"],
            last_sog_kn=meta.get("anchor_sog_kn"),
            last_seen_ts=_anchor_ts(rec),
            has_model="mcmnet" in rec,
        ))
    return out


def prediction_for_vessel(predictions: List[Dict[str, Any]], mmsi: int) -> Optional[Dict[str, Any]]:
    latest = latest_per_vessel(predictions)
    return latest.get(mmsi)


def to_detail(rec: Dict[str, Any]) -> PredictionDetail:
    meta = rec["meta"]
    anchor = TrackPoint(lat=meta["anchor_lat"], lon=meta["anchor_lon"])
    hist = rec.get("history")
    if hist and hist.get("lat"):
        history = [TrackPoint(lat=la, lon=lo) for la, lo in zip(hist["lat"], hist["lon"])]
    else:
        history = [anchor]

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
        anchor=anchor,
        history=history,
        cv=_hyp_block(rec.get("cv")) or HypothesisBlock(lat=[], lon=[]),
        kalman=_hyp_block(rec.get("kalman")) or HypothesisBlock(lat=[], lon=[]),
        model=model_block,
        e2e_latency_s=rec.get("e2e_latency_s"),
        worker_latency_s=rec.get("worker_latency_s"),
    )


def _diff(a: str, b: str):
    def get(row: Dict[str, Any]) -> Optional[float]:
        if row.get(a) is None or row.get(b) is None:
            return None
        return row[a] - row[b]
    return get


# (label, value-of-row, description). The first three are stored as-is by
# MCM_streaming's reconcile/scoring.py; the marginal one is derived from its
# routed_ade/routed_cvkal_ade pair, exactly how ROUTING_SCORER_REPORT.md
# separates routing's contribution from MCM-Net's.
_DELTAS = [
    ("Served rule vs. Constant-Velocity", lambda r: r.get("delta_ade_routed_minus_cv"),
     "Mean ADE delta (m) of the served (routed) trajectory vs. CV; negative = closer to ground truth."),
    ("Served rule vs. Kalman", lambda r: r.get("delta_ade_routed_minus_kalman"),
     "Mean ADE delta (m) of the served (routed) trajectory vs. Kalman."),
    ("MCM-Net marginal (routed vs. CV|Kalman-only routing)", _diff("routed_ade", "routed_cvkal_ade"),
     "Same routing decision with CV on the maneuver leg instead of MCM-Net: what the deep model itself adds."),
    ("MCM-Net top-1 alone vs. Constant-Velocity", lambda r: r.get("delta_ade_mcmnet_minus_cv"),
     "The raw first candidate, before scoring/routing blend it with the baselines."),
]


def metrics_summary(predictions: List[Dict[str, Any]], scores: List[Dict[str, Any]],
                    data_mode: str) -> MetricsSummary:
    deltas = []
    for label, value, desc in _DELTAS:
        vals = [v for v in map(value, scores) if v is not None]
        if vals:
            deltas.append(BaselineDelta(label=label, n=len(vals),
                                        mean_delta_m=round(statistics.mean(vals), 2),
                                        description=desc))

    e2e = [r["e2e_latency_s"] for r in predictions if r.get("e2e_latency_s") is not None]
    mnet = [r["mcmnet_latency_s"] for r in predictions if r.get("mcmnet_latency_s") is not None]
    newest = max(predictions, key=_anchor_ts) if predictions else None

    return MetricsSummary(
        data_mode=data_mode,
        model_version=newest.get("model_version") if newest else None,
        n_predictions=len(predictions),
        n_scored=len(scores),
        n_vessels=len(latest_per_vessel(predictions)),
        served_vs_baselines=deltas,
        median_e2e_latency_s=round(statistics.median(e2e), 3) if e2e else None,
        median_mcmnet_latency_s=round(statistics.median(mnet), 3) if mnet else None,
    )
