"""Loads prediction/score records for either serving mode.

Bridge mode reads exactly the on-disk layout MCM_streaming's
``serving/config/deployment.yaml`` declares under ``paths:`` --

    <runtime>/queue/predictions/*.json   live, not-yet-collected forecasts
                                          (serving/aisstream/predict/server.py)
    <runtime>/metrics/metrics.sqlite     reconciled scores, `scores` table
                                          (serving/aisstream/metrics/aggregate.py)

Nothing here imports MCM_streaming code; it only parses the JSON/SQLite it
writes. See docs/DATA_CONTRACT.md.
"""
from __future__ import annotations

import glob
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .config import Settings
from . import demo_data

logger = logging.getLogger("ais_platform.data_source")


class Store:
    """In-memory snapshot of predictions + reconciled scores, reloadable."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.predictions: List[Dict[str, Any]] = []
        self.scores: List[Dict[str, Any]] = []
        self.source_description = ""
        self.reload()

    # -- loading ----------------------------------------------------------
    def reload(self) -> None:
        if self.settings.data_mode == "bridge":
            self._load_bridge()
        else:
            self._load_demo()
        logger.info("loaded %d predictions, %d scores (%s)",
                   len(self.predictions), len(self.scores), self.source_description)

    def _load_demo(self) -> None:
        json_path = self.settings.demo_data_dir / "predictions.json"
        scores_path = self.settings.demo_data_dir / "scores.json"
        if json_path.exists() and scores_path.exists():
            self.predictions = json.loads(json_path.read_text())
            self.scores = json.loads(scores_path.read_text())
            self.source_description = "bundled demo dataset: {}".format(self.settings.demo_data_dir)
        else:
            # Fall back to generating in-process so the service still works
            # even if `demo/generate_demo_data.py` was never run.
            self.predictions, self.scores = demo_data.generate()
            self.source_description = "generated in-process demo dataset (no files on disk)"

    def _load_bridge(self) -> None:
        model_dir = self.settings.model_data_dir
        queue_dir = model_dir / "queue" / "predictions"
        metrics_db = model_dir / "metrics" / "metrics.sqlite"

        predictions: List[Dict[str, Any]] = []
        for path in sorted(glob.glob(str(queue_dir / "*.json")))[-self.settings.max_vessels * 20:]:
            try:
                predictions.append(json.loads(Path(path).read_text()))
            except (OSError, ValueError) as exc:
                logger.warning("skipping unreadable prediction record %s: %r", path, exc)
        self.predictions = predictions

        scores: List[Dict[str, Any]] = []
        if metrics_db.exists():
            try:
                conn = sqlite3.connect(str(metrics_db))
                try:
                    rows = conn.execute(
                        "SELECT payload_json FROM scores ORDER BY anchor_ts DESC LIMIT ?",
                        (self.settings.max_vessels * 50,),
                    ).fetchall()
                    scores = [json.loads(r[0]) for r in rows]
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                logger.warning("could not read metrics db %s: %r", metrics_db, exc)
        self.scores = scores
        self.source_description = "live MCM_streaming serving runtime: {}".format(model_dir)
