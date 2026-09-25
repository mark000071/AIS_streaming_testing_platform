import os
import sys
from pathlib import Path

os.environ["AIS_DATA_MODE"] = "demo"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["data_mode"] == "demo"
    assert body["n_records_loaded"] > 0


def test_list_vessels_nonempty():
    r = client.get("/api/vessels")
    assert r.status_code == 200
    vessels = r.json()
    assert len(vessels) == 5  # matches demo_data._DEMO_VESSELS
    for v in vessels:
        assert "mmsi" in v and "last_lat" in v and "last_lon" in v


def test_vessel_prediction_detail():
    mmsi = client.get("/api/vessels").json()[0]["mmsi"]
    r = client.get(f"/api/vessels/{mmsi}/prediction")
    assert r.status_code == 200
    detail = r.json()
    assert detail["mmsi"] == mmsi
    assert len(detail["cv"]["lat"]) > 0
    assert detail["model"] is not None
    assert len(detail["model"]["hypotheses"]) == 20


def test_vessel_prediction_404_for_unknown_mmsi():
    r = client.get("/api/vessels/999999999/prediction")
    assert r.status_code == 404


def test_metrics_summary_has_baseline_deltas():
    r = client.get("/api/metrics/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["n_predictions"] > 0
    assert body["served_vs_baselines"]
    labels = {d["label"] for d in body["served_vs_baselines"]}
    assert "Served rule vs. Constant-Velocity" in labels


def test_reload_endpoint():
    r = client.post("/api/reload")
    assert r.status_code == 200
    assert r.json()["reloaded"] is True
