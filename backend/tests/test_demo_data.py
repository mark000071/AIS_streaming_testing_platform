import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import demo_data


def _dist_m(lat1, lon1, lat2, lon2):
    dy = math.radians(lat2 - lat1) * demo_data.EARTH_RADIUS_M
    dx = math.radians(lon2 - lon1) * demo_data.EARTH_RADIUS_M * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def test_generate_is_deterministic():
    assert demo_data.generate(seed=1, n_ticks=2) == demo_data.generate(seed=1, n_ticks=2)


def test_shapes_match_predict_worker_schema():
    preds, scores = demo_data.generate(seed=1, n_ticks=1)
    rec = preds[0]
    assert {"job_id", "meta", "cv", "kalman", "mcmnet"} <= set(rec)
    assert len(rec["mcmnet"]["xy"]) == len(rec["mcmnet"]["lat"]) == demo_data.N_HYPOTHESES
    for block in (rec["cv"], rec["kalman"], rec["mcmnet"]["routed"], rec["mcmnet"]["routed_cvkal"]):
        assert len(block["xy"]) == len(block["lat"]) == len(block["lon"]) == demo_data.FUTURE_LEN
    assert rec["mcmnet"]["scorer"]["alpha"] == demo_data.SCORER_ALPHA
    assert len(scores) == len(preds)


def test_history_ends_at_anchor_and_forecasts_start_next_to_it():
    preds, _ = demo_data.generate(seed=1, n_ticks=2)
    for rec in preds:
        m = rec["meta"]
        hist = rec["history"]
        assert len(hist["lat"]) == demo_data.PAST_LEN
        assert _dist_m(m["anchor_lat"], m["anchor_lon"], hist["lat"][-1], hist["lon"][-1]) < 0.01
        step_m = m["anchor_sog_kn"] * demo_data.KNOTS_TO_MPS * demo_data.GRID_INTERVAL_S
        first = _dist_m(m["anchor_lat"], m["anchor_lon"], rec["cv"]["lat"][0], rec["cv"]["lon"][0])
        assert 0.5 * step_m < first < 1.5 * step_m


def test_vessels_advance_between_ticks():
    preds, _ = demo_data.generate(seed=1, n_ticks=2)
    by_vessel = {}
    for rec in preds:
        by_vessel.setdefault(rec["meta"]["mmsi"], []).append(rec["meta"])
    for metas in by_vessel.values():
        a, b = metas
        moved = _dist_m(a["anchor_lat"], a["anchor_lon"], b["anchor_lat"], b["anchor_lon"])
        expected = a["anchor_sog_kn"] * demo_data.KNOTS_TO_MPS * demo_data.CADENCE_S
        assert abs(moved - expected) < 0.01 * expected


def test_scores_reproduce_the_deployment_story():
    # CV worse than Kalman worse than served; MCM-Net's own marginal
    # contribution real but a small share of the win; oracle >> top-1.
    _, scores = demo_data.generate()

    def mean(f):
        return statistics.mean(f(s) for s in scores)

    vs_cv = mean(lambda s: s["delta_ade_routed_minus_cv"])
    vs_kalman = mean(lambda s: s["delta_ade_routed_minus_kalman"])
    marginal = mean(lambda s: s["routed_ade"] - s["routed_cvkal_ade"])
    assert mean(lambda s: s["kalman_ade"]) < mean(lambda s: s["cv_ade"])
    assert vs_cv < vs_kalman < 0
    assert vs_cv < marginal < 0
    assert abs(marginal) < 0.5 * abs(vs_cv)
    assert mean(lambda s: s["mcmnet_best20_ade"]) < 0.5 * mean(lambda s: s["mcmnet_top1_ade"])
