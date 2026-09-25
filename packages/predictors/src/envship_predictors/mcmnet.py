"""MCM-Net as a platform predictor.

Runs the deployed MCM-Net artifact (``weights/combined`` from the MCM_streaming Hugging Face repo) on every
eligible window, next to the cv / kalman / imm baselines, scored by the same reconcile referee.

The model code, feature construction and environment rasterisation are imported unmodified from a checkout of
the MCM_streaming repository, so the inputs are built exactly as in training and in MCM's own deployment:

* ``model/models/model_test_trajectory_res.py`` builds the inference model around the trained autoencoder,
* ``serving/aisstream/features/{projection,builder}.py`` projects the history and builds the (30, 11) features,
* ``serving/aisstream/envtiles`` rasterises the 8-channel environment patch from pre-built OSM tiles.

Configuration (environment variables):

``MCM_ROOT``      MCM_streaming checkout (needs ``model/`` and ``serving/``)
``MCM_WEIGHTS``   directory with ``model_ae.pt``, ``memory_bank/``, ``frozen_feature_stats.json``,
                  ``preprocessing_config.json`` (the ``weights/combined`` folder)
``MCM_TILES``     env tile store root(s) built by ``aisstream.envtiles.build_tiles``, comma-separated.
                  Windows outside every store's coverage get the training loader's "missing environment"
                  input (all-zero descriptor and raster) and the revision is tagged ``+no-env``.
``MCM_DEVICE``    ``cpu`` (default) or ``cuda``
``MCM_THREADS``   torch intra-op threads on CPU (default: min(4, CPU count))

Needs ``torch`` and ``scipy`` on top of the workspace dependencies (``uv sync --group mcmnet``).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from envship_contracts import Window
from envship_sdk import PredictorInfo, TileStore, make_candidates
from envship_sdk.geo import to_enu

log = logging.getLogger("envship.mcmnet")

K = 20
PAST_LEN = 30
FUTURE_LEN = 30
UNKNOWN_CLASS = "unknown"  # the platform has no static (ship type) messages yet


class _Point(SimpleNamespace):
    """The subset of MCM's resampled-window point the feature builder reads."""


def kmeans_seed(mmsi: int, anchor_ts: float) -> int:
    """Deterministic per-window k-means seed, identical to MCM's predict worker (RESIDUAL_LOCALIZATION #3)."""
    return (int(mmsi) * 2654435761 + int(float(anchor_ts))) & 0x7FFFFFFF


def window_points(w: Window) -> list[_Point]:
    """Window -> the 30 resampled points MCM's FeatureBuilder expects (oldest first, anchor last)."""
    lat_lon = np.asarray(w.abs_xy, dtype=np.float64)
    tokens = np.asarray(w.tokens, dtype=np.float64)
    pts = []
    for (lat, lon), tok in zip(lat_lon, tokens, strict=True):
        sog, cog = float(tok[2]), float(tok[3])
        pts.append(
            _Point(
                lat=float(lat),
                lon=float(lon),
                sog=None if not np.isfinite(sog) else sog,
                cog=None if not np.isfinite(cog) else cog,
            )
        )
    return pts


def to_platform_paths(hyps: np.ndarray, projection: Any, anchor_lat: float, anchor_lon: float) -> np.ndarray:
    """MCM's anchor-relative metres (its own equirectangular projection) -> the platform's ENU metres."""
    lat, lon = projection.inverse_arrays(hyps[..., 0], hyps[..., 1], anchor_lat, anchor_lon)
    x, y = to_enu(lat, lon, anchor_lat, anchor_lon)
    return np.stack([x, y], axis=-1).astype(np.float32)


