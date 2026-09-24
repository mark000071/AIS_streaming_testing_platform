"""In-process mode for local development: run a predictor over windows without Redis."""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator

from envship_contracts import Candidates, Window

from .predictor import Predictor, TileStore


def predict_all(
    predictor: Predictor, windows: Iterable[Window], tile_dir: str | None = None
) -> Iterator[Candidates]:
    predictor.warmup(TileStore(tile_dir))
    info = predictor.info()
    for w in windows:
        t0 = time.perf_counter()
        c = predictor.predict(w)
        c.window_id = w.window_id
        c.predictor_id = c.predictor_id or info.id
        c.model_revision = c.model_revision or info.version
        c.mmsi = w.mmsi
        c.anchor_ts = w.anchor_ts
        c.enqueue_wall = w.enqueue_wall
        c.compute_ms = (time.perf_counter() - t0) * 1000
        c.issue_wall = time.time()
        yield c
