# Deployment configuration requirements

This covers the two services in this repository (the EnvShip platform and
the MCM_streaming deployment viewer), **and** restates
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
| **EnvShip platform (this repo)**: ingest / replay / tracker / reconcile / jobs / API + predictor containers | `packages/`, `web/`, `compose.yaml` | No | Python 3.11 (uv workspace), Node 20+ to build `web/` | Redis Streams bus; each predictor is its own container, limited to 2 CPU / 4 GB (`compose.yaml`) |
| **MCM_streaming deployment viewer (this repo)** | `backend/`, `frontend/`, `deploy/` | No | Python 3.11+ | FastAPI + static Leaflet frontend; reads parquet/JSON/SQLite only, never runs a model |
| **MCM-Net predictor (`mcmnet`, `mcmnet-top1`)** | `packages/predictors/.../mcmnet.py`, model code from an MCM_streaming checkout, weights from HF | No for live 1× traffic; yes (or more CPU workers) for fast replay, see §4 | the uv workspace + torch, scipy (`scripts/setup_mcmnet.sh`); artifacts from HF: `weights/combined` (model, memory bank, `scorer_online_final`, `type_cache.json`) and `envtiles/tiles_{finland,norway}_full` | Runs as a process under `envship dev` when `MCM_ROOT`/`MCM_WEIGHTS` are set; ~0.75 GB RSS measured while predicting on CPU (model 281 MB + memory bank 146 MB); no Docker image yet |

## 2. Environment requirements

### 2.1 Presentation service (this repo) — what you actually deploy from this PR

- Python **3.11+** (developed/tested here on 3.11.15).
- Dependencies pinned in `backend/requirements.txt` (FastAPI, Uvicorn,
  Pydantic — no ML libraries). Bridge mode additionally needs `pyarrow`
  (`backend/requirements-bridge.txt`) to read the model side's collected
  `predictions/*.parquet`; `metrics.sqlite` and the queue JSON are read with
  the standard library. The Docker image installs the bridge set.
- A modern evergreen browser for the frontend (Leaflet 1.9.4, vendored
  under `frontend/static/vendor/`, no CDN dependency — see §6 for why that
  matters for hardware/network-constrained deployments).
- Outbound HTTPS to `tile.openstreetmap.org` for basemap tiles at runtime
  (see §6 for the offline-deployment alternative).

### 2.2 Model-inference environment (unchanged, from `MCM_streaming`)

Restated here only so a deployer sizing hardware for the *combined* demo
knows what the presentation service depends on for real (`bridge`-mode)
data:

