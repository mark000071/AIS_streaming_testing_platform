"""Runtime settings for the presentation service.

Everything here is environment-driven so the same container image serves
either mode without a rebuild:

- ``demo``   -- reads the bundled synthetic dataset under ``demo/data``.
- ``bridge`` -- reads a real MCM_streaming ``serving/runtime`` tree
                (``AIS_MODEL_DATA_DIR``), read-only; no MCM_streaming code
                is imported (see docs/DATA_CONTRACT.md).

When ``AIS_DATA_MODE`` is unset the mode is auto-detected: ``bridge`` if
``AIS_MODEL_DATA_DIR`` is an existing, non-empty directory, else ``demo``.
Host/port are uvicorn CLI arguments (scripts/run_demo.sh, deploy/Dockerfile),
not settings here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEMO_DATA_DIR = REPO_ROOT / "demo" / "data"

# Sibling checkout convention used throughout docs/ARCHITECTURE.md: the two
# repositories are cloned next to each other, e.g.
#   workspace/AIS_streaming_testing_platform
#   workspace/MCM_streaming
# AIS_MODEL_DATA_DIR points at MCM_streaming's serving/runtime -- the
# directory tree serving/README.md documents under `paths:` in
# config/deployment.yaml (queue/, predictions/, metrics/metrics.sqlite).
# This repo only ever reads from it.
DEFAULT_MODEL_DATA_DIR = (
    REPO_ROOT.parent / "MCM_streaming" / "serving" / "runtime"
)


@dataclass(frozen=True)
class Settings:
    data_mode: str            # "demo" | "bridge"
    demo_data_dir: Path
    model_data_dir: Path
    max_vessels: int


def _resolve_mode(model_dir: Path) -> str:
    forced = os.environ.get("AIS_DATA_MODE", "").strip().lower()
    if forced in ("demo", "bridge"):
        return forced
    if model_dir.is_dir() and any(model_dir.iterdir()):
        return "bridge"
    return "demo"


def load_settings() -> Settings:
    demo_dir = Path(os.environ.get("AIS_DEMO_DATA_DIR", str(DEFAULT_DEMO_DATA_DIR)))
    model_dir = Path(os.environ.get("AIS_MODEL_DATA_DIR", str(DEFAULT_MODEL_DATA_DIR)))
    return Settings(
        data_mode=_resolve_mode(model_dir),
        demo_data_dir=demo_dir,
        model_data_dir=model_dir,
        max_vessels=int(os.environ.get("AIS_MAX_VESSELS", "200")),
    )
