# Deployment configuration requirements

This covers the presentation service in this repository, **and** restates
(without modifying) the model-side requirements from
`MCM_streaming/serving/README.md` and `MCM_streaming/serving/config/deployment.yaml`
that a combined deployment needs to satisfy — because the presentation
layer is only useful once real predictions exist to show it. Model-side
figures are cited from `MCM_streaming`, not re-derived here.

## 1. Component map

| Component | Repo | Needs GPU? | Python | Notes |
|---|---|---|---|---|
| Ingest / tracker / gate / features / reconcile / metrics / supervisor | `MCM_streaming/serving` | No | py3.12 venv | pandas/pyarrow/zstd; unrelated to torch |
| **Model inference (`predict` worker)** | `MCM_streaming/serving/aisstream/predict/server.py` | **No — CPU-only in production** (see below) | py3.7 conda env `memonet37`, torch 1.7.1+cu110 build (CUDA build installed but run with `use_cuda: False`) | Loads `model_ae` + memory bank once per process, then serves one CPU forward pass per job |
| Continual retrain (`continual/retrain.py`) | `MCM_streaming/serving/continual` | Optional, faster with GPU | same `memonet37` env | Off the request path; periodic batch job, not part of this platform's serving path |
| **Presentation API + UI (this repo)** | `AIS_streaming_testing_platform` | No | Python 3.11+ | FastAPI + static Leaflet frontend; reads JSON/SQLite only, never runs a model |

## 2. Environment requirements

### 2.1 Presentation service (this repo) — what you actually deploy from this PR

- Python **3.11+** (developed/tested here on 3.11.15).
- Dependencies pinned in `backend/requirements.txt` (FastAPI, Uvicorn,
  Pydantic — no ML libraries; `pandas`/`pyarrow` are intentionally **not**
  required because bridge mode reads the live queue JSON + `metrics.sqlite`
  directly with the standard library).
- A modern evergreen browser for the frontend (Leaflet 1.9.4, vendored
  under `frontend/static/vendor/`, no CDN dependency — see §5 for why that
  matters for hardware/network-constrained deployments).
- Outbound HTTPS to `tile.openstreetmap.org` for basemap tiles at runtime
  (see §5 for the offline-deployment alternative).

### 2.2 Model-inference environment (unchanged, from `MCM_streaming`)

Restated here only so a deployer sizing hardware for the *combined* demo
knows what the presentation service depends on for real (`bridge`-mode)
data:

- `memonet37`: Python 3.7, `torch==1.7.1+cu110`, run with `use_cuda: False`
  in production (`serving/config/deployment.yaml`'s `model:` block) —
  **DECISIONS #8**: CPU inference was chosen deliberately; see §3 for why
  it's sufficient.
- Serving glue venv: Python 3.12, needs `pyyaml`, `pandas`, `pyarrow`,
  `zstandard`.
- Both environments are process-isolated by design (file-queue IPC between
  `predict/server.py` and the collector) specifically so the torch runtime
  never has to coexist with the newer venv — do not try to merge them.

## 3. Hardware configuration

### 3.1 Presentation service (this repo)

Stateless, reads small JSON/SQLite snapshots on each `/api/reload`; sized
for a demo/review deployment, not a production fleet:

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 1 vCPU | 2 vCPU (FastAPI + Uvicorn workers) |
| RAM | 256 MB | 512 MB–1 GB (headroom for `metrics.sqlite` scans; bounded by `AIS_MAX_VESSELS`) |
| Disk | ~20 MB (code + vendored Leaflet + demo dataset) | + whatever local cache you add for tiles if going offline (§5) |
| Network | none required in `demo` mode | outbound to OSM tiles (or a local tile server, §5); read access (NFS/bind-mount) to `MCM_streaming/serving/runtime/` in `bridge` mode |

Horizontally scalable: multiple replicas can point at the same read-only
`AIS_MODEL_DATA_DIR` mount; there is no server-side session state.

### 3.2 Model inference (`predict` worker) — cited from `MCM_streaming`

The paper's headline finding is that this is **not** a hardware-scaling
problem: `serving/README.md` reports **0.31 s median CPU inference
latency** and a **120 s freshness SLA met on 99.7%** of forecasts over a
20.9-day window, with latency **queue/feed-bound, not compute-bound**. So:

- CPU-only is sufficient for production serving — no GPU required on the
  `predict` worker host.
- `mcmnet_wrapper.py` calls `torch.set_num_threads(num_threads)`
  (`config.model` doesn't set this explicitly today — default is
  `torch`'s own heuristic; pin it explicitly, e.g. 4, on a shared host to
  avoid the predict worker starving `ingest`/`reconcile`).
- `reconcile` streams parquet via `iter_batches` specifically to keep peak
  RSS **< 0.3 GB** even on multi-GB daily archives — don't provision for
  loading full days into memory.
- A GPU is only relevant to `continual/retrain.py` (periodic, off the hot
  path) and offline training in `model/` — irrelevant to what this
  platform demonstrates live.

### 3.3 Combined-demo minimum (both repos, one host, for a review/demo)

For running `MCM_streaming/serving` (in replay mode against an archived
window — no live feed needed) alongside this presentation service on one
machine:

| Resource | Minimum |
|---|---|
| CPU | 4 cores (1–2 for the predict worker at `num_threads=4`, rest for ingest/tracker/reconcile/metrics + this API) |
| RAM | 8 GB (memory bank + embedding tables + `env_tiles` LRU cache of 64 tiles, per `config/deployment.yaml`'s `env_tiles.lru_tiles`, plus reconcile's bounded streaming) |
| Disk | Depends on replay window length; hourly zstd-parquet archive + `metrics.sqlite` grow with feed volume — plan retention/rotation for anything beyond a short demo window |
| GPU | Not required |

## 4. Parameters that matter for model inference (cited, not owned, by this repo)

These live in `MCM_streaming/serving/config/deployment.yaml` and are
surfaced here because they directly shape what the presentation layer
renders (candidate count, cadence, latency budget):

| Parameter | Value | Effect on the presentation layer |
|---|---|---|
| `model.past_len` / `model.future_len` | 30 / 30 (20 s grid → 10 min history, 10 min horizon) | `frontend/static/app.js` draws exactly this many points per polyline |
| Candidate count (`mcmnet.xy` shape) | 20 hypotheses per forecast | Rendered as the translucent "candidate fan" layer |
| `model.dim_embedding_key` | 64 | Fixed by the trained artifact; no presentation-side tuning |
| `model.kmeans_seed_mode` | `per_job` (deterministic candidate clustering) | Makes `bridge`-mode replays visually reproducible |
| `scorer.alpha` (deployed) | 0.1 (blend weight of the scored candidate vs. CV) | Shown as the "served rule" polyline |
| `routing.enabled` + `routing.rule_path` | motion-mode router (straight → Kalman, maneuver → scored blend) | Drives `routed.mode`/`routed.source`, shown in the detail panel |
| Predict cadence | 1 forecast per vessel per 5 min (`predict/cadence.py`) | Sets the natural `/api/reload` polling cadence — polling faster than this on a real deployment wastes cycles |
| Predict worker poll interval | `--poll-s` (default 2 s) | Floor on how fresh `bridge` mode's queue tail can be |
| `baselines.kalman.process_noise_accel` / `measurement_noise_m` | 0.05 m/s² / 15.0 m | Only relevant if reproducing the real Kalman baseline exactly; this repo's demo-mode stand-in (`backend/app/demo_data.py::_kalman_like`) is a simplified analogue, clearly labeled as such |
| Freshness SLA | 120 s, met on 99.7% of forecasts (20.9-day window) | Presentation-side `/api/reload` / auto-refresh cadence should stay well under this to avoid implying staler data than the model side actually produces |

## 5. Presentation-service configuration reference

Environment variables read by `backend/app/config.py`:

| Variable | Default | Purpose |
|---|---|---|
| `AIS_DATA_MODE` | auto-detect | Force `demo` or `bridge` |
| `AIS_DEMO_DATA_DIR` | `demo/data` | Where the bundled dataset lives |
| `AIS_MODEL_DATA_DIR` | `../MCM_streaming/serving/runtime` (sibling checkout) | Root of the model-side `runtime/` tree in bridge mode |
| `AIS_HOST` / `AIS_PORT` | `0.0.0.0` / `8080` | Bind address |
| `AIS_POLL_INTERVAL_S` | `2.0` | Reserved for a future auto-refresh loop; today the frontend refreshes on load + the reload button |
| `AIS_MAX_VESSELS` | `200` | Caps both the vessel list and how much of the queue/`scores` table is scanned per reload, bounding memory on a busy live feed |

**Offline / air-gapped deployments**: the frontend vendors Leaflet locally
(`frontend/static/vendor/leaflet/`) specifically so the only remaining
external dependency is the OpenStreetMap basemap tile fetch in
`frontend/static/app.js`. For a fully offline demo, point the `L.tileLayer`
URL at a local tile server or a pre-downloaded `.mbtiles` server — no other
code in this repo makes outbound network calls.

## 6. Running it

```bash
# Demo mode (no MCM_streaming checkout needed)
pip install -r backend/requirements.txt
python3 demo/generate_demo_data.py          # regenerate if you edited demo_data.py
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8080
# -> http://localhost:8080

# Bridge mode (MCM_streaming/serving already deployed/replaying next to this repo)
AIS_DATA_MODE=bridge \
AIS_MODEL_DATA_DIR=/path/to/MCM_streaming/serving/runtime \
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8080
```

Or via Docker (`deploy/Dockerfile`, `deploy/docker-compose.yml`):

```bash
docker compose -f deploy/docker-compose.yml up --build
```