- `memonet37`: Python 3.7, `torch==1.7.1+cu110`, run with `use_cuda: False`
  in production (`serving/config/deployment.yaml`'s `model:` block) —
  **DECISIONS #8**: CPU inference was chosen deliberately; see §4 for why
  it's sufficient.
- Serving glue venv: Python 3.12, needs `pyyaml`, `pandas`, `pyarrow`,
  `zstandard`.
- Both environments are process-isolated by design (file-queue IPC between
  `predict/server.py` and the collector) specifically so the torch runtime
  never has to coexist with the newer venv — do not try to merge them.

## 3. Hardware configuration

### 3.0 EnvShip platform (this repo)

From `compose.yaml` and `docs/roadmap.md` §3.5:

| Resource | Demo (replay, 4 built-in predictors; estimate) | Recommended long-running host (roadmap) |
|---|---|---|
| CPU | 4 vCPU | 8 vCPU (roadmap: Hetzner CX42 class). Each predictor container is capped at 2 CPU |
| RAM | 8 GB | 16 GB. Each predictor container is capped at 4 GB; Redis keeps only the trimmed stream retention |
| Disk | ~2 GB (images + fixture replay output under `data/`) | 160 GB + a 100 GB volume for the parquet archive |
| GPU | None | None |
| Network | None for replay | Outbound MQTT-over-WSS to Digitraffic for `--live`; ports 80/443 for Caddy |

Measured locally for this merge (replay at 20×, 4 predictors): cv/kalman
compute p50 0.3–1.3 ms and IMM ~6 ms per window, with queue p90 under
0.3 s, far inside the 5 s answer budget.

### 3.1 MCM_streaming deployment viewer (this repo)

Stateless, reads small JSON/SQLite snapshots on each `/api/reload`; sized
for a demo/review deployment, not a production fleet:

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 1 vCPU | 2 vCPU (FastAPI + Uvicorn workers) |
| RAM | 256 MB | 512 MB–1 GB (headroom for `metrics.sqlite` scans; bounded by `AIS_MAX_VESSELS`) |
| Disk | ~20 MB (code + vendored Leaflet + demo dataset) | + whatever local cache you add for tiles if going offline (§6) |
| Network | none required in `demo` mode | outbound to OSM tiles (or a local tile server, §6); read access (NFS/bind-mount) to `MCM_streaming/serving/runtime/` in `bridge` mode |

Horizontally scalable: multiple replicas can point at the same read-only
`AIS_MODEL_DATA_DIR` mount; there is no server-side session state.

### 3.2 Model inference (`predict` worker) — cited from `MCM_streaming`

The paper's headline finding is that this is **not** a hardware-scaling
problem: `serving/README.md` reports **0.31 s median CPU inference
latency** and a **120 s freshness SLA met on 99.7%** of forecasts over a
20.9-day window, with latency **queue/feed-bound, not compute-bound**. So:

- CPU-only is sufficient for production serving — no GPU required on the
  `predict` worker host.
- `mcmnet_wrapper.py` calls `torch.set_num_threads(num_threads)` with the
  constructor default of 4; `load_from_config` does not pass a value, so
  the predict worker always uses 4 intra-op threads. Budget 4 cores for it
  on a shared host, or it will compete with `ingest`/`reconcile`.
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
| CPU | 6–8 cores: 4 for the predict worker's torch threads, the rest for ingest/tracker/reconcile/metrics + this API |
| RAM | 8 GB (estimate, not a measured figure: model + memory bank, the `env_tiles` LRU cache of 64 tiles from `env_tiles.lru_tiles`, reconcile's bounded streaming, and this API) |
| Disk | Depends on replay window length; hourly zstd-parquet archive + `metrics.sqlite` grow with feed volume — plan retention/rotation for anything beyond a short demo window |
| GPU | Not required |

## 4. Why model inference does not use a GPU

MCM_streaming made this choice deliberately (`docs/DECISIONS.md` #8: "部署推理走
CPU … GPU 留给训练"), and its own measurements back it
(`reports/LATENCY_SLA_ANALYSIS.md`, 944,589 served forecasts):

- **Compute is 2.9% of service latency.** A forward pass takes 0.31 s
  median on CPU; the queue adds 10 s median / 61 s P99, and window
  formation up to ~77 s. A GPU that made inference 10× faster would cut
  P99 service latency by ~0.28 s (<0.5%) and leave freshness unchanged.
  Zeroing compute entirely would move P99 freshness only from 138 s to
  137.6 s.
- **Requests are single samples.** Each forecast is one vessel window
  (batch size 1), paced at one per vessel per 5 min. A GPU's advantage is
  large batches, which this workload doesn't have, and the CPU worker
  already runs at only ~25% average utilization.
- **The scaling lever is more workers, not a GPU.** One serial worker
  sustains ~3.17 forecasts/s. The only overload is sub-minute bursts
  (peak 4.03/s); a second CPU worker fixes that (P99 61 s → ~20 s). About
  7 workers at half duty (one 8–16-core CPU host) would serve ~5× today's
  fleet, on the order of 10,000+ vessels within the 120 s SLA.
- **The GPU is busy with training.** Retraining, memory-bank A/B tests and
  ablations queue for the single NVIDIA L40S behind a flock lock
  (`docs/DECISIONS.md`, `docs/RESOURCE_REQUEST.md`). Serving on it would
  contend with that work and tie the always-on service to one card.
- **It keeps the deployment simple.** CPU inference runs on any commodity
  VM (the roadmap's 8 vCPU / 16 GB host), with no GPU host to rent or
  maintain for an always-on service.

**When a GPU would matter:** training and retraining (already on GPU);
micro-batching many vessels per forward pass at far larger scale; or a
much heavier future model where compute stops being a rounding error in
latency. The EnvShip roadmap lists "GPU 推理（计算占延迟 2.9%）" among the options
it rejected (`docs/roadmap.md` §3) for live serving.

**The replay demo is the exception.** Live traffic reaches the model at under
1 window/s, but the default 20× replay produces about 6 windows/s, while one
CPU `mcmnet` process answers 1.2–3 windows/s (0.3–0.8 s each, measured at 2–4
threads). Windows that wait longer than the platform's 5 s budget are skipped
and count as misses. For a replay demo use `envship dev --speed 5`, several
`--mcmnet-replicas` on a multi-core host, or `MCM_DEVICE=cuda` on a GPU. The
offline `envship benchmark` has no such limit: it answers every window.

## 5. Parameters that matter for model inference (cited, not owned, by this repo)

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
| Predict worker poll interval | `--poll-s` (default 2 s) | Floor on how fresh a forecast `bridge` mode can show |
| `baselines.kalman.process_noise_accel` / `measurement_noise_m` | 0.05 m/s² / 15.0 m | Only relevant if reproducing the real Kalman baseline exactly; this repo's demo-mode stand-in (`backend/app/demo_data.py::_kalman_like`) is a simplified analogue, clearly labeled as such |
| Freshness SLA | 120 s, met on 99.7% of forecasts (20.9-day window) | Presentation-side `/api/reload` / auto-refresh cadence should stay well under this to avoid implying staler data than the model side actually produces |

## 6. Viewer configuration reference

Environment variables read by `backend/app/config.py`:

| Variable | Default | Purpose |
|---|---|---|
| `AIS_DATA_MODE` | auto-detect | Force `demo` or `bridge` |
| `AIS_DEMO_DATA_DIR` | `demo/data` | Where the bundled dataset lives |
| `AIS_MODEL_DATA_DIR` | `../MCM_streaming/serving/runtime` (sibling checkout) | Root of the model-side `runtime/` tree in bridge mode |
| `AIS_HOST` / `AIS_PORT` | `0.0.0.0` / `8090` | Bind address, passed to uvicorn by `scripts/run_demo.sh` and `deploy/Dockerfile` |
| `AIS_MAX_VESSELS` | `200` | Per reload: vessels decoded from the parquet store (newest first), queue files read (5×), `scores` rows read (50×) |

The frontend loads once and refreshes on the "Reload data" button
(`POST /api/reload`). In bridge mode a reload reads the key columns of the
newest daily parquet file and then streams its `payload_json` column in
512-row batches, so late in a busy day it is a multi-second scan of a large
file; memory stays bounded, but don't wire it to a fast timer.

**Offline / air-gapped deployments**: the frontend vendors Leaflet locally
(`frontend/static/vendor/leaflet/`) specifically so the only remaining
external dependency is the OpenStreetMap basemap tile fetch in
`frontend/static/app.js`. For a fully offline demo, point the `L.tileLayer`
URL at a local tile server or a pre-downloaded `.mbtiles` server — no other
code in this repo makes outbound network calls.

## 7. Running the viewer

```bash
# Demo mode (no MCM_streaming checkout needed)
pip install -r backend/requirements.txt
python3 demo/generate_demo_data.py          # regenerate if you edited demo_data.py
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8090
# -> http://localhost:8090

# Bridge mode (MCM_streaming/serving already deployed/replaying next to this repo)
pip install -r backend/requirements-bridge.txt
AIS_DATA_MODE=bridge \
AIS_MODEL_DATA_DIR=/path/to/MCM_streaming/serving/runtime \
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8090
```

Or via Docker (`deploy/Dockerfile`, `deploy/docker-compose.yml`):

```bash
docker compose -f deploy/docker-compose.yml up --build
```
