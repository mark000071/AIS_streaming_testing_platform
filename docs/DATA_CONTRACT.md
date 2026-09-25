# Data contract with MCM_streaming

This repository is the **presentation layer** (呈现端) for MCM-Net vessel
trajectory predictions. The **model / serving layer** (模型端) is
[`MCM_streaming`](https://github.com/mark000071/MCM_streaming) — a separate
repository that this project never modifies, vendors, or imports code from.
The two are combined purely through the on-disk record formats
`MCM_streaming/serving/` already writes. This document is the interface
contract; if `MCM_streaming/serving/` ever changes these shapes, this file
(and `backend/app/data_source.py` / `backend/app/schemas.py`) are what to
update on this side.

## 1. Prediction record

Written by `MCM_streaming/serving/aisstream/predict/server.py::process_job`
as one JSON file per forecast in `queue/predictions/<job_id>.json`. The
collector (`predict/collect.py`, run every main-loop tick by `run_live.py`)
then moves each record into `predictions/YYYY-MM-DD.parquet` (column
`payload_json`, alongside `job_id`, `mmsi`, `anchor_ts`, …) and deletes the
JSON. Bridge mode therefore reads **the newest daily parquet file** as its
primary source (latest forecast per vessel; needs `pyarrow`, see
`backend/requirements-bridge.txt`) and adds whatever is still sitting in the
queue. Both carry the same record:

```jsonc
{
  "job_id": "…",
  "meta": {
    "mmsi": 230123456, "source": "digitraffic" | "kystdatahuset",
    "ais_class": "A" | "B" | null, "unified_class": "cargo" | …,
    "anchor_ts": 1785000000.0, "anchor_lat": 60.17, "anchor_lon": 24.94,
    "anchor_sog_kn": 14.5, "run_label": "pilot_combined_scorer"
  },
  "model_version": "model_ae_2026-07-19",
  "issued_ts": 1785000000.31,
  "e2e_latency_s": 2.1, "worker_latency_s": 0.34, "mcmnet_latency_s": 0.31,
  "cv":     { "xy": [[…]], "lat": [...30], "lon": [...30] },
  "kalman": { "xy": [[…]], "lat": [...30], "lon": [...30] },
  "mcmnet": {
    "xy": [[…20 x 30 x 2…]], "lat": [[…20…]], "lon": [[…20…]],
    "top1_index": 0,
    "scorer": { "index": 0, "alpha": 0.1, "xy": [[…]], "lat": [...], "lon": [...] },
    "routed": { "mode": "straight" | "maneuver", "source": "cv" | "kalman" | "scorer",
               "xy": [[…]], "lat": [...], "lon": [...] },
    "routed_cvkal": { … ablation comparator, see server.py }
  }
}
```

`cv`/`kalman` are always present; `mcmnet` (and its `scorer`/`routed` sub-blocks)
are present only when the corresponding stage was `enabled: true` in
`config/deployment.yaml` on the model side. This repo's `PredictionDetail`
schema (`backend/app/schemas.py`) treats all of `mcmnet`/`scorer`/`routed` as
optional for exactly this reason.

There is **no history array** in the real record (the predict worker doesn't
echo its input window back out) and **no ground truth** (that arrives later,
via reconcile). The demo dataset adds a synthetic `history` block purely so
the map has something to draw before the anchor; bridge mode falls back to a
single-point history (the anchor) when it's absent, which is the expected
case against a real feed.

## 2. Reconciled score row (`metrics/metrics.sqlite`, table `scores`)

Written by `MCM_streaming/serving/aisstream/reconcile/scoring.py` +
`metrics/aggregate.py::MetricsDB`, ~10 minutes after each forecast once the
realized track lands. The `payload_json` column is what this repo reads
(via stdlib `sqlite3`, no MCM code imported):

```jsonc
{
  "job_id": "…", "mmsi": 230123456, "source": "…", "ship_class": "…",
  "geo_stratum": "in_domain" | "norway_ood" | "finland_ood",
  "cv_ade": 191.3, "cv_fde": …, "kalman_ade": …, "kalman_fde": …,
  "mcmnet_top1_ade": …, "mcmnet_best20_ade": …,
  "routed_ade": …, "routed_fde": …,
  "routed_cvkal_ade": …, "routed_cvkal_fde": …,
  "delta_ade_routed_minus_cv": …,
  "delta_ade_routed_minus_kalman": …,
  "delta_ade_mcmnet_minus_cv": …
}
```

The metrics panel averages these per row. Over the paper's 15.5-day window
the means were −14.6 m (`delta_ade_routed_minus_cv`) and −11.4 m
(`delta_ade_routed_minus_kalman`); MCM-Net's own marginal contribution,
−0.9 m, is `routed_ade − routed_cvkal_ade` (same routing decision with CV
on the maneuver leg), which the panel derives. `delta_ade_mcmnet_minus_cv`
is the raw first candidate against CV, before scoring/routing.

Field names are frozen by `scoring.py` on the model side;
`backend/app/metrics.py` (`_DELTAS`) skips any that are absent, so a
deployment without the scorer/router stages shows fewer rows rather than
failing, and one with no reconciled rows yet shows a latency-only summary.

## 3. What this repo guarantees NOT to do

- No import of, or dependency on, any Python module under `MCM_streaming/`.
- No write access to `MCM_streaming/serving/runtime/` — read-only.
- No assumption that a live feed is present: every code path here has a
  demo-data fallback, since the actual weights and live AIS feeds are not
  available in every environment this presentation layer runs in (e.g. a
  laptop demo, a CI job, a reviewer's sandbox).
