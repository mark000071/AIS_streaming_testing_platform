import importlib.util
from pathlib import Path

import numpy as np
from envship_contracts import Window
from envship_sdk import Predictor, predict_all

EXAMPLE = Path(__file__).resolve().parents[1] / "packages/sdk/examples/minimal.py"


def _window():
    xy = np.stack([np.linspace(-2900, 0, 30), np.zeros(30)], 1)
    tokens = np.zeros((30, 8), np.float32)
    tokens[:, :2] = xy
    tokens[:, 2], tokens[:, 3] = 10.0, 90.0
    return Window(
        window_id="w",
        mmsi=1,
        feed="fi",
        anchor_ts=0,
        enqueue_wall=0,
        anchor_lat=60,
        anchor_lon=24,
        tokens=tokens,
        abs_xy=np.zeros((30, 2)),
        geom=np.zeros(19, np.float32),
        tags=[],
    )


def test_minimal_example_satisfies_protocol():
    spec = importlib.util.spec_from_file_location("minimal", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    p = mod.MeanVelocity()
    assert isinstance(p, Predictor)
    assert len(EXAMPLE.read_text().splitlines()) <= 30
    (c,) = predict_all(p, [_window()])
    assert c.paths.shape == (1, 30, 2) and c.predictor_id == "example-meanvel"
    assert np.allclose(c.paths[0, -1], [3000.0, 0.0], atol=1.0)  # 100 m per step, 30 steps
