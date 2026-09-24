"""Replay-style end-to-end run without Redis: tracker → predictors (in-process) → reconcile."""

import logging
import time

import numpy as np
from envship_contracts import Candidates, Score, Truth, Window, streams
from envship_core.reconcile import Reconciler, score_candidates
from envship_core.settings import Settings
from envship_core.tracker import Tracker
from envship_predictors import IMM, ConstantVelocity, Kalman
from envship_sdk import predict_all

from .conftest import FakePipe, straight_track

T0 = 1_790_000_000.0


def run_pipeline(tmp_path, tracks):
    s = Settings(data_dir=tmp_path, window_stride_steps=15)
    pipe = FakePipe()
    trk = Tracker(s, r=None, log=logging.getLogger("test"))
    msgs = sorted((m for tr in tracks for m in tr), key=lambda m: m.recv_ts)
    for m in msgs:
        trk.on_raw(m, pipe)
    windows = pipe.decoded(streams.WINDOWS, Window)
    truths = pipe.decoded(streams.TRUTH, Truth)

    rec = Reconciler(s, r=None, log=logging.getLogger("test"))
    predictors = [ConstantVelocity(), Kalman(), IMM()]
    rec.registry = {p.info().id: {"last_beat": time.time(), "required_tags": []} for p in predictors}
    for w in windows:
        rec.on_window(w)
    for p in predictors:
        for c in predict_all(p, windows):
            rec.on_candidates(c)
    out = FakePipe()
    for t in truths:
        rec.on_truth(t, out)
    return windows, truths, out.decoded(streams.SCORES, Score)


def test_straight_vessel_is_predicted_by_cv(tmp_path):
    windows, truths, scores = run_pipeline(tmp_path, [straight_track(230000001, T0, 45, 12.0, 70.0)])
    assert windows and truths
    assert {t.window_id for t in truths} <= {
        w.window_id for w in windows
    }  # the last windows' futures lie past the data
    assert all("scene:straight" in w.tags for w in windows)
    cv = [s for s in scores if s.predictor_id == "cv"]
    assert cv and all(not s.miss for s in cv)
    assert (
        max(s.served_ade or np.inf for s in cv) < 15.0
    )  # dead reckoning is (nearly) exact on a straight line
    kalman = [s for s in scores if s.predictor_id == "kalman"]
    assert max(s.served_ade or np.inf for s in kalman) < 40.0


def test_turning_vessel_favours_imm(tmp_path):
    _, _, scores = run_pipeline(tmp_path, [straight_track(230000002, T0, 45, 12.0, 0.0, turn_deg_s=0.12)])
    ade = {
        pid: np.mean([s.served_ade or np.nan for s in scores if s.predictor_id == pid])
        for pid in ("cv", "imm")
    }
    oracle_imm = np.mean([s.oracle_ade or np.nan for s in scores if s.predictor_id == "imm"])
    assert oracle_imm < ade["cv"]


def test_stationary_and_sparse_vessels_are_not_eligible(tmp_path):
    windows, _, _ = run_pipeline(
        tmp_path,
        [
            straight_track(230000003, T0, 45, 0.0, 0.0),  # moored
            straight_track(230000004, T0, 45, 12.0, 0.0, every_s=120),  # reports too rarely
        ],
    )
    assert windows == []


def test_missing_prediction_counts_as_miss(tmp_path):
    s = Settings(data_dir=tmp_path)
    rec = Reconciler(s, r=None, log=logging.getLogger("test"))
    rec.registry = {"cv": {"last_beat": time.time()}, "slow": {"last_beat": time.time()}}
    w = Window(
        window_id="w1",
        mmsi=1,
        feed="fi",
        anchor_ts=T0,
        enqueue_wall=100.0,
        anchor_lat=60,
        anchor_lon=24,
        tokens=np.zeros((30, 8), np.float32),
        abs_xy=np.zeros((30, 2)),
        geom=np.zeros(19, np.float32),
        tags=["benchmark"],
    )
    rec.on_window(w)
    fut = np.cumsum(np.ones((30, 2), np.float32), axis=0)
    rec.on_candidates(
        Candidates(
            window_id="w1",
            predictor_id="cv",
            paths=fut[None],
            enqueue_wall=100.0,
            issue_wall=100.2,
            anchor_ts=T0,
        )
    )
    rec.on_candidates(
        Candidates(
            window_id="w1",
            predictor_id="slow",
            paths=fut[None],
            enqueue_wall=100.0,
            issue_wall=107.0,
            anchor_ts=T0,
        )
    )
    out = FakePipe()
    rec.on_truth(
        Truth(
            window_id="w1",
            mmsi=1,
            feed="fi",
            anchor_ts=T0,
            future_xy=fut,
            valid=np.ones(30, bool),
            coverage=1.0,
        ),
        out,
    )
    by = {s.predictor_id: s for s in out.decoded(streams.SCORES, Score)}
    assert not by["cv"].miss and by["cv"].served_ade == 0.0 and by["cv"].comparable
    assert by["slow"].miss  # answered after the 5 s budget


def test_score_math_respects_validity_mask():
    fut = np.zeros((30, 2), np.float32)
    valid = np.ones(30, bool)
    valid[20:] = False
    fut[20:] = np.nan
    paths = np.zeros((2, 30, 2), np.float32)
    paths[0, :, 0] = 10.0  # 10 m off everywhere
    paths[1, :, 0] = 3.0
    c = Candidates(window_id="w", paths=paths, selected=0)
    t = Truth(window_id="w", mmsi=1, feed="fi", anchor_ts=0, future_xy=fut, valid=valid, coverage=2 / 3)
    r = score_candidates(c, t)
    assert r["served_ade"] == 10.0 and r["oracle_ade"] == 3.0 and r["served_fde"] == 10.0
    assert np.isnan(r["per_horizon"][25]) and r["k"] == 2
