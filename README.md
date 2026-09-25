# AIS Streaming Testing Platform

The **presentation layer** (呈现端) for
[MCM-Net](https://github.com/mark000071/MCM_streaming)'s live vessel-trajectory
predictions: a small FastAPI service plus a Leaflet map UI that renders
predicted-vs-baseline trajectories and the deployment's headline
"served rule beats Constant-Velocity/Kalman" numbers.

This repo is deliberately thin. All model training, the live serving
pipeline (ingest → tracker → gate → features → predict → reconcile →
metrics), and the paper live in the separate `MCM_streaming` repository
(模型端 / model side), which this project reads from but never modifies —
see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for why, and
[`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) for exactly what's read.

## Quick start (demo mode, no dependencies on MCM_streaming)

```bash
./scripts/run_demo.sh
# -> http://localhost:8080
```

This generates and serves a deterministic synthetic dataset shaped exactly
like real MCM_streaming output, so the UI and the "served rule vs.
baselines" panel work standalone, anywhere.

## Pointing it at a real deployment (bridge mode)

```bash
AIS_DATA_MODE=bridge \
AIS_MODEL_DATA_DIR=/path/to/MCM_streaming/serving/runtime \
./scripts/run_demo.sh
```

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full environment,
hardware, and model-inference parameter requirements for a combined
deployment, and [`deploy/`](deploy/) for a Docker/Compose setup.

## Repository layout

| Path | What's inside |
|---|---|
| [`backend/`](backend/) | FastAPI service: `app/main.py` (API + static hosting), `app/data_source.py` (reads MCM_streaming's prediction queue / `metrics.sqlite`), `app/demo_data.py` (synthetic dataset generator), `tests/` |
| [`frontend/`](frontend/) | Static Leaflet map UI (`static/app.js`), vendored locally under `static/vendor/leaflet/` (no CDN dependency) |
| [`demo/`](demo/) | Bundled demo dataset + its generator script |
| [`deploy/`](deploy/) | `Dockerfile`, `docker-compose.yml`, env-file examples for demo/bridge mode |
| [`docs/`](docs/) | [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) (how the two repos combine), [`DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) (the exact JSON/SQLite interface), [`DEPLOYMENT.md`](docs/DEPLOYMENT.md) (environment, hardware, model-inference parameters) |

## Testing

```bash
pip install -r backend/requirements-dev.txt
python3 -m pytest backend/tests
```
