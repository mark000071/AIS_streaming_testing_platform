"""Live in-memory view of the platform, fed by a background thread reading the Redis streams."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque

import numpy as np
from envship_contracts import Candidates, HealthBeat, RawPosition, Score, Truth, Window, decode, streams
from envship_sdk import bus
from envship_sdk.geo import from_enu

log = logging.getLogger("envship.api.state")

MAX_WINDOWS = 20_000
VESSEL_TTL_S = 1800.0


def _latlon(xy: np.ndarray, lat0: float, lon0: float) -> list:
    lat, lon = from_enu(xy[..., 0], xy[..., 1], lat0, lon0)
    out = np.stack([lon, lat], axis=-1)  # deck.gl / GeoJSON order
    return nan_to_none(np.round(out, 6))


def nan_to_none(a: np.ndarray) -> list:
    obj = a.astype(object)
    obj[~np.isfinite(a)] = None
    return obj.tolist()


class LiveState:
    def __init__(self, redis_url: str):
        self.r = bus.connect(redis_url)
        self.lock = threading.Lock()
        self.seq = 0
        self.vessels: dict[int, dict] = {}
        self.vessel_seq: dict[int, int] = {}
        self.removed: deque[tuple[int, int]] = deque(maxlen=20_000)
        self.windows: OrderedDict[str, dict] = OrderedDict()
        self.vessel_windows: dict[int, deque[str]] = {}
        self.events: deque[tuple[int, dict]] = deque(maxlen=5_000)
        self.scores: deque[tuple[int, dict]] = deque(maxlen=2_000)
        self.health: dict[str, dict] = {}
        self.registry: dict[str, dict] = {}
        self.clock: dict[str, float] = {}
        self.stats = {"raw": 0, "windows": 0, "candidates": 0, "truths": 0, "scores": 0}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="live-state", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---- consumer ---------------------------------------------------------------------------------------
    def _run(self) -> None:
        cursors: dict[str, str] = {}
        last_registry = 0.0
        while not self._stop.is_set():
            try:
                now = time.time()
                if now - last_registry > 5:
                    self.registry = bus.registry(self.r)
                    wanted = [streams.raw(f) for f in streams.FEEDS] + [
                        streams.WINDOWS,
                        streams.TRUTH,
                        streams.SCORES,
                        streams.HEALTH,
                        *[streams.predictions(p) for p in self.registry],
                    ]
                    for t in wanted:
                        cursors.setdefault(t, "$")
                    last_registry = now
                    self._expire()
                for topic, entries in bus.read(self.r, cursors, count=2000, block=500):
                    with self.lock:
                        for _entry_id, fields in entries:
                            try:
                                self._apply(topic, fields)
                            except Exception:
                                log.exception("bad entry on %s", topic)
                    cursors[topic] = entries[-1][0]
            except Exception:
                log.exception("live-state loop error; retrying")
                time.sleep(1)

    def _bump(self) -> int:
        self.seq += 1
        return self.seq

    def _apply(self, topic: str, fields: dict) -> None:
        if topic.startswith("ais.raw."):
            p = decode(fields, RawPosition)
            self.vessels[p.mmsi] = {
                "mmsi": p.mmsi,
                "feed": p.feed,
                "lat": p.lat,
                "lon": p.lon,
                "sog": p.sog,
                "cog": p.cog,
                "ts": p.recv_ts,
                "wid": self.vessels.get(p.mmsi, {}).get("wid"),
            }
            self.vessel_seq[p.mmsi] = self._bump()
            self.clock[p.feed] = max(self.clock.get(p.feed, 0.0), p.recv_ts)
            self.stats["raw"] += 1
        elif topic == streams.WINDOWS:
            w = decode(fields, Window)
            self.windows[w.window_id] = {
                "window_id": w.window_id,
                "mmsi": w.mmsi,
                "feed": w.feed,
                "anchor_ts": w.anchor_ts,
                "anchor": [w.anchor_lon, w.anchor_lat],
                "tags": w.tags,
                "enqueue_wall": w.enqueue_wall,
                "history": [[round(lon, 6), round(lat, 6)] for lat, lon in np.asarray(w.abs_xy).tolist()],
                "candidates": {},
                "truth": None,
                "scores": {},
            }
            while len(self.windows) > MAX_WINDOWS:
                self.windows.popitem(last=False)
            dq = self.vessel_windows.setdefault(w.mmsi, deque(maxlen=6))
            dq.append(w.window_id)
            if w.mmsi in self.vessels:
                self.vessels[w.mmsi]["wid"] = w.window_id
                self.vessel_seq[w.mmsi] = self._bump()
            self.events.append((self._bump(), {"type": "window", "mmsi": w.mmsi, "window_id": w.window_id}))
            self.stats["windows"] += 1
        elif topic.startswith("predictions."):
            c = decode(fields, Candidates)
            win = self.windows.get(c.window_id)
            if win is None:
                return
            lon0, lat0 = win["anchor"]
            win["candidates"][c.predictor_id] = {
                "paths": _latlon(np.asarray(c.paths, dtype=np.float64), lat0, lon0),
                "scores": None
                if c.scores is None
                else np.round(np.asarray(c.scores, dtype=float), 4).tolist(),
                "selected": c.selected,
                "compute_ms": round(c.compute_ms, 2),
                "queue_ms": round((c.issue_wall - c.enqueue_wall) * 1000, 1),
                "model_revision": c.model_revision,
            }
            self.events.append(
                (
                    self._bump(),
                    {
                        "type": "candidates",
                        "mmsi": c.mmsi,
                        "window_id": c.window_id,
                        "predictor_id": c.predictor_id,
                    },
                )
            )
            self.stats["candidates"] += 1
        elif topic == streams.TRUTH:
            t = decode(fields, Truth)
            win = self.windows.get(t.window_id)
            if win is None:
                return
            lon0, lat0 = win["anchor"]
            win["truth"] = {
                "path": _latlon(np.asarray(t.future_xy, dtype=np.float64), lat0, lon0),
                "coverage": round(t.coverage, 3),
            }
            self.events.append((self._bump(), {"type": "truth", "mmsi": t.mmsi, "window_id": t.window_id}))
            self.stats["truths"] += 1
        elif topic == streams.SCORES:
            s = decode(fields, Score)
            d = s.model_dump(mode="json", exclude={"per_horizon"})
            win = self.windows.get(s.window_id)
            if win is not None:
                win["scores"][s.predictor_id] = d
            self.scores.append((self._bump(), d))
            self.stats["scores"] += 1
        elif topic == streams.HEALTH:
            h = decode(fields, HealthBeat)
            self.health[f"{h.service}@{h.instance}"] = h.model_dump(mode="json")

    def _expire(self) -> None:
        with self.lock:
            clock = max(self.clock.values(), default=0.0)
            dead = [m for m, v in self.vessels.items() if v["ts"] < clock - VESSEL_TTL_S]
            for m in dead:
                del self.vessels[m]
                self.vessel_seq.pop(m, None)
                self.removed.append((self._bump(), m))
            now = time.time()
            for k in [k for k, h in self.health.items() if now - h["ts"] > 60]:
                del self.health[k]

    # ---- readers (call with lock held) ---------------------------------------------------------------------
    @staticmethod
    def vessel_row(v: dict) -> list:
        return [
            v["mmsi"],
            round(v["lon"], 6),
            round(v["lat"], 6),
            v["sog"],
            v["cog"],
            v["ts"],
            v["wid"] is not None,
        ]

    def frame(self, since: int | None) -> dict:
        full = since is None
        if since is None:
            vessels = [self.vessel_row(v) for v in self.vessels.values()]
            removed: list[int] = []
            events: list[dict] = []
        else:
            vessels = [self.vessel_row(self.vessels[m]) for m, sq in self.vessel_seq.items() if sq > since]
            removed = [m for sq, m in self.removed if sq > since]
            events = [e for sq, e in self.events if sq > since]
        return {
            "seq": self.seq,
            "full": full,
            "clock": self.clock,
            "vessels": vessels,
            "removed": removed,
            "events": events[-500:],
            "stats": self.stats,
        }

    def scores_since(self, since: int) -> tuple[int, list[dict]]:
        return self.seq, [s for sq, s in self.scores if sq > since]

    def vessel(self, mmsi: int) -> dict | None:
        v = self.vessels.get(mmsi)
        wids = list(self.vessel_windows.get(mmsi, ()))
        windows = [self.windows[w] for w in reversed(wids) if w in self.windows]
        if v is None and not windows:
            return None
        return {"vessel": v, "windows": windows}
