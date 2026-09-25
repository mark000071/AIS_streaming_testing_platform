"""api: REST + SSE over the live state and the DuckDB archive snapshot (roadmap §4.4)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from ..settings import get_settings
from .query import QueryError, Snapshot, check_user_sql
from .state import LiveState, nan_to_none

log = logging.getLogger("envship.api")
s = get_settings()
state = LiveState(s.redis_url)
snapshot = Snapshot(s.data_dir)

WINDOWS = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "all": None}
STRATA = {
    "all": None,
    "comparable": ("comparable", True),
    "feed:fi": ("feed", "fi"),
    "feed:no": ("feed", "no"),
    "scene:turning": ("scene", "turning"),
    "scene:straight": ("scene", "straight"),
    "speed:slow": ("speed_band", "slow"),
    "speed:medium": ("speed_band", "medium"),
    "speed:fast": ("speed_band", "fast"),
}


def _refresher(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            snapshot.refresh()
        except Exception:
            log.exception("snapshot refresh failed")
        stop.wait(s.snapshot_refresh_s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    state.start()
    threading.Thread(target=_refresher, args=(stop,), daemon=True, name="snapshot").start()
    yield
    stop.set()
    state.stop()


app = FastAPI(title="EnvShip online prediction platform", version="0.1.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


def _clean(v):
    if isinstance(v, float) and not np.isfinite(v):
        return None
    if isinstance(v, np.floating):
        return None if not np.isfinite(v) else float(v)
    return v


def _window_bounds(window: str) -> tuple[str, list]:
    if window not in WINDOWS:
        raise HTTPException(400, f"window must be one of {list(WINDOWS)}")
    span = WINDOWS[window]
    if span is None:
        return "TRUE", []
    return "anchor_ts >= (SELECT max(anchor_ts) FROM scores) - ?", [span]


def _stratum(stratum: str) -> tuple[str, list]:
    if stratum not in STRATA:
        raise HTTPException(400, f"stratum must be one of {list(STRATA)}")
    spec = STRATA[stratum]
    if spec is None:
        return "TRUE", []
    return f"{spec[0]} = ?", [spec[1]]


def _bootstrap_ci(x: np.ndarray, reps: int = 400) -> tuple[float | None, float | None]:
    if len(x) < 2:
        return None, None
    rng = np.random.default_rng(0)
    n = min(len(x), 5000)
    means = np.array([rng.choice(x, n).mean() for _ in range(reps)])
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


# ---- REST ------------------------------------------------------------------------------------------------
@app.get("/v1/leaderboard")
def leaderboard(window: str = "1h", stratum: str = "all"):
    if not snapshot.has("scores"):
        return {"window": window, "stratum": stratum, "rows": [], "horizon_s": [], "latest_anchor": None}
    wc, wp = _window_bounds(window)
    sc, sp = _stratum(stratum)
    try:
        cols, rows, _ = snapshot.run(
            f"SELECT predictor_id, miss, served_ade, oracle_ade, served_fde, compute_ms, per_horizon, anchor_ts "
            f"FROM scores WHERE {wc} AND {sc}",
            wp + sp,
            max_rows=500_000,
        )
    except QueryError as e:
        raise HTTPException(503, str(e)) from e
    by: dict[str, list] = {}
    latest = None
    for r in rows:
        by.setdefault(r[0], []).append(r)
        latest = r[7] if latest is None else max(latest, r[7])
    out = []
    for pid, rs in by.items():
        scored = [r for r in rs if not r[1]]
        served = np.array([r[2] for r in scored], dtype=float)
        lo, hi = _bootstrap_ci(served)
        per_h = np.array([r[6] for r in scored if r[6] is not None], dtype=float)
        curve = np.nanmean(per_h, axis=0).tolist() if len(per_h) else []
        coverage = len(scored) / len(rs) if rs else 0.0
        out.append(
            {
                "predictor_id": pid,
                "n": len(rs),
                "n_scored": len(scored),
                "coverage": coverage,
                "served_ade": _clean(float(served.mean())) if len(served) else None,
                "ci95": [lo, hi],
                "oracle_ade": _clean(float(np.mean([r[3] for r in scored]))) if scored else None,
                "served_fde": _clean(float(np.mean([r[4] for r in scored]))) if scored else None,
                "compute_p50_ms": _clean(float(np.median([r[5] for r in scored]))) if scored else None,
                "per_horizon": [_clean(float(v)) for v in curve],
                "ranked": coverage >= 0.95 and len(scored) >= 20,
            }
        )
    out.sort(key=lambda r: (not r["ranked"], r["served_ade"] if r["served_ade"] is not None else 1e18))
    info = state.registry
    for r in out:
        r["description"] = info.get(r["predictor_id"], {}).get("description", "")
        r["k"] = info.get(r["predictor_id"], {}).get("k")
    return {
        "window": window,
        "stratum": stratum,
        "rows": out,
        "latest_anchor": latest,
        "horizon_s": [20 * (i + 1) for i in range(30)],
        "snapshot_at": snapshot.built_at,
    }


@app.get("/v1/leaderboard/timeseries")
def leaderboard_timeseries():
    if not snapshot.has("leaderboard_hourly"):
        return {"bucket_s": s.bucket_s, "series": {}}
    _, rows, _ = snapshot.run(
        "SELECT predictor_id, epoch(bucket), served_ade, n, coverage FROM leaderboard_hourly ORDER BY 2",
        max_rows=100_000,
    )
    series: dict[str, list] = {}
    for pid, b, ade, n, cov in rows:
        series.setdefault(pid, []).append([b * 1000, _clean(ade), n, _clean(cov)])
    return {"bucket_s": s.bucket_s, "series": series}


@app.get("/v1/sla")
def sla(feed: str = "fi", window: str = "1h"):
    if window not in WINDOWS:
        raise HTTPException(400, "bad window")
    result: dict = {"feed": feed, "window": window, "predictors": [], "series": {}, "budget_s": 5.0}
    if snapshot.has("predictions") and snapshot.has("windows"):
        span = WINDOWS[window] or 10**12
        _, rows, _ = snapshot.run(
            """
            WITH w AS (SELECT window_id, feed, list_contains(tags, 'replay') AS replay FROM windows WHERE feed = ?),
                 p AS (SELECT * FROM predictions JOIN w USING (window_id)
                       WHERE anchor_ts >= (SELECT max(anchor_ts) FROM predictions) - ?)
            SELECT predictor_id, count(*),
                   quantile_cont((issue_wall - enqueue_wall) * 1000, [0.5, 0.9, 0.99]),
                   quantile_cont(compute_ms, [0.5, 0.9, 0.99]),
                   avg(((issue_wall - enqueue_wall) <= 5)::INT),
                   bool_or(replay),
                   quantile_cont(enqueue_wall - anchor_ts, [0.5, 0.9, 0.99]),
                   quantile_cont(issue_wall - anchor_ts, [0.5, 0.9, 0.99])
            FROM p GROUP BY 1 ORDER BY 1""",
            [feed, span],
        )
        for pid, n, q, c, within, replay, fresh, service in rows:
            result["predictors"].append(
                {
                    "predictor_id": pid,
                    "n": n,
                    "queue_ms": q,
                    "compute_ms": c,
                    "within_budget": within,
                    "replay": replay,
                    "freshness_s": None if replay else fresh,
                    "service_s": None if replay else service,
                }
            )
    if snapshot.has("sla_hourly"):
        _, rows, _ = snapshot.run(
            "SELECT predictor_id, epoch(bucket), queue_p50_ms, queue_p99_ms, compute_p99_ms, "
            "within_budget FROM sla_hourly WHERE feed = ? ORDER BY 2",
            [feed],
        )
        for pid, b, q50, q99, c99, within in rows:
            result["series"].setdefault(pid, []).append([b * 1000, q50, q99, c99, within])
    return result


@app.get("/v1/health")
def health():
    with state.lock:
        beats = sorted(state.health.values(), key=lambda h: h["service"])
        return {
            "now": time.time(),
            "services": beats,
            "clock": state.clock,
            "stats": state.stats,
            "snapshot": {"built_at": snapshot.built_at, "tables": snapshot.tables},
        }


@app.get("/v1/predictors")
def predictors():
    now = time.time()
    return [
        {**info, "alive": now - info.get("last_beat", 0) <= s.predictor_alive_s}
        for info in sorted(state.registry.values(), key=lambda i: i["id"])
    ]


@app.get("/v1/vessels/{mmsi}/latest")
def vessel_latest(mmsi: int):
    with state.lock:
        v = state.vessel(mmsi)
        if v is None:
            raise HTTPException(404, "vessel not in live state")
        return json.loads(json.dumps(v))


@app.get("/v1/windows/{window_id}")
def window_detail(window_id: str):
    with state.lock:
        w = state.windows.get(window_id)
        if w is not None:
            return json.loads(json.dumps(w))
    return _window_from_archive(window_id)


def _window_from_archive(window_id: str):
    from envship_sdk.geo import from_enu

    base = Path(s.data_dir)
    if not (base / "windows").exists():
        raise HTTPException(404, "unknown window")
    con = duckdb.connect()
    src = lambda t: f"read_parquet('{base / t}/**/*.parquet', hive_partitioning = true, union_by_name = true)"  # noqa: E731
    row = con.execute(
        f"SELECT mmsi, feed, anchor_ts, anchor_lat, anchor_lon, abs_xy, tags FROM {src('windows')} "
        "WHERE window_id = ?",
        [window_id],
    ).fetchone()
    if row is None:
        raise HTTPException(404, "unknown window")
    mmsi, feed, anchor_ts, lat0, lon0, abs_xy, tags = row
    hist = np.asarray(abs_xy).reshape(-1, 2)

    def ll(flat, shape):
        a = np.asarray(flat, dtype=float).reshape(shape)
        lat, lon = from_enu(a[..., 0], a[..., 1], lat0, lon0)
        o = np.stack([lon, lat], -1)
        return nan_to_none(np.round(o, 6))

    out = {
        "window_id": window_id,
        "mmsi": mmsi,
        "feed": feed,
        "anchor_ts": anchor_ts,
        "anchor": [lon0, lat0],
        "tags": tags,
        "history": [[round(b, 6), round(a, 6)] for a, b in hist.tolist()],
        "candidates": {},
        "truth": None,
        "scores": {},
        "source": "archive",
    }
    if (base / "predictions").exists():
        for pid, k, paths, sc, sel, cms in con.execute(
            f"SELECT predictor_id, k, paths, scores, selected, compute_ms FROM {src('predictions')} "
            "WHERE window_id = ?",
            [window_id],
        ).fetchall():
            out["candidates"][pid] = {
                "paths": ll(paths, (k, 30, 2)),
                "scores": sc,
                "selected": sel,
                "compute_ms": cms,
            }
    if (base / "truth").exists():
        t = con.execute(
            f"SELECT future_xy, coverage FROM {src('truth')} WHERE window_id = ?", [window_id]
        ).fetchone()
        if t:
            out["truth"] = {"path": ll(t[0], (30, 2)), "coverage": t[1]}
    if (base / "scores").exists():
        cur = con.execute(
            f"SELECT * EXCLUDE (per_horizon) FROM {src('scores')} WHERE window_id = ?", [window_id]
        )
        names = [d[0] for d in cur.description or []]
        for r in cur.fetchall():
            d = {k: _clean(v) for k, v in zip(names, r, strict=True)}
            out["scores"][d["predictor_id"]] = d
    return out


class SqlBody(BaseModel):
    sql: str


@app.post("/v1/query")
def query(body: SqlBody):
    try:
        sql = check_user_sql(body.sql)
        cols, rows, truncated = snapshot.run(sql, timeout_s=s.query_timeout_s, max_rows=s.query_max_rows)
    except QueryError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "columns": cols,
        "rows": [[_clean(v) if isinstance(v, float) else v for v in r] for r in rows],
        "truncated": truncated,
        "tables": snapshot.tables,
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---- SSE ---------------------------------------------------------------------------------------------------
def _sse(event: str, seq: int, data: dict) -> str:
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, default=_clean)}\n\n"


@app.get("/stream/positions")
async def stream_positions(request: Request, frame_s: float = Query(None, ge=0.5, le=30)):
    period = frame_s or s.frame_s

    async def gen():
        since = None  # always open with a full snapshot, so a reconnect never loses state
        while not await request.is_disconnected():
            with state.lock:
                f = state.frame(since)
            since = f["seq"]
            yield _sse("frame", f["seq"], f)
            await asyncio.sleep(period)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/stream/scores")
async def stream_scores(request: Request):
    async def gen():
        with state.lock:
            backlog = list(state.scores)[-60:]
            since = (
                backlog[0][0] - 1 if backlog else state.seq
            )  # replay a short backlog so the page is never empty
        yield ": connected\n\n"
        while not await request.is_disconnected():
            with state.lock:
                seq, items = state.scores_since(since)
            since = seq
            for it in items:
                yield _sse("score", seq, it)
            await asyncio.sleep(1.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- static front-end (Caddy serves it in compose; this is for single-process dev) ------------------------
if s.web_dist and Path(s.web_dist).exists():
    dist = Path(s.web_dist).resolve()
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = dist / path
        if path and f.is_file() and dist in f.resolve().parents:
            return FileResponse(f)
        return FileResponse(dist / "index.html")
