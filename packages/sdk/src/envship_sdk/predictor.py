from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
from envship_contracts import Candidates, PredictorInfo, Window


class TileStore:
    """Read-only access to pre-built environment tiles on the shared volume.

    The demo ships no OSM tiles; ``get`` returns None and windows carry ``tile_id="none"``.
    """

    def __init__(self, root: str | Path | None):
        self.root = Path(root) if root else None

    def get(self, tile_id: str) -> np.ndarray | None:
        if self.root is None or tile_id == "none":
            return None
        path = self.root / f"{tile_id}.npy"
        return np.load(path, mmap_mode="r") if path.exists() else None


@runtime_checkable
class Predictor(Protocol):
    def info(self) -> PredictorInfo: ...

    def warmup(self, tiles: TileStore) -> None: ...

    def predict(self, w: Window) -> Candidates: ...


def make_candidates(
    w: Window, paths: np.ndarray, scores: np.ndarray | None = None, selected: int = 0
) -> Candidates:
    """Build a Candidates for ``w``; the runner fills predictor id, revision and clocks."""
    paths = np.asarray(paths, dtype=np.float32)
    if paths.ndim == 2:
        paths = paths[None]
    if scores is not None and selected == 0:
        selected = int(np.argmax(scores))
    return Candidates(
        window_id=w.window_id,
        mmsi=w.mmsi,
        anchor_ts=w.anchor_ts,
        enqueue_wall=w.enqueue_wall,
        paths=paths,
        scores=None if scores is None else np.asarray(scores, dtype=np.float32),
        selected=selected,
    )
