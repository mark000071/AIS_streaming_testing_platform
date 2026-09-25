"""Loads prediction/score records for either serving mode.

Bridge mode reads the on-disk layout MCM_streaming's
``serving/config/deployment.yaml`` declares under ``paths:``:

    <runtime>/queue/predictions/*.json    forecasts not yet collected
                                           (serving/aisstream/predict/server.py)
    <runtime>/predictions/YYYY-MM-DD.parquet
                                           collected forecasts, ``payload_json``
                                           column (serving/aisstream/predict/collect.py)
    <runtime>/metrics/metrics.sqlite      reconciled scores, ``scores`` table
                                           (serving/aisstream/metrics/aggregate.py)

The live service's main loop (``run_live.py``) runs the collector every
tick and deletes each queue JSON once it is in parquet, so the queue alone
is nearly always empty; the parquet store is the primary source and the
queue only adds the few seconds of forecasts not collected yet.

Nothing here imports MCM_streaming code. See docs/DATA_CONTRACT.md.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from . import demo_data
from .config import Settings

logger = logging.getLogger("ais_platform.data_source")


def _read_queue(queue_dir: Path, limit: int) -> List[Dict[str, Any]]:
    if not queue_dir.is_dir():
        return []
    # the collector deletes files concurrently, so any stat/read may vanish
    stamped = []
    for path in queue_dir.glob("*.json"):
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    records = []
    for _, path in sorted(stamped)[-limit:]:
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return records


def _read_prediction_store(store_dir: Path, max_vessels: int) -> List[Dict[str, Any]]:
    """Latest forecast per vessel from the newest daily parquet file.

    Two passes so memory stays bounded on a multi-GB day: the small key
    columns pick the wanted job_ids, then ``payload_json`` is streamed in
    batches and only those rows are decoded.
    """
    # glob("*.parquet") skips the collector's in-flight "*.parquet.tmp"
    files = sorted(store_dir.glob("*.parquet")) if store_dir.is_dir() else []
    if not files:
        return []
    try:
        import pyarrow.parquet as pq
    except ImportError:
        logger.warning("%s holds collected predictions but pyarrow is not installed; "
                       "install backend/requirements-bridge.txt to read them", store_dir)
        return []

    pf = pq.ParquetFile(str(files[-1]))
    latest: Dict[Any, tuple] = {}
    for row in pf.read(columns=["job_id", "mmsi", "anchor_ts"]).to_pylist():
        ts = row["anchor_ts"] or 0.0
        if row["mmsi"] is not None and ts >= latest.get(row["mmsi"], (None, -1.0))[1]:
            latest[row["mmsi"]] = (row["job_id"], ts)
    newest = sorted(latest.values(), key=lambda v: v[1], reverse=True)[:max_vessels]
    wanted = {job_id for job_id, _ in newest}

    records = []
    # a payload is ~15-25 KB (20x30x2 candidates as xy + lat/lon), so 512 rows
    # keeps each decoded batch around 10 MB
    for batch in pf.iter_batches(batch_size=512, columns=["job_id", "payload_json"]):
        for job_id, payload in zip(batch.column(0).to_pylist(), batch.column(1).to_pylist()):
            if job_id in wanted:
                records.append(json.loads(payload))
    return records


def _read_scores(metrics_db: Path, limit: int) -> List[Dict[str, Any]]:
    if not metrics_db.exists():
        return []
    try:
        # mode=ro: never create or write the model side's database
        conn = sqlite3.connect("file:{}?mode=ro".format(metrics_db.as_posix()), uri=True)
        try:
            rows = conn.execute(
                "SELECT payload_json FROM scores ORDER BY anchor_ts DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("could not read metrics db %s: %r", metrics_db, exc)
        return []
    return [json.loads(r[0]) for r in rows]


class Store:
    """In-memory snapshot of predictions + reconciled scores, reloadable."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.predictions: List[Dict[str, Any]] = []
        self.scores: List[Dict[str, Any]] = []
        self.source_description = ""
        self.reload()

    def reload(self) -> None:
        if self.settings.data_mode == "bridge":
            predictions, scores, desc = self._load_bridge()
        else:
            predictions, scores, desc = self._load_demo()
        # swap both at once so a concurrent request never mixes snapshots
        self.predictions, self.scores, self.source_description = predictions, scores, desc
        logger.info("loaded %d predictions, %d scores (%s)", len(predictions), len(scores), desc)

    def _load_demo(self):
        pred_path = self.settings.demo_data_dir / "predictions.json"
        scores_path = self.settings.demo_data_dir / "scores.json"
        if pred_path.exists() and scores_path.exists():
            return (json.loads(pred_path.read_text()), json.loads(scores_path.read_text()),
                    "bundled demo dataset: {}".format(self.settings.demo_data_dir))
        predictions, scores = demo_data.generate()
        return predictions, scores, "generated in-process demo dataset (no files on disk)"

    def _load_bridge(self):
        root = self.settings.model_data_dir
        cap = self.settings.max_vessels
        predictions = (_read_prediction_store(root / "predictions", cap)
                       + _read_queue(root / "queue" / "predictions", cap * 5))
        scores = _read_scores(root / "metrics" / "metrics.sqlite", cap * 50)
        return predictions, scores, "MCM_streaming serving runtime: {}".format(root)