class MCMNet:
    def __init__(
        self,
        mcm_root: str | None = None,
        weights: str | None = None,
        tiles: str | None = None,
        device: str | None = None,
        threads: int | None = None,
    ):
        root = mcm_root or os.environ.get("MCM_ROOT")
        wdir = weights or os.environ.get("MCM_WEIGHTS")
        if not root or not wdir:
            raise RuntimeError(
                "MCM-Net needs MCM_ROOT (MCM_streaming checkout) and MCM_WEIGHTS (weights/combined)"
            )
        self.root = Path(root)
        self.weights = Path(wdir)
        self.tile_roots = [p for p in (tiles or os.environ.get("MCM_TILES", "")).split(",") if p]
        self.device = device or os.environ.get("MCM_DEVICE", "cpu")
        self.threads = threads or int(os.environ.get("MCM_THREADS", 0) or min(4, os.cpu_count() or 1))
        self.revision = "combined"
        self._model: Any = None

    def info(self) -> PredictorInfo:
        return PredictorInfo(
            id="mcmnet",
            version="combined",
            k=K,
            description="MCM-Net (deployed combined model), 20 k-means candidates; served = first candidate",
            cpu=float(self.threads),
            mem_mb=2048,
        )

    # ---- setup -------------------------------------------------------------------------------------------
    def _import(self, subdir: str, module: str) -> Any:
        path = str(self.root / subdir)
        if path not in sys.path:
            sys.path.insert(0, path)
        return importlib.import_module(module)

    def warmup(self, tiles: TileStore) -> None:
        torch = importlib.import_module("torch")
        pre = json.loads((self.weights / "preprocessing_config.json").read_text())
        projection_mod = self._import("serving", "aisstream.features.projection")
        builder_mod = self._import("serving", "aisstream.features.builder")
        self.projection = projection_mod.Projection(pre)
        self.features = builder_mod.FeatureBuilder(
            SimpleNamespace(pre=pre), self.projection, str(self.weights / "frozen_feature_stats.json")
        )

        store_mod = self._import("serving", "aisstream.envtiles.store")
        env = pre["env_v2"]
        self.stores = [
            store_mod.TileStore(
                r, patch_radius_m=float(env["patch_radius_m"]), grid_size=int(env["raster"]["grid_size"])
            )
            for r in self.tile_roots
            if os.path.isdir(r)
        ]
        if not self.stores:
            log.warning("MCM_TILES not set or missing: every window runs with a missing-environment input")
        grid = int(env["raster"]["grid_size"])
        # shiploader's convention for samples without environment data
        self._no_env = (
            np.zeros(len(env["descriptor_columns_ordered"]), np.float32),
            np.zeros((8, grid, grid), np.float32),
        )

        torch.set_num_threads(self.threads)
        use_cuda = self.device == "cuda"
        if use_cuda and not torch.cuda.is_available():
            raise RuntimeError("MCM_DEVICE=cuda but torch sees no GPU")
        self._torch = torch
        self._dev = torch.device("cuda" if use_cuda else "cpu")
        model_mod = self._import("model", "models.model_test_trajectory_res")
        ae = torch.load(self.weights / "model_ae.pt", map_location="cpu", weights_only=False)
        settings = {
            "train_batch_size": 1,
            "test_batch_size": 1,
            "use_cuda": use_cuda,
            "dim_feature_tracklet": PAST_LEN * 2,
            "dim_feature_future": FUTURE_LEN * 2,
            "dim_embedding_key": 64,
            "past_len": PAST_LEN,
            "future_len": FUTURE_LEN,
            "extra_feature_dim": 11,
            "memory_path_prefix": self._memory_prefix(),
            "use_env_input": True,
            "env_desc_dim": 19,
            "env_raster_channels": 8,
            "kmeans_seed_mode": "per_job",
        }
        self._model = model_mod.model_encdec(settings, ae).to(self._dev)
        self._model.eval()
        log.info(
            "MCM-Net loaded on %s (%d threads), memory bank %d entries, %d tile store(s)",
            self._dev,
            self.threads,
            int(self._model.memory_past.shape[0]),
            len(self.stores),
        )

    def _memory_prefix(self) -> str:
        """The model loads ``<prefix>_filter_past.pt``; the HF artifact names them ``memory_bank/filter_past.pt``."""
        bank = self.weights / "memory_bank"
        link_dir = Path(tempfile.mkdtemp(prefix="mcmnet_mem_"))
        for part in ("filter_past", "filter_fut"):
            (link_dir / f"combined_{part}.pt").symlink_to((bank / f"{part}.pt").resolve())
        return str(link_dir / "combined")

    def _environment(self, lat: float, lon: float) -> tuple[np.ndarray, np.ndarray] | None:
        for store in self.stores:
            if store.covers(lat, lon):
                desc_raw, masks6, sdf_shore, sdf_nav = store.build_patch(lat, lon)
                return self.features.normalize_descriptor(desc_raw), self.features.env_raster(
                    masks6, sdf_shore, sdf_nav
                )
        return None

    # ---- inference -------------------------------------------------------------------------------------------
    def predict(self, w: Window):
        torch = self._torch
        pts = window_points(w)
        traj_hist = self.features.hist_xy(pts)
        feats = self.features.hist_features(pts, UNKNOWN_CLASS)
        env = self._environment(w.anchor_lat, w.anchor_lon)
        env_desc, raster = env if env is not None else self._no_env

        self._model.kmeans_seed = kmeans_seed(w.mmsi, w.anchor_ts)
        dev = self._dev
        with torch.no_grad():
            hist = torch.from_numpy(traj_hist).unsqueeze(0).to(dev)
            x = hist - hist[:, PAST_LEN - 1 : PAST_LEN, :]
            out = self._model(
                x,
                hist,
                torch.from_numpy(feats).unsqueeze(0).to(dev),
                torch.tensor([[0, 1]], device=dev),
                hist[:, PAST_LEN - 1, :] / 1000.0,
                env_raster=torch.from_numpy(raster).unsqueeze(0).to(dev),
                env_desc=torch.from_numpy(env_desc).unsqueeze(0).to(dev),
            )
        hyps = out[0].detach().cpu().numpy()  # (20, 30, 2) MCM anchor-relative metres
        c = make_candidates(
            w, to_platform_paths(hyps, self.projection, w.anchor_lat, w.anchor_lon), selected=0
        )
        c.model_revision = self.revision if env is not None else f"{self.revision}+no-env"
        return c


def main() -> None:
    from envship_sdk import run_predictor

    t0 = time.time()
    predictor = MCMNet()
    log.info("starting MCM-Net predictor (setup %.1fs)", time.time() - t0)
    run_predictor(predictor)


if __name__ == "__main__":
    main()
