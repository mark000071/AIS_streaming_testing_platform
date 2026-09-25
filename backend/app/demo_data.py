"""Synthetic demo dataset generator.

Produces records shaped like the ones MCM_streaming's
``serving/aisstream/predict/server.py`` writes (see docs/DATA_CONTRACT.md),
plus reconciled score rows shaped like
``serving/aisstream/reconcile/scoring.py`` output (the ``scores`` table in
``metrics.sqlite``).

This is a **simulation for the presentation demo**, not a re-run of MCM-Net:
this repo has no GPU, no trained weights, and does not vendor the model
code. The synthetic numbers are tuned to reproduce the *shape* of the
deployment's finding, not its values: the served rule beats CV/Kalman
mostly through motion-mode routing, while MCM-Net's own marginal
contribution (routed vs. the CV|Kalman-only routed ablation) is small.
Point bridge mode at a real ``AIS_MODEL_DATA_DIR`` for actual numbers.

Geometry follows the real contract: coordinates are anchor-relative meters,
the anchor is the *last* history point (0, 0), and every forecast starts
one grid step after it.
"""

from __future__ import annotations

import math
import random
from typing import Any

EARTH_RADIUS_M = 6371000.0
KNOTS_TO_MPS = 0.514444
GRID_INTERVAL_S = 20.0
PAST_LEN = 30
FUTURE_LEN = 30
N_HYPOTHESES = 20
SCORER_ALPHA = 0.1  # deployed scorer.alpha in MCM_streaming config/deployment.yaml
CADENCE_S = 300.0  # one forecast per vessel per 5 min (predict/cadence.py)
POSITION_NOISE_M = 4.0

# Illustrative starting points roughly inside the two feeds MCM_streaming
# serves (Finland Digitraffic / Norway Kystdatahuset) -- not real AIS data.
_DEMO_VESSELS = [
    {
        "mmsi": 230123456,
        "source": "digitraffic",
        "ship_class": "cargo",
        "lat0": 60.080,
        "lon0": 24.700,
        "heading_deg": 95.0,
        "sog_kn": 14.5,
    },
    {
        "mmsi": 230987654,
        "source": "digitraffic",
        "ship_class": "tanker",
        "lat0": 60.020,
        "lon0": 25.300,
        "heading_deg": 210.0,
        "sog_kn": 11.0,
    },
    {
        "mmsi": 257345678,
        "source": "kystdatahuset",
        "ship_class": "passenger",
        "lat0": 59.500,
        "lon0": 10.600,
        "heading_deg": 340.0,
        "sog_kn": 18.0,
    },
    {
        "mmsi": 257765432,
        "source": "kystdatahuset",
        "ship_class": "fishing",
        "lat0": 59.200,
        "lon0": 5.050,
        "heading_deg": 55.0,
        "sog_kn": 7.5,
    },
    {
        "mmsi": 230555111,
        "source": "digitraffic",
        "ship_class": "cargo",
        "lat0": 59.950,
        "lon0": 25.600,
        "heading_deg": 250.0,
        "sog_kn": 12.0,
    },
]
# Classes that turn in the demo; the real router decides from turn features.
_MANEUVER_CLASSES = ("passenger", "fishing")


def _inverse_project(x, y, ref_lat, ref_lon):
    """Anchor-relative meters -> (lat, lon); same equirectangular formula as
    MCM_streaming/serving/aisstream/predict/server.py::inverse_project."""
    ref_lat_rad = math.radians(ref_lat)
    lat = math.degrees(y / EARTH_RADIUS_M + ref_lat_rad)
    lon = math.degrees(x / (EARTH_RADIUS_M * math.cos(ref_lat_rad)) + math.radians(ref_lon))
    return lat, lon


def _step(x, y, heading_rad, dist_m):
    return x + dist_m * math.sin(heading_rad), y + dist_m * math.cos(heading_rad)


def _history(heading_deg, sog_kn, rng):
    """PAST_LEN points ending at the anchor (0, 0), oldest first: a gentle
    random turn plus AIS-like position noise. The noise is what makes the
    two-point CV velocity jittery and the smoothed (Kalman stand-in)
    velocity better."""
    dist = sog_kn * KNOTS_TO_MPS * GRID_INTERVAL_S
    turn = math.radians(rng.uniform(-0.05, 0.05))
    heading = math.radians(heading_deg)
    pts = [(0.0, 0.0)]
    x, y = 0.0, 0.0
    for _ in range(PAST_LEN - 1):
        heading -= turn
        x, y = _step(x, y, heading + math.pi, dist)  # walk backwards in time
        pts.append((x + rng.gauss(0, POSITION_NOISE_M), y + rng.gauss(0, POSITION_NOISE_M)))
    return pts[::-1]


