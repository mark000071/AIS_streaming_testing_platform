"""Synthetic demo dataset generator.

Produces records that are byte-for-byte shaped like the ones MCM_streaming's
``serving/aisstream/predict/server.py`` writes to its prediction queue (see
docs/DATA_CONTRACT.md), plus a reconciled "scores" table shaped like a row
of ``serving/aisstream/reconcile/scoring.py`` / the ``scores`` table in
``metrics.sqlite``.

This is a **simulation for the presentation demo**, not a re-run of MCM-Net:
this repo has no GPU, no trained weights, and does not vendor the model
code. The synthetic "mcmnet" candidate fan is a deliberately-smarter
extrapolation (curved lookahead + small Gaussian spread) than the CV/Kalman
baselines, tuned so it reproduces the qualitative shape of the deployment's
headline finding (served rule beats CV/Kalman, MCMnet's own marginal
contribution is small but real) for demonstration purposes. Swap in a real
``AIS_MODEL_DATA_DIR`` (bridge mode) to see actual model-side output instead
-- see docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Tuple

EARTH_RADIUS_M = 6371000.0
GRID_INTERVAL_S = 20.0
PAST_LEN = 30
FUTURE_LEN = 30
N_HYPOTHESES = 20
KALMAN_Q = 0.05
KALMAN_R = 15.0

# A handful of illustrative starting points/headings roughly along the two
# feeds MCM_streaming serves (Finland Digitraffic / Norway Kystdatahuset),
# purely for a plausible-looking demo map -- not real AIS data.
_DEMO_VESSELS = [
    {"mmsi": 230123456, "source": "digitraffic", "ship_class": "cargo",
     "lat0": 60.170, "lon0": 24.940, "heading_deg": 95.0, "sog_kn": 14.5},
    {"mmsi": 230987654, "source": "digitraffic", "ship_class": "tanker",
     "lat0": 60.145, "lon0": 24.90, "heading_deg": 210.0, "sog_kn": 11.0},
    {"mmsi": 257345678, "source": "kystdatahuset", "ship_class": "passenger",
     "lat0": 59.905, "lon0": 10.72, "heading_deg": 340.0, "sog_kn": 18.0},
    {"mmsi": 257765432, "source": "kystdatahuset", "ship_class": "fishing",
     "lat0": 59.30, "lon0": 5.32, "heading_deg": 55.0, "sog_kn": 7.5},
    {"mmsi": 230555111, "source": "digitraffic", "ship_class": "cargo",
     "lat0": 60.05, "lon0": 25.10, "heading_deg": 250.0, "sog_kn": 12.0},
]


def _project(lat, lon, ref_lat, ref_lon):
    """lat/lon -> anchor-relative meters, matching the sign convention in
    MCM_streaming/serving/aisstream/predict/server.py::inverse_project."""
    ref_lat_rad = math.radians(ref_lat)
    x = (math.radians(lon) - math.radians(ref_lon)) * EARTH_RADIUS_M * math.cos(ref_lat_rad)
    y = (math.radians(lat) - math.radians(ref_lat)) * EARTH_RADIUS_M
    return x, y


def _inverse_project(x, y, ref_lat, ref_lon):
    ref_lat_rad = math.radians(ref_lat)
    lat = math.degrees(y / EARTH_RADIUS_M + ref_lat_rad)
    lon = math.degrees(x / (EARTH_RADIUS_M * math.cos(ref_lat_rad)) + math.radians(ref_lon))
    return lat, lon


def _track_xy(heading_deg, sog_kn, n_steps, dt_s, rng, curvature_deg_per_step=0.0):
    """Straight-ish track with a small stochastic heading drift, in
    anchor-relative meters, step index 0 == oldest."""
    speed_mps = sog_kn * 0.514444
    heading = math.radians(heading_deg)
    x, y = 0.0, 0.0
    pts = [(x, y)]
    for _ in range(n_steps - 1):
        heading += math.radians(curvature_deg_per_step) + rng.gauss(0, 0.01)
        x += speed_mps * dt_s * math.sin(heading)
        y += speed_mps * dt_s * math.cos(heading)
        pts.append((x, y))
    return pts


def _constant_velocity(hist_xy, horizon):
    (x1, y1), (x0, y0) = hist_xy[-1], hist_xy[-2]
    vx, vy = (x1 - x0) / GRID_INTERVAL_S, (y1 - y0) / GRID_INTERVAL_S
    out = []
    x, y = x1, y1
    for _ in range(horizon):
        x, y = x + vx * GRID_INTERVAL_S, y + vy * GRID_INTERVAL_S
        out.append((x, y))
    return out


def _kalman_like(hist_xy, horizon, rng):
    """Cheap stand-in for the real constant-velocity Kalman filter: instead
    of differencing only the last two points (as CV does, and is therefore
    sensitive to per-step heading noise), average the velocity over a
    trailing window -- a smoothed CV, consistently a little better than raw
    CV on straight tracks and roughly on par with it through a maneuver,
    which reproduces the paper's ordering (CV worse than Kalman worse than
    the served rule)."""
    window = min(10, len(hist_xy) - 1)
    (xN, yN) = hist_xy[-1]
    (x0, y0) = hist_xy[-1 - window]
    vx, vy = (xN - x0) / (window * GRID_INTERVAL_S), (yN - y0) / (window * GRID_INTERVAL_S)
    out = []
    x, y = xN, yN
    for _ in range(horizon):
        x, y = x + vx * GRID_INTERVAL_S, y + vy * GRID_INTERVAL_S
        out.append((x, y))
    return out


def _future_ground_truth(hist_xy, heading_deg, sog_kn, horizon, rng, maneuver):
    """The 'true' future used only to score the demo (never sent to the
    frontend as anything but derived deltas) -- a gentle turn on
    'maneuver' vessels, a straight continuation otherwise, matching the
    paper's framing that MCMnet's edge shows up specifically on maneuvers."""
    curvature = 1.4 if maneuver else 0.0
    rel = _track_xy(heading_deg, sog_kn, horizon + 1, GRID_INTERVAL_S, rng, curvature)
    x1, y1 = hist_xy[-1]
    return [(x1 + x, y1 + y) for x, y in rel[1:]]


def _mcmnet_fan(hist_xy, gt_xy, horizon, rng, n_hyp=N_HYPOTHESES):
    """A synthetic candidate fan: one candidate is a good-but-imperfect
    estimate of the (otherwise unknown) future, the rest are plausible
    alternates -- reproducing 'best-of-20 << top-1 < CV/Kalman on
    maneuvers' qualitatively, exactly what the demo dashboard highlights
    (see reports/GAP_DECOMPOSITION_REPORT.md in MCM_streaming for the real
    numbers this stands in for)."""
    hyps = []
    for k in range(n_hyp):
        noise_scale = 6.0 + 2.0 * k / n_hyp
        hyp = [(gx + rng.gauss(0, noise_scale), gy + rng.gauss(0, noise_scale))
               for gx, gy in gt_xy]
        hyps.append(hyp)
    # candidate 0 gets a slightly larger, systematic bias so top-1 != best-of-20,
    # mirroring the real deployment's honest gap between the two.
    x1, y1 = hist_xy[-1]
    hyps[0] = [(x + rng.gauss(0, 4.0), y + rng.gauss(0, 4.0)) for x, y in hyps[0]]
    return hyps


def _round_xy(xy):
    """Round to millimeters, matching server.py's np.round(arr, 3) -- also
    keeps the bundled demo JSON from ballooning with float noise."""
    return [(round(x, 3), round(y, 3)) for x, y in xy]


def _traj_errors(pred_xy, gt_xy):
    d = [math.hypot(px - gx, py - gy) for (px, py), (gx, gy) in zip(pred_xy, gt_xy)]
    return sum(d) / len(d), d[-1]


def generate(seed: int = 20260101, n_ticks: int = 6) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Returns (prediction_records, score_rows) -- both JSON-serializable and
    schema-compatible with real MCM_streaming output (see demo_data module
    docstring)."""
    rng = random.Random(seed)
    predictions: List[Dict[str, Any]] = []
    scores: List[Dict[str, Any]] = []
    base_ts = 1785000000.0  # arbitrary but stable epoch for the demo window

    for vessel in _DEMO_VESSELS:
        maneuver = vessel["ship_class"] in ("passenger", "fishing")
        for tick in range(n_ticks):
            anchor_ts = base_ts + tick * 300.0  # one forecast per vessel per 5 min (predict/cadence.py)
            hist = _track_xy(vessel["heading_deg"], vessel["sog_kn"], PAST_LEN,
                             GRID_INTERVAL_S, rng, curvature_deg_per_step=rng.uniform(-0.3, 0.3))
            ref_lat, ref_lon = vessel["lat0"], vessel["lon0"]

            cv = _constant_velocity(hist, FUTURE_LEN)
            ka = _kalman_like(hist, FUTURE_LEN, rng)
            gt = _future_ground_truth(hist, vessel["heading_deg"], vessel["sog_kn"],
                                      FUTURE_LEN, rng, maneuver)
            hyps = _mcmnet_fan(hist, gt, FUTURE_LEN, rng)
            top1 = hyps[0]
            # scorer/router blend: nudge top-1 toward CV the way the real
            # deployed rule does (alpha * candidate + (1-alpha) * CV,
            # config/deployment.yaml scorer.alpha == 0.1)
            alpha = 0.35 if maneuver else 0.1
            blended = [(alpha * hx + (1 - alpha) * cx, alpha * hy + (1 - alpha) * cy)
                      for (hx, hy), (cx, cy) in zip(top1, cv)]

            cv, ka, blended = _round_xy(cv), _round_xy(ka), _round_xy(blended)
            hyps = [_round_xy(h) for h in hyps]

            def to_latlon(xy):
                lat, lon = [], []
                for x, y in xy:
                    la, lo = _inverse_project(x, y, ref_lat, ref_lon)
                    lat.append(round(la, 7)); lon.append(round(lo, 7))
                return lat, lon

            cv_lat, cv_lon = to_latlon(cv)
            ka_lat, ka_lon = to_latlon(ka)
            hyp_latlon = [to_latlon(h) for h in hyps]
            blended_lat, blended_lon = to_latlon(blended)
            hist_lat, hist_lon = to_latlon(hist)

            job_id = "demo-{}-{}".format(vessel["mmsi"], int(anchor_ts))
            record = {
                "job_id": job_id,
                "meta": {
                    "job_id": job_id,
                    "mmsi": vessel["mmsi"],
                    "source": vessel["source"],
                    "ais_class": "A",
                    "unified_class": vessel["ship_class"],
                    "anchor_ts": anchor_ts,
                    "anchor_lat": ref_lat,
                    "anchor_lon": ref_lon,
                    "anchor_sog_kn": vessel["sog_kn"],
                    "run_label": "demo_dataset",
                    "enqueued_ts": anchor_ts,
                },
                "model_version": "demo-synthetic-v1",
                "issued_ts": anchor_ts + 0.31,
                "e2e_latency_s": round(rng.uniform(1.8, 3.2), 3),
                "worker_latency_s": round(rng.uniform(0.28, 0.42), 3),
                "mcmnet_latency_s": round(rng.uniform(0.25, 0.36), 3),
                "history": {"lat": hist_lat, "lon": hist_lon},
                "cv": {"xy": cv, "lat": cv_lat, "lon": cv_lon},
                "kalman": {"xy": ka, "lat": ka_lat, "lon": ka_lon},
                "mcmnet": {
                    "xy": [list(h) for h in hyps],
                    "lat": [ll[0] for ll in hyp_latlon],
                    "lon": [ll[1] for ll in hyp_latlon],
                    "top1_index": 0,
                    "scorer": {
                        "index": 0, "alpha": alpha, "artifact": "demo-scorer",
                        "xy": blended, "lat": blended_lat, "lon": blended_lon,
                    },
                    "routed": {
                        "mode": "maneuver" if maneuver else "straight",
                        "source": "scorer" if maneuver else "kalman",
                        "xy": blended if maneuver else ka,
                        "lat": blended_lat if maneuver else ka_lat,
                        "lon": blended_lon if maneuver else ka_lon,
                        "router": "demo-router",
                    },
                },
            }
            predictions.append(record)

            cv_ade, cv_fde = _traj_errors(cv, gt)
            ka_ade, ka_fde = _traj_errors(ka, gt)
            top1_ade, top1_fde = _traj_errors(top1, gt)
            best_ade = min(_traj_errors(h, gt)[0] for h in hyps)
            routed_xy = blended if maneuver else ka
            routed_ade, routed_fde = _traj_errors(routed_xy, gt)
            scores.append({
                "job_id": job_id,
                "mmsi": vessel["mmsi"],
                "source": vessel["source"],
                "ship_class": vessel["ship_class"],
                "anchor_ts": anchor_ts,
                "geo_stratum": "finland_ood" if vessel["source"] == "digitraffic" else "in_domain",
                "cv_ade": cv_ade, "cv_fde": cv_fde,
                "kalman_ade": ka_ade, "kalman_fde": ka_fde,
                "mcmnet_top1_ade": top1_ade, "mcmnet_top1_fde": top1_fde,
                "mcmnet_best20_ade": best_ade,
                "routed_ade": routed_ade, "routed_fde": routed_fde,
                "delta_ade_routed_minus_cv": routed_ade - cv_ade,
                "delta_ade_routed_minus_kalman": routed_ade - ka_ade,
                "delta_ade_mcmnet_minus_cv": top1_ade - cv_ade,
            })
    return predictions, scores
