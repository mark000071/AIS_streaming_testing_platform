"""Bridge mode against a fake MCM_streaming serving/runtime tree, written with
the same layouts as serving/aisstream/predict/collect.py (daily parquet with
payload_json) and metrics/aggregate.py (scores table)."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import demo_data, metrics
from app.config import Settings, _resolve_mode
from app.data_source import Store

SCORES_SCHEMA = """
CREATE TABLE scores (
    job_id TEXT PRIMARY KEY, run_label TEXT, ais_class TEXT,
    mmsi INTEGER, source TEXT, anchor_ts REAL,
    geo_stratum TEXT, ship_class TEXT, speed_stratum TEXT,
    comparable INTEGER, gt_coverage REAL, gt_points INTEGER,
    payload_json TEXT
);
"""


def _settings(root: Path) -> Settings:
    return Settings(data_mode="bridge", demo_data_dir=root / "unused",
                    model_data_dir=root, max_vessels=200)


def _real_shaped(records):
    """Demo records minus the demo-only 'history' block, like server.py output."""
    return [{k: v for k, v in r.items() if k != "history"} for r in records]


def _write_parquet(path: Path, records):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    rows = [{
        "job_id": r["job_id"], "mmsi": r["meta"]["mmsi"], "source": r["meta"]["source"],
        "anchor_ts": r["meta"]["anchor_ts"], "payload_json": json.dumps(r),
    } for r in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), str(path), compression="zstd")


def _write_scores(path: Path, scores):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCORES_SCHEMA)
    for s in scores:
        conn.execute("INSERT INTO scores (job_id, mmsi, source, anchor_ts, payload_json) "
                     "VALUES (?,?,?,?,?)",
                     (s["job_id"], s["mmsi"], s["source"], s["anchor_ts"], json.dumps(s)))
    conn.commit()
    conn.close()


@pytest.fixture
def runtime(tmp_path):
    predictions, scores = demo_data.generate(seed=3, n_ticks=3)
    return tmp_path, _real_shaped(predictions), scores


def test_reads_collected_parquet_when_queue_is_drained(runtime):
    root, predictions, _ = runtime
    _write_parquet(root / "predictions" / "2026-07-20.parquet", predictions)
    (root / "queue" / "predictions").mkdir(parents=True)   # drained by the collector

    store = Store(_settings(root))

    vessels = metrics.vessel_summaries(store.predictions)
    assert len(vessels) == len(demo_data._DEMO_VESSELS)
    newest_ts = max(p["meta"]["anchor_ts"] for p in predictions)
    assert all(v.last_seen_ts == newest_ts for v in vessels)
    # only the latest forecast per vessel is decoded from the parquet store
    assert len(store.predictions) == len(vessels)


def test_uncollected_queue_record_wins_when_newer(runtime):
    root, predictions, _ = runtime
    _write_parquet(root / "predictions" / "2026-07-20.parquet", predictions)
    newer = json.loads(json.dumps(predictions[0]))
    newer["job_id"] = newer["meta"]["job_id"] = "fresh-job"
    newer["meta"]["anchor_ts"] += 10_000
    queue = root / "queue" / "predictions"
    queue.mkdir(parents=True)
    (queue / "fresh-job.json").write_text(json.dumps(newer))
    (queue / "half-written.json").write_text("{not json")

    store = Store(_settings(root))

    rec = metrics.prediction_for_vessel(store.predictions, newer["meta"]["mmsi"])
    assert rec["job_id"] == "fresh-job"


def test_real_records_without_history_use_anchor(runtime):
    root, predictions, _ = runtime
    queue = root / "queue" / "predictions"
    queue.mkdir(parents=True)
    (queue / "a.json").write_text(json.dumps(predictions[0]))

    detail = metrics.to_detail(Store(_settings(root)).predictions[0])

    assert [(p.lat, p.lon) for p in detail.history] == [(detail.anchor.lat, detail.anchor.lon)]
    assert len(detail.model.hypotheses) == demo_data.N_HYPOTHESES
    assert detail.model.routed_mode in ("straight", "maneuver")


def test_scores_read_read_only_from_metrics_db(runtime):
    root, _, scores = runtime
    db = root / "metrics" / "metrics.sqlite"
    _write_scores(db, scores)
    before = db.stat().st_mtime_ns

    store = Store(_settings(root))
    summary = metrics.metrics_summary(store.predictions, store.scores, "bridge")

    assert summary.n_scored == len(scores)
    labels = [d.label for d in summary.served_vs_baselines]
    assert "MCM-Net marginal (routed vs. CV|Kalman-only routing)" in labels
    assert db.stat().st_mtime_ns == before


def test_missing_runtime_pieces_degrade_to_empty(tmp_path):
    store = Store(_settings(tmp_path))
    assert store.predictions == [] and store.scores == []
    summary = metrics.metrics_summary(store.predictions, store.scores, "bridge")
    assert summary.served_vs_baselines == [] and summary.model_version is None


def test_mode_autodetect(tmp_path, monkeypatch):
    monkeypatch.delenv("AIS_DATA_MODE", raising=False)
    assert _resolve_mode(tmp_path / "missing") == "demo"
    assert _resolve_mode(tmp_path) == "demo"          # exists but empty
    (tmp_path / "metrics").mkdir()
    assert _resolve_mode(tmp_path) == "bridge"
    monkeypatch.setenv("AIS_DATA_MODE", "demo")
    assert _resolve_mode(tmp_path) == "demo"


def test_malformed_records_do_not_crash_listing():
    good = _real_shaped(demo_data.generate(seed=1, n_ticks=1)[0])[0]
    no_anchor = json.loads(json.dumps(good))
    no_anchor["meta"]["mmsi"] = 1
    del no_anchor["meta"]["anchor_lat"]
    no_ts = json.loads(json.dumps(good))
    no_ts["meta"]["mmsi"] = 2
    no_ts["meta"]["anchor_ts"] = None

    vessels = metrics.vessel_summaries([good, no_anchor, no_ts])

    assert {v.mmsi for v in vessels} == {good["meta"]["mmsi"], 2}
