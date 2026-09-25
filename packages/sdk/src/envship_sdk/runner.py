"""Container runner: subscribe to ``windows.eligible``, call the predictor, publish candidates.

Environment: REDIS_URL, PREDICTOR_ID (defaults to info().id), TILE_DIR.
Replicas of one predictor share a consumer group, so windows are split between them.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import signal
import time

from envship_contracts import Window, decode, streams

from . import bus
from .predictor import Predictor, TileStore

log = logging.getLogger("envship.sdk")

HEARTBEAT_S = 5.0


def _register(r, pid: str, info_json: dict) -> None:
    info_json = {**info_json, "id": pid, "last_beat": time.time()}
    r.hset(streams.PREDICTOR_REGISTRY, pid, json.dumps(info_json))


def run_predictor(
    predictor: Predictor,
    *,
    redis_url: str | None = None,
    predictor_id: str | None = None,
    max_windows: int | None = None,
) -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
    info = predictor.info()
    pid = predictor_id or os.environ.get("PREDICTOR_ID") or info.id
    r = bus.connect(redis_url)
    predictor.warmup(TileStore(os.environ.get("TILE_DIR")))

    group = f"pred:{pid}"
    consumer = bus.instance_name()
    bus.ensure_group(r, streams.WINDOWS, group)
    info_json = {**info.model_dump(), "started_wall": time.time()}
    _register(r, pid, info_json)
    out_topic = streams.predictions(pid)
    log.info("predictor %s (%s, K=%d) listening as %s", pid, info.version, info.k, consumer)

    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    pool = cf.ThreadPoolExecutor(max_workers=1)
    counters = {"predicted": 0.0, "timeouts": 0.0, "stale": 0.0, "skipped_tags": 0.0, "errors": 0.0}
    last_beat = 0.0
    handled = 0
    cursor = "0"  # first drain our own pending entries after a restart, then new ones
    while not stop:
        now = time.time()
        if now - last_beat >= HEARTBEAT_S:
            _register(r, pid, info_json)
            bus.beat(
                r, f"predictor:{pid}", queue_depth=bus.group_lag(r, streams.WINDOWS, group), counters=counters
            )
            last_beat = now
        resp = bus.read_group(r, group, consumer, {streams.WINDOWS: cursor}, count=16, block=1000)
        entries = resp[0][1] if resp else []
        if cursor == "0" and not entries:
            cursor = ">"
            continue
        for entry_id, fields in entries:
            try:
                w = decode(fields, Window)
            except Exception:
                log.exception("undecodable window %s", entry_id)
                r.xack(streams.WINDOWS, group, entry_id)
                continue
            if info.required_tags and not set(info.required_tags) <= set(w.tags):
                counters["skipped_tags"] += 1
            elif time.time() - w.enqueue_wall > streams.PREDICT_TIMEOUT_S:
                counters["stale"] += 1
            else:
                t0 = time.perf_counter()
                fut = pool.submit(predictor.predict, w)
                try:
                    c = fut.result(timeout=streams.PREDICT_TIMEOUT_S)
                except cf.TimeoutError:
                    counters["timeouts"] += 1
                    log.warning(
                        "window %s timed out after %.1fs (counted as miss)",
                        w.window_id,
                        streams.PREDICT_TIMEOUT_S,
                    )
                    pool.shutdown(wait=False, cancel_futures=True)
                    pool = cf.ThreadPoolExecutor(max_workers=1)
                    c = None
                except Exception:
                    counters["errors"] += 1
                    log.exception("predict failed for %s", w.window_id)
                    c = None
                if c is not None:
                    c.window_id = w.window_id
                    c.predictor_id = pid
                    c.model_revision = c.model_revision or info.version
                    c.mmsi = w.mmsi
                    c.anchor_ts = w.anchor_ts
                    c.enqueue_wall = w.enqueue_wall
                    c.compute_ms = (time.perf_counter() - t0) * 1000
                    c.issue_wall = time.time()
                    bus.publish(r, out_topic, c)
                    counters["predicted"] += 1
            r.xack(streams.WINDOWS, group, entry_id)
            handled += 1
            if max_windows is not None and handled >= max_windows:
                stop = True
                break
        if cursor == "0" and entries:
            cursor = entries[-1][0]
    pool.shutdown(wait=False, cancel_futures=True)
    log.info("predictor %s stopped", pid)
