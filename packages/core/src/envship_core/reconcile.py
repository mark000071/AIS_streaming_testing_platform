"""reconcile: the platform's single referee.

Remembers every eligible window and which predictors were expected to answer it, collects candidates,
and when ``truth.realized`` arrives scores every expected predictor with the same rules:
served ADE/FDE of the selected candidate, best-of-K (oracle) ADE, per-horizon error, and a miss when
no answer arrived within the 5 s budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from envship_contracts import Candidates, Score, Truth, Window, decode, streams
from envship_sdk import bus
from prometheus_client import Counter, Histogram

from .archive import ParquetSink, date_hour
from .service import Stopper, setup
from .settings import Settings, get_settings

GROUP = "reconcile"
M_SCORES = Counter("reconcile_scores_total", "scores emitted", ["predictor_id", "outcome"])
M_ADE = Histogram(
    "reconcile_served_ade_m",
    "served ADE (m)",
    ["predictor_id"],
    buckets=(25, 50, 100, 200, 400, 800, 1600, 3200),
)


@dataclass
class WindowMeta:
    mmsi: int
    feed: str
    anchor_ts: float
    enqueue_wall: float
    tags: list[str]
    expected: set[str]
    preds: dict[str, Candidates] = field(default_factory=dict)


def score_candidates(c: Candidates, truth: Truth) -> dict:
    valid = np.asarray(truth.valid, dtype=bool)
    fut = np.asarray(truth.future_xy, dtype=np.float64)
    paths = np.asarray(c.paths, dtype=np.float64)
    err = np.linalg.norm(paths - fut[None], axis=2)  # (K, 30), NaN where truth invalid
    ade_k = err[:, valid].mean(axis=1)
    sel = int(c.selected) if 0 <= c.selected < len(paths) else 0
    last = int(np.flatnonzero(valid)[-1])
    per_h = np.where(valid, err[sel], np.nan).astype(np.float32)
    k = int(c.oracle_k) if 0 < c.oracle_k <= len(paths) else len(paths)
    return {
        "served_ade": float(ade_k[sel]),
        "served_fde": float(err[sel, last]),
        "oracle_ade": float(ade_k[:k].min()),
        "per_horizon": per_h,
        "k": k,
    }


def _tag(tags: list[str], prefix: str, default: str) -> str:
    for t in tags:
        if t.startswith(prefix):
            return t[len(prefix) :]
    return default


class Reconciler:
    def __init__(self, s: Settings, r, log):
        self.s, self.r, self.log = s, r, log
        self.windows: dict[str, WindowMeta] = {}
        self.registry: dict[str, dict] = {}
        self.scores_sink = ParquetSink(s.data_dir, "scores", s.parquet_flush_s)
        self.pred_sink = ParquetSink(s.data_dir, "predictions", s.parquet_flush_s)
        self.counters: dict[str, float] = {}

    def refresh_registry(self) -> list[str]:
        self.registry = bus.registry(self.r)
        for pid in self.registry:
            bus.ensure_group(self.r, streams.predictions(pid), GROUP, start="0")
        return [streams.predictions(p) for p in self.registry]

    def alive(self, pid: str, now: float) -> bool:
        info = self.registry.get(pid)
        return info is not None and now - info.get("last_beat", 0) <= self.s.predictor_alive_s

    def on_window(self, w: Window) -> None:
        now = time.time()
        expected = {
            pid
            for pid, info in self.registry.items()
            if self.alive(pid, now) and set(info.get("required_tags", [])) <= set(w.tags)
        }
        self.windows[w.window_id] = WindowMeta(w.mmsi, w.feed, w.anchor_ts, w.enqueue_wall, w.tags, expected)

    def on_candidates(self, c: Candidates) -> None:
        meta = self.windows.get(c.window_id)
        if meta is not None:
            meta.preds[c.predictor_id] = c
        self.pred_sink.add(
            {
                "window_id": c.window_id,
                "mmsi": c.mmsi,
                "model_revision": c.model_revision,
                "anchor_ts": c.anchor_ts,
                "enqueue_wall": c.enqueue_wall,
                "issue_wall": c.issue_wall,
                "k": int(len(c.paths)),
                "paths": np.asarray(c.paths).ravel().tolist(),
                "scores": None if c.scores is None else np.asarray(c.scores).ravel().tolist(),
                "selected": c.selected,
                "compute_ms": c.compute_ms,
            },
            predictor_id=c.predictor_id,
            date=date_hour(c.anchor_ts)[0],
        )

    def on_truth(self, t: Truth, out) -> None:
        meta = self.windows.pop(t.window_id, None)
        if meta is None:
            self._count("truth_unknown_window")
            return
        if t.coverage < self.s.min_truth_coverage or not np.any(t.valid):
            self._count("truth_insufficient")
            return
        now = time.time()
        comparable = "benchmark" in meta.tags and t.coverage >= self.s.comparable_coverage
        common = {
            "window_id": t.window_id,
            "mmsi": meta.mmsi,
            "feed": meta.feed,
            "anchor_ts": meta.anchor_ts,
            "scored_wall": now,
            "comparable": comparable,
            "coverage": t.coverage,
            "scene": _tag(meta.tags, "scene:", "straight"),
            "speed_band": _tag(meta.tags, "speed:", "slow"),
        }
        for pid in sorted(meta.expected | set(meta.preds)):
            c = meta.preds.get(pid)
            queue_ms = None if c is None else (c.issue_wall - c.enqueue_wall) * 1000
            if c is None or queue_ms is None or queue_ms > streams.PREDICT_TIMEOUT_S * 1000:
                sc = Score(predictor_id=pid, miss=True, queue_ms=queue_ms, **common)
                M_SCORES.labels(pid, "miss").inc()
            else:
                sc = Score(
                    predictor_id=pid,
                    miss=False,
                    queue_ms=queue_ms,
                    compute_ms=c.compute_ms,
                    **score_candidates(c, t),
                    **common,
                )
                M_SCORES.labels(pid, "scored").inc()
                M_ADE.labels(pid).observe(sc.served_ade or 0)
            bus.publish(out, streams.SCORES, sc)
            row = sc.model_dump(exclude={"predictor_id", "per_horizon"})
            row["per_horizon"] = None if sc.per_horizon is None else np.asarray(sc.per_horizon).tolist()
            self.scores_sink.add(row, predictor_id=pid, date=date_hour(meta.anchor_ts)[0])
            self._count("scores")

    def expire(self, clock: float) -> None:
        cutoff = clock - 3 * streams.HORIZON_S
        stale = [wid for wid, m in self.windows.items() if m.anchor_ts < cutoff]
        for wid in stale:
            del self.windows[wid]
        if stale:
            self._count("windows_expired", len(stale))

    def _count(self, key: str, n: float = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n


def main() -> None:
    s = get_settings()
    log = setup("reconcile", s)
    r = bus.connect(s.redis_url)
    rec = Reconciler(s, r, log)
    for topic in (streams.WINDOWS, streams.TRUTH):
        bus.ensure_group(r, topic, GROUP, start="0")
    stop = Stopper()
    consumer = bus.instance_name()
    pred_topics = rec.refresh_registry()
    last_refresh = last_beat = time.time()
    clock = 0.0
    log.info("reconcile running; predictors: %s", sorted(rec.registry))
    while not stop.stopped:
        topics = {streams.WINDOWS: ">", **{t: ">" for t in pred_topics}, streams.TRUTH: ">"}
        by_topic = dict(bus.read_group(r, GROUP, consumer, topics, count=500, block=1000))
        out = r.pipeline(transaction=False)
        ordered = [streams.WINDOWS, *[t for t in by_topic if t.startswith("predictions.")], streams.TRUTH]
        for topic in ordered:
            entries = by_topic.get(topic, [])
            for entry_id, fields in entries:
                try:
                    if topic == streams.WINDOWS:
                        w = decode(fields, Window)
                        clock = max(clock, w.anchor_ts)
                        rec.on_window(w)
                    elif topic == streams.TRUTH:
                        rec.on_truth(decode(fields, Truth), out)
                    else:
                        rec.on_candidates(decode(fields, Candidates))
                except Exception:
                    log.exception("failed on %s %s", topic, entry_id)
            if entries:
                out.xack(topic, GROUP, *[e[0] for e in entries])
        out.execute()
        rec.scores_sink.maybe_flush()
        rec.pred_sink.maybe_flush()
        now = time.time()
        if now - last_refresh >= 5:
            pred_topics = rec.refresh_registry()
            rec.expire(clock)
            last_refresh = now
        if now - last_beat >= 5:
            lag = bus.group_lag(r, streams.WINDOWS, GROUP) + bus.group_lag(r, streams.TRUTH, GROUP)
            bus.beat(
                r,
                "reconcile",
                queue_depth=lag,
                last_event_ts=clock or None,
                counters={**rec.counters, "open_windows": float(len(rec.windows))},
            )
            last_beat = now
    rec.scores_sink.flush()
    rec.pred_sink.flush()


if __name__ == "__main__":
    main()