def _future_ground_truth(heading_deg, sog_kn, maneuver, rng):
    """The realized future, used only to score the demo. Maneuvering vessels
    turn ~40 degrees over the horizon; the rest hold course."""
    dist = sog_kn * KNOTS_TO_MPS * GRID_INTERVAL_S
    turn = math.radians(1.4 if maneuver else 0.0)
    heading = math.radians(heading_deg)
    x, y, out = 0.0, 0.0, []
    for _ in range(FUTURE_LEN):
        heading += turn + rng.gauss(0, 0.01)
        x, y = _step(x, y, heading, dist)
        out.append((x, y))
    return out


def _extrapolate(hist_xy, window):
    """Constant velocity from the last `window` steps. window=1 is the CV
    baseline; a longer window is the demo's stand-in for the Kalman filter
    (a smoothed CV velocity -- not the real filter, which MCM_streaming runs
    with process_noise_accel=0.05, measurement_noise_m=15.0)."""
    (xn, yn), (x0, y0) = hist_xy[-1], hist_xy[-1 - window]
    vx, vy = (xn - x0) / window, (yn - y0) / window
    return [(xn + vx * (i + 1), yn + vy * (i + 1)) for i in range(FUTURE_LEN)]


def _mcmnet_fan(cv_xy, gt_xy, rng):
    """N_HYPOTHESES candidates, each pulled a random fraction of the way from
    CV toward the realized future, plus noise. Some land close (so
    best-of-20 is good) but candidate 0 -- the served top-1 -- is only a
    modest improvement, matching the deployment's honest top-1 vs. oracle
    gap."""
    hyps = []
    for k in range(N_HYPOTHESES):
        pull = 0.3 if k == 0 else rng.uniform(0.0, 1.0)
        hyps.append(
            [
                (cx + pull * (gx - cx) + rng.gauss(0, 15.0), cy + pull * (gy - cy) + rng.gauss(0, 15.0))
                for (cx, cy), (gx, gy) in zip(cv_xy, gt_xy, strict=True)
            ]
        )
    return hyps


def _blend(a_xy, b_xy, alpha):
    """alpha * a + (1 - alpha) * b -- the deployed scorer rule with a = scored
    candidate, b = CV."""
    return [
        (alpha * ax + (1 - alpha) * bx, alpha * ay + (1 - alpha) * by)
        for (ax, ay), (bx, by) in zip(a_xy, b_xy, strict=True)
    ]


def _round_xy(xy):
    """Millimeters, matching server.py's np.round(arr, 3)."""
    return [(round(x, 3), round(y, 3)) for x, y in xy]


def _traj_errors(pred_xy, gt_xy):
    d = [math.hypot(px - gx, py - gy) for (px, py), (gx, gy) in zip(pred_xy, gt_xy, strict=True)]
    return sum(d) / len(d), d[-1]


def _latlon_block(xy, ref_lat, ref_lon):
    lat, lon = [], []
    for x, y in xy:
        la, lo = _inverse_project(x, y, ref_lat, ref_lon)
        lat.append(round(la, 7))
        lon.append(round(lo, 7))
    return {"xy": [list(p) for p in xy], "lat": lat, "lon": lon}


