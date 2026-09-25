"""benchmark: score predictors on an archived raw_ais set, offline and in-process.

Runs the recorded positions through the same tracker and reconcile referee as the live stack, but calls every
predictor synchronously on every window, so slow predictors (MCM-Net on a CPU) are compared on identical windows
without real-time pressure: nothing goes stale and nothing is missed. The live leaderboard remains the place
to judge real-time behaviour (coverage, latency).

``envship benchmark [--source fixtures/raw_ais] [--predictors cv,kalman,imm,mcmnet] [--max-windows N]``
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from envship_contracts import RawPosition, Score, Truth, Window, decode, streams

from .reconcile import Reconciler
from .replay import load
from .settings import Settings, get_settings
from .tracker import Tracker


class _Collector:
    """Captures what the tracker and reconciler would publish to Redis."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def xadd(self, topic, fields, **_):
        self.entries.append((topic, fields))

    def hset(self, *_, **__):
        pass

    def hdel(self, *_, **__):
        pass

    def take(self, topic: str, cls):
        return [decode(f, cls) for t, f in self.entries if t == topic]


def _mean(values) -> float:
    vals = [v for v in values if v is not None]
    return float(np.mean(vals)) if vals else float("nan")


def windows_and_truths(source: Path, s: Settings, log: logging.Logger) -> tuple[list[Window], list[Truth]]:
    table = load(source)
    cols = {
        c: table[c].to_pylist()
        for c in ("mmsi", "recv_ts", "lat", "lon", "sog", "cog", "heading", "nav_status", "ais_class", "feed")
    }
    trk = Tracker(s, r=None, log=log)
    out = _Collector()
    for i in range(len(cols["mmsi"])):
        trk.on_raw(
            RawPosition(
                mmsi=cols["mmsi"][i],
                feed=cols["feed"][i],
                recv_ts=cols["recv_ts"][i],
                ingest_wall=0.0,
                lat=cols["lat"][i],
                lon=cols["lon"][i],
                sog=cols["sog"][i],
                cog=cols["cog"][i],
                heading=cols["heading"][i],
                nav_status=cols["nav_status"][i],
                ais_class=cols["ais_class"][i] or "A",
                replay=True,
            ),
            out,
        )
    return out.take(streams.WINDOWS, Window), out.take(streams.TRUTH, Truth)


def _build(kind: str):
    from envship_predictors import REGISTRY

    if kind == "mcmnet":
        from envship_predictors.mcmnet import MCMNet

        return MCMNet()
    return REGISTRY[kind]()


def run(
    source: Path, kinds: list[str], max_windows: int | None, s: Settings, log: logging.Logger
) -> list[dict]:
    from envship_sdk import TileStore

    windows, truths = windows_and_truths(source, s, log)
    scored_ids = {t.window_id for t in truths}
    windows = [w for w in windows if w.window_id in scored_ids][:max_windows]
    truths = [t for t in truths if t.window_id in {w.window_id for w in windows}]
    log.info("%d windows with a realized future", len(windows))

    rec = Reconciler(s, r=None, log=log)
    rec.registry = {k: {"last_beat": time.time(), "required_tags": []} for k in kinds}
    for w in windows:
        rec.on_window(w)
    compute: dict[str, list[float]] = defaultdict(list)
    for kind in kinds:
        p = _build(kind)
        p.warmup(TileStore(None))
        pid = p.info().id
        for i, w in enumerate(windows):
            t0 = time.perf_counter()
            c = p.predict(w)
            compute[pid].append(time.perf_counter() - t0)
            c.predictor_id = pid
            rec.on_candidates(c)
            if (i + 1) % 200 == 0:
                log.info("%s: %d/%d windows", pid, i + 1, len(windows))
    out = _Collector()
    for t in truths:
        rec.on_truth(t, out)
    scores = out.take(streams.SCORES, Score)

    tags = {w.window_id: set(w.tags) for w in windows}
    rows = []
    for kind in kinds:
        pid = kind
        mine = [x for x in scores if x.predictor_id == pid and not x.miss and x.served_ade is not None]
        for stratum in ("all", "scene:straight", "scene:turning"):
            sel = [x for x in mine if stratum == "all" or stratum in tags[x.window_id]]
            if not sel:
                continue
            rows.append(
                {
                    "predictor": pid,
                    "stratum": stratum,
                    "n": len(sel),
                    "served_ade": _mean(x.served_ade for x in sel),
                    "best_of_k_ade": _mean(x.oracle_ade for x in sel),
                    "served_fde": _mean(x.served_fde for x in sel),
                    "compute_ms_p50": 1000 * float(np.median(compute[pid])) if compute[pid] else float("nan"),
                }
            )
    return rows


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser(prog="envship benchmark")
    ap.add_argument("--source", type=Path, default=s.replay_source)
    ap.add_argument("--predictors", default="cv,kalman,imm", help="comma-separated; add mcmnet to include it")
    ap.add_argument("--max-windows", type=int, default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log = logging.getLogger("envship.benchmark")
    rows = run(
        args.source, [k.strip() for k in args.predictors.split(",") if k.strip()], args.max_windows, s, log
    )
    print(
        f"\n{'predictor':<10} {'stratum':<16} {'n':>5} {'served ADE':>11} {'best-of-K':>10} {'FDE':>9} {'p50 ms':>8}"
    )
    for r in rows:
        print(
            f"{r['predictor']:<10} {r['stratum']:<16} {r['n']:>5} {r['served_ade']:>10.1f}m "
            f"{r['best_of_k_ade']:>9.1f}m {r['served_fde']:>8.1f}m {r['compute_ms_p50']:>8.1f}"
        )


if __name__ == "__main__":
    main()
