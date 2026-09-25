import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import demo_data


def test_generate_is_deterministic():
    preds_a, scores_a = demo_data.generate(seed=1, n_ticks=2)
    preds_b, scores_b = demo_data.generate(seed=1, n_ticks=2)
    assert preds_a == preds_b
    assert scores_a == scores_b


def test_generate_shapes_match_predict_worker_schema():
    preds, scores = demo_data.generate(seed=1, n_ticks=1)
    assert preds
    rec = preds[0]
    assert set(["job_id", "meta", "cv", "kalman", "mcmnet"]).issubset(rec)
    assert len(rec["mcmnet"]["xy"]) == demo_data.N_HYPOTHESES
    for block in (rec["cv"], rec["kalman"]):
        assert len(block["xy"]) == demo_data.FUTURE_LEN
        assert len(block["lat"]) == demo_data.FUTURE_LEN == len(block["lon"])
    assert scores
    assert "delta_ade_routed_minus_cv" in scores[0]


def test_scores_show_served_rule_beating_baselines_on_average():
    _, scores = demo_data.generate(seed=1, n_ticks=6)
    mean_delta = sum(s["delta_ade_routed_minus_cv"] for s in scores) / len(scores)
    # The whole point of the demo dataset is to make the paper's headline
    # claim visible: the served rule should beat plain CV on average.
    assert mean_delta < 0