def generate(seed: int = 20260101, n_ticks: int = 6) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (prediction_records, score_rows), both JSON-serializable."""
    rng = random.Random(seed)
    predictions: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    base_ts = 1785000000.0

    for vessel in _DEMO_VESSELS:
        maneuver = vessel["ship_class"] in _MANEUVER_CLASSES
        heading_rad = math.radians(vessel["heading_deg"])
        tick_dist = vessel["sog_kn"] * KNOTS_TO_MPS * CADENCE_S
        for tick in range(n_ticks):
            anchor_ts = base_ts + tick * CADENCE_S
            # the vessel advances between forecasts
            anchor_lat, anchor_lon = _inverse_project(
                *_step(0.0, 0.0, heading_rad, tick_dist * tick), vessel["lat0"], vessel["lon0"]
            )

            hist = _history(vessel["heading_deg"], vessel["sog_kn"], rng)
            gt = _future_ground_truth(vessel["heading_deg"], vessel["sog_kn"], maneuver, rng)
            cv = _round_xy(_extrapolate(hist, 1))
            ka = _round_xy(_extrapolate(hist, 10))
            hyps = [_round_xy(h) for h in _mcmnet_fan(cv, gt, rng)]
            top1 = hyps[0]
            scored = _round_xy(_blend(top1, cv, SCORER_ALPHA))
            # server.py: routed serves the scored blend on maneuvers, Kalman
            # otherwise; routed_cvkal is the same decision with CV on the
            # maneuver leg (the MCM-Net-free ablation).
            routed = scored if maneuver else ka
            routed_cvkal = cv if maneuver else ka

            def block(xy, ref_lat=anchor_lat, ref_lon=anchor_lon):
                return _latlon_block(xy, ref_lat, ref_lon)

            fan = [block(h) for h in hyps]
            job_id = "demo-{}-{}".format(vessel["mmsi"], int(anchor_ts))
            predictions.append(
                {
                    "job_id": job_id,
                    "meta": {
                        "job_id": job_id,
                        "mmsi": vessel["mmsi"],
                        "source": vessel["source"],
                        "ais_class": "A",
                        "unified_class": vessel["ship_class"],
                        "anchor_ts": anchor_ts,
                        "anchor_lat": round(anchor_lat, 7),
                        "anchor_lon": round(anchor_lon, 7),
                        "anchor_sog_kn": vessel["sog_kn"],
                        "run_label": "demo_dataset",
                        "enqueued_ts": anchor_ts,
                    },
                    "model_version": "demo-synthetic-v1",
                    "issued_ts": anchor_ts + 0.31,
                    "e2e_latency_s": round(rng.uniform(1.8, 3.2), 3),
                    "worker_latency_s": round(rng.uniform(0.28, 0.42), 3),
                    "mcmnet_latency_s": round(rng.uniform(0.25, 0.36), 3),
                    # not in real records (see DATA_CONTRACT.md); lets the demo map
                    # draw the track leading into the anchor
                    "history": {k: v for k, v in block(_round_xy(hist)).items() if k != "xy"},
                    "cv": block(cv),
                    "kalman": block(ka),
                    "mcmnet": {
                        "xy": [f["xy"] for f in fan],
                        "lat": [f["lat"] for f in fan],
                        "lon": [f["lon"] for f in fan],
                        "top1_index": 0,
                        "scorer": dict(block(scored), index=0, alpha=SCORER_ALPHA, artifact="demo-scorer"),
                        "routed": dict(
                            block(routed),
                            mode="maneuver" if maneuver else "straight",
                            source="scorer" if maneuver else "kalman",
                            router="demo-router",
                        ),
                        "routed_cvkal": dict(
                            block(routed_cvkal),
                            mode="maneuver" if maneuver else "straight",
                            source="cv" if maneuver else "kalman",
                            router="demo-router",
                        ),
                    },
                }
            )

            cv_ade, cv_fde = _traj_errors(cv, gt)
            ka_ade, ka_fde = _traj_errors(ka, gt)
            top1_ade, top1_fde = _traj_errors(top1, gt)
            routed_ade, routed_fde = _traj_errors(routed, gt)
            cvkal_ade, cvkal_fde = _traj_errors(routed_cvkal, gt)
            scores.append(
                {
                    "job_id": job_id,
                    "mmsi": vessel["mmsi"],
                    "source": vessel["source"],
                    "ship_class": vessel["ship_class"],
                    "anchor_ts": anchor_ts,
                    "geo_stratum": "finland_ood" if vessel["source"] == "digitraffic" else "in_domain",
                    "cv_ade": cv_ade,
                    "cv_fde": cv_fde,
                    "kalman_ade": ka_ade,
                    "kalman_fde": ka_fde,
                    "mcmnet_top1_ade": top1_ade,
                    "mcmnet_top1_fde": top1_fde,
                    "mcmnet_best20_ade": min(_traj_errors(h, gt)[0] for h in hyps),
                    "routed_ade": routed_ade,
                    "routed_fde": routed_fde,
                    "routed_cvkal_ade": cvkal_ade,
                    "routed_cvkal_fde": cvkal_fde,
                    "delta_ade_routed_minus_cv": routed_ade - cv_ade,
                    "delta_ade_routed_minus_kalman": routed_ade - ka_ade,
                    "delta_ade_routed_cvkal_minus_cv": cvkal_ade - cv_ade,
                    "delta_ade_mcmnet_minus_cv": top1_ade - cv_ade,
                }
            )
    return predictions, scores
