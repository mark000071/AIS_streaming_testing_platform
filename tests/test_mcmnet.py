import importlib.util
import logging
import math
import os

import numpy as np
import pytest
from envship_contracts import Window, streams
from envship_core import devstack
from envship_core.settings import Settings
from envship_core.tracker import Tracker
from envship_predictors.mcmnet import MCMNet, kmeans_seed, to_platform_paths, window_points
from envship_sdk import TileStore

from .conftest import FakePipe, straight_track

T0 = 1_790_000_000.0
MCM_R = 6371000.0  # MCM's preprocessing_config projection radius


class _McmProjection:
    """MCM's equirectangular inverse (serving/aisstream/features/projection.py), for tests without MCM."""

    def inverse_arrays(self, xs, ys, ref_lat, ref_lon):
        lat = np.degrees(np.asarray(ys) / MCM_R + math.radians(ref_lat))
        lon = np.degrees(np.asarray(xs) / (MCM_R * math.cos(math.radians(ref_lat))) + math.radians(ref_lon))
        return lat, lon


def _windows(tmp_path, track) -> list[Window]:
    pipe = FakePipe()
    trk = Tracker(Settings(data_dir=tmp_path, window_stride_steps=15), r=None, log=logging.getLogger("t"))
    for m in track:
        trk.on_raw(m, pipe)
    return pipe.decoded(streams.WINDOWS, Window)


def test_kmeans_seed_matches_mcm_predict_worker():
    # serving/aisstream/predict/server.py: (mmsi * 2654435761 + int(anchor_ts)) & 0x7fffffff
    assert kmeans_seed(230000001, 1_790_000_123.9) == (230000001 * 2654435761 + 1_790_000_123) & 0x7FFFFFFF


def test_window_points_follow_the_window_oldest_first(tmp_path):
    w = _windows(tmp_path, straight_track(230000001, T0, 45, 12.0, 70.0))[0]
    pts = window_points(w)
    assert len(pts) == 30
    assert (pts[-1].lat, pts[-1].lon) == pytest.approx((w.anchor_lat, w.anchor_lon))
    assert pts[-1].sog == pytest.approx(12.0, abs=0.1) and pts[-1].cog == pytest.approx(70.0, abs=0.5)


def test_unknown_speed_and_course_become_none(tmp_path):
    w = _windows(tmp_path, straight_track(230000001, T0, 45, 12.0, 70.0))[0]
    tokens = np.array(w.tokens, copy=True)
    tokens[3, 2] = np.nan
    tokens[4, 3] = np.nan
    pts = window_points(w.model_copy(update={"tokens": tokens}))
    assert pts[3].sog is None and pts[4].cog is None


def test_model_output_converts_to_platform_east_north():
    hyps = np.zeros((20, 30, 2))
    hyps[:, :, 0] = np.linspace(100, 3000, 30)  # due east in MCM metres
    paths = to_platform_paths(hyps, _McmProjection(), 60.2, 24.9)
    assert paths.shape == (20, 30, 2) and paths.dtype == np.float32
    # the two projections differ only in earth radius (6371000 vs 6371008.8 m): ~1.4e-6 relative
    assert np.allclose(paths[0, :, 0], hyps[0, :, 0], rtol=1e-5)
    assert np.allclose(paths[0, :, 1], 0.0, atol=1e-3)


def test_mcmnet_needs_its_paths(monkeypatch):
    monkeypatch.delenv("MCM_ROOT", raising=False)
    monkeypatch.delenv("MCM_WEIGHTS", raising=False)
    with pytest.raises(RuntimeError, match="MCM_ROOT"):
        MCMNet()


def test_devstack_runs_mcmnet_only_when_configured(monkeypatch):
    monkeypatch.delenv("MCM_ROOT", raising=False)
    monkeypatch.delenv("MCM_WEIGHTS", raising=False)
    assert devstack._mcmnet_ready() is False


_HAVE_MODEL = bool(os.environ.get("MCM_ROOT") and os.environ.get("MCM_WEIGHTS")) and all(
    importlib.util.find_spec(m) is not None for m in ("torch", "scipy")
)


@pytest.mark.skipif(not _HAVE_MODEL, reason="needs MCM_ROOT, MCM_WEIGHTS and torch")
def test_real_model_predicts_ahead_of_a_straight_vessel(tmp_path):
    w = _windows(tmp_path, straight_track(230000001, T0, 45, 12.0, 90.0))[0]
    p = MCMNet(threads=2)
    p.warmup(TileStore(None))
    c = p.predict(w)
    assert c.paths.shape == (20, 30, 2) and c.selected == 0
    end = np.asarray(c.paths)[0, -1]
    # 10 min at 12 kn due east is ~3.7 km east; the served candidate should head that way
    assert 2000 < end[0] < 5000 and abs(end[1]) < 1500
    assert p.predict(w).paths.tolist() == c.paths.tolist()  # deterministic per window
