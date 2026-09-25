"""tracker (+ features): per-vessel state, causal 20 s grid, three-way eligibility, realized truth.

Consumes ``ais.raw.*``; publishes ``windows.eligible`` and, one horizon later, ``truth.realized``.
Event time comes from the messages, so the same code serves live feeds and N× replay.
"""

from __future__ import annotations

import heapq
import math
import time
from bisect import bisect_right
from dataclasses import dataclass, field

import msgpack
import numpy as np
from envship_contracts import RawPosition, Truth, Window, decode, streams
from envship_sdk import bus
from envship_sdk.geo import KN, to_enu
from prometheus_client import Counter, Gauge

from .archive import ParquetSink, date_hour
from .features import Track, build_tokens, history, require_parity
from .service import Stopper, setup
from .settings import Settings, get_settings

GROUP = "tracker"
MAX_SPEED_MS = 50 * KN

M_RAW = Counter("tracker_raw_total", "raw positions consumed", ["feed", "result"])
M_ELIG = Counter("tracker_eligibility_total", "eligibility decisions", ["feed", "outcome"])
M_TRUTH = Counter("tracker_truth_total", "truths published", ["feed"])
M_VESSELS = Gauge("tracker_vessels", "vessels in state", ["feed"])


@dataclass
class Vessel:
    track: Track = field(default_factory=Track)
    replay: bool = False
    ais_class: str = "A"


@dataclass
class FeedState:
    clock: float = 0.0
    next_tick: float = 0.0
    vessels: dict[int, Vessel] = field(default_factory=dict)
    pending: list[tuple[float, str]] = field(default_factory=list)
    meta: dict[str, dict] = field(default_factory=dict)


def _wrap(d: np.ndarray) -> np.ndarray:
    return (d + 180.0) % 360.0 - 180.0


def realize(
    tr: Track, anchor: float, lat0: float, lon0: float, max_gap: float
) -> tuple[np.ndarray, np.ndarray]:
    fut = np.full((streams.FUT_STEPS, 2), np.nan, dtype=np.float64)
    valid = np.zeros(streams.FUT_STEPS, dtype=bool)
    ts = tr.ts
    for k in range(streams.FUT_STEPS):
        t = anchor + (k + 1) * streams.GRID_S
        i = bisect_right(ts, t) - 1
        if i < 0:
            continue
        if ts[i] == t:
            lat, lon = tr.lat[i], tr.lon[i]
        elif i + 1 < len(ts) and ts[i + 1] - ts[i] <= max_gap:
            a = (t - ts[i]) / (ts[i + 1] - ts[i])
            lat = tr.lat[i] + a * (tr.lat[i + 1] - tr.lat[i])
            lon = tr.lon[i] + a * (tr.lon[i + 1] - tr.lon[i])
        else:
            continue
        x, y = to_enu(lat, lon, lat0, lon0)
        fut[k] = (float(x), float(y))
        valid[k] = True
    return fut, valid


class Tracker:
    def __init__(self, s: Settings, r, log):
        self.s, self.r, self.log = s, r, log
        self.feeds: dict[str, FeedState] = {f: FeedState() for f in streams.FEEDS}
        self.dirty: set[tuple[str, int]] = set()
        self.windows_sink = ParquetSink(s.data_dir, "windows", s.parquet_flush_s)
        self.truth_sink = ParquetSink(s.data_dir, "truth", s.parquet_flush_s)
        self.counters: dict[str, float] = {}
        self.last_state_flush = time.time()

    # ---- state persistence (Redis hash per MMSI, TTL 2 h) -------------------------------------------
    def _key(self, feed: str, mmsi: int) -> str:
        return f"trk:{feed}:{mmsi}"

    def restore(self) -> None:
        n = 0
        for feed, fs in self.feeds.items():
            clock = self.r.get(f"trk:{feed}:clock")
            if clock:
                fs.clock = float(clock)
                fs.next_tick = math.floor(fs.clock / streams.GRID_S) * streams.GRID_S + streams.GRID_S
            for key in self.r.scan_iter(f"trk:{feed}:[0-9]*"):
                data = self.r.hgetall(key)
                if not data:
                    continue
                mmsi = int(key.decode().rsplit(":", 1)[1])
                fs.vessels[mmsi] = Vessel(
                    Track.from_lists(msgpack.unpackb(data[b"pts"])),
                    data.get(b"replay") == b"1",
                    data.get(b"cls", b"A").decode(),
                )
                n += 1
            for wid, blob in self.r.hgetall(f"trk:pending:{feed}").items():
                meta = msgpack.unpackb(blob)
                fs.meta[wid.decode()] = meta
                heapq.heappush(fs.pending, (meta["due"], wid.decode()))
        if n:
            self.log.info("restored %d vessels from Redis", n)

    def flush_state(self) -> None:
        p = self.r.pipeline(transaction=False)
        for feed, mmsi in self.dirty:
            v = self.feeds[feed].vessels.get(mmsi)
            key = self._key(feed, mmsi)
            if v is None:
                p.delete(key)
                continue
            p.hset(
                key,
                mapping={
                    "pts": msgpack.packb(v.track.as_lists()),
                    "replay": "1" if v.replay else "0",
                    "cls": v.ais_class,
                },
            )
            p.expire(key, 7200)
        for feed, fs in self.feeds.items():
            if fs.clock:
                p.set(f"trk:{feed}:clock", fs.clock, ex=7200)
        p.execute()
        self.dirty.clear()
        self.last_state_flush = time.time()

    def reset_feed(self, feed: str) -> None:
        self.log.warning(
            "event time went backwards on feed %s (replay restarted?); resetting its state", feed
        )
        self.feeds[feed] = FeedState()
        keys = list(self.r.scan_iter(f"trk:{feed}:*"))
        if keys:
            self.r.delete(*keys)
        self.r.delete(f"trk:pending:{feed}")
        self.dirty = {d for d in self.dirty if d[0] != feed}

    # ---- ingestion -----------------------------------------------------------------------------------
    def on_raw(self, p: RawPosition, out) -> None:
        fs = self.feeds[p.feed]
        if fs.clock and p.recv_ts < fs.clock - 1800:
            self.reset_feed(p.feed)
            fs = self.feeds[p.feed]
        v = fs.vessels.get(p.mmsi)
        if v is None:
            v = fs.vessels[p.mmsi] = Vessel(replay=p.replay, ais_class=p.ais_class)
        tr = v.track
        if tr.ts:
            dt = p.recv_ts - tr.ts[-1]
            if dt <= 0:
                M_RAW.labels(p.feed, "out_of_order").inc()
                return
            x, y = to_enu(p.lat, p.lon, tr.lat[-1], tr.lon[-1])
            if math.hypot(float(x), float(y)) / dt > MAX_SPEED_MS and dt < 600:
                M_RAW.labels(p.feed, "teleport").inc()
                return
        sog = p.sog if p.sog is not None and p.sog < 102.2 else None
        cog = p.cog if p.cog is not None and p.cog < 360.0 else None
        tr.append(p.recv_ts, p.lat, p.lon, sog, cog)
        self.dirty.add((p.feed, p.mmsi))
        M_RAW.labels(p.feed, "ok").inc()

        if p.recv_ts > fs.clock:
            fs.clock = p.recv_ts
        if not fs.next_tick or fs.next_tick < fs.clock - 600:
            fs.next_tick = math.floor(fs.clock / streams.GRID_S) * streams.GRID_S + streams.GRID_S
        while fs.clock >= fs.next_tick:
            self.tick(p.feed, fs.next_tick, out)
            fs.next_tick += streams.GRID_S

    # ---- grid tick: windows + truths -------------------------------------------------------------------
    def tick(self, feed: str, anchor: float, out) -> None:
        s = self.s
        fs = self.feeds[feed]
        step = int(anchor // streams.GRID_S)
        now = time.time()
        for mmsi, v in list(fs.vessels.items()):
            if not v.track.ts or v.track.ts[-1] < anchor - s.state_keep_s:
                del fs.vessels[mmsi]
                self.dirty.add((feed, mmsi))
                continue
            if (step + mmsi) % s.window_stride_steps:
                continue
            v.track.trim_before(anchor - s.state_keep_s)
            if v.track.ts[-1] < anchor - s.max_report_age_s:
                continue  # vessel silent: not a candidate at all
            samples, reason = history(v.track, anchor, s.max_report_age_s)
            if samples is None:
                self._count(feed, reason)
                continue
            sog = samples[:, 2]
            span_s = (streams.HIST_STEPS - 1) * streams.GRID_S
            mean_sog = (
                float(np.nanmean(sog))
                if np.isfinite(sog).any()
                else self._displacement(samples) / span_s / KN
            )
            if mean_sog < s.min_mean_sog_kn and self._displacement(samples) < 300:
                self._count(feed, "stationary")
                continue
            self._count(feed, "eligible")
            tokens, abs_xy, lat0, lon0 = build_tokens(samples)
            window_id = f"{feed}-{mmsi}-{int(anchor)}"
            tags = self._tags(feed, v, tokens, samples)
            w = Window(
                window_id=window_id,
                mmsi=mmsi,
                feed=feed,
                anchor_ts=anchor,
                enqueue_wall=now,
                anchor_lat=lat0,
                anchor_lon=lon0,
                tokens=tokens,
                abs_xy=abs_xy,
                geom=np.zeros(streams.GEOM_DIM, dtype=np.float32),
                tags=tags,
            )
            bus.publish(out, streams.WINDOWS, w)
            d, h = date_hour(anchor)
            self.windows_sink.add(
                {
                    "window_id": window_id,
                    "mmsi": mmsi,
                    "feed": feed,
                    "anchor_ts": anchor,
                    "enqueue_wall": now,
                    "anchor_lat": lat0,
                    "anchor_lon": lon0,
                    "abs_xy": abs_xy.ravel().tolist(),
                    "tokens": tokens.ravel().tolist(),
                    "tags": tags,
                    "protocol_version": w.protocol_version,
                },
                date=d,
                hour=h,
            )
            meta = {
                "mmsi": mmsi,
                "anchor": anchor,
                "lat0": lat0,
                "lon0": lon0,
                "due": anchor + streams.HORIZON_S + s.truth_grace_s,
            }
            fs.meta[window_id] = meta
            heapq.heappush(fs.pending, (meta["due"], window_id))
            out.hset(f"trk:pending:{feed}", window_id, msgpack.packb(meta))
        while fs.pending and fs.pending[0][0] <= anchor:
            _, window_id = heapq.heappop(fs.pending)
            meta = fs.meta.pop(window_id, None)
            if meta is not None:
                self._publish_truth(feed, window_id, meta, out)
        M_VESSELS.labels(feed).set(len(fs.vessels))

    def _publish_truth(self, feed: str, window_id: str, meta: dict, out) -> None:
        v = self.feeds[feed].vessels.get(meta["mmsi"])
        if v is None:
            fut = np.full((streams.FUT_STEPS, 2), np.nan)
            valid = np.zeros(streams.FUT_STEPS, dtype=bool)
        else:
            fut, valid = realize(v.track, meta["anchor"], meta["lat0"], meta["lon0"], self.s.truth_max_gap_s)
        coverage = float(valid.mean())
        t = Truth(
            window_id=window_id,
            mmsi=meta["mmsi"],
            feed=feed,
            anchor_ts=meta["anchor"],
            future_xy=fut.astype(np.float32),
            valid=valid,
            coverage=coverage,
        )
        bus.publish(out, streams.TRUTH, t)
        out.hdel(f"trk:pending:{feed}", window_id)
        self.truth_sink.add(
            {
                "window_id": window_id,
                "mmsi": meta["mmsi"],
                "feed": feed,
                "anchor_ts": meta["anchor"],
                "future_xy": fut.astype(np.float32).ravel().tolist(),
                "valid": valid.tolist(),
                "coverage": coverage,
                "fill_method": "linear",
            },
            date=date_hour(meta["anchor"])[0],
        )
        M_TRUTH.labels(feed).inc()
        self.counters["truths"] = self.counters.get("truths", 0) + 1

    def _displacement(self, samples: np.ndarray) -> float:
        x, y = to_enu(samples[0, 0], samples[0, 1], samples[-1, 0], samples[-1, 1])
        return float(math.hypot(float(x), float(y)))

    def _tags(self, feed: str, v: Vessel, tokens: np.ndarray, samples: np.ndarray) -> list[str]:
        s = self.s
        tags = [f"feed:{feed}", "moving", f"class:{v.ais_class}"]
        heading_change = float(np.max(np.abs(_wrap(tokens[-10:, 3] - tokens[-10, 3]))))
        tags.append("scene:turning" if heading_change > s.turn_threshold_deg else "scene:straight")
        sog = float(tokens[-1, 2])
        tags.append("speed:fast" if sog >= 16 else "speed:medium" if sog >= 8 else "speed:slow")
        if float(np.max(samples[:, 4])) <= s.benchmark_max_age_s:
            tags.append("benchmark")
        if v.replay:
            tags.append("replay")
        return tags

    def _count(self, feed: str, outcome: str) -> None:
        M_ELIG.labels(feed, outcome).inc()
        self.counters[outcome] = self.counters.get(outcome, 0) + 1


def main() -> None:
    s = get_settings()
    log = setup("tracker", s)
    require_parity()
    r = bus.connect(s.redis_url)
    topics = [streams.raw(f) for f in streams.FEEDS]
    for t in topics:
        bus.ensure_group(r, t, GROUP, start="0")
    trk = Tracker(s, r, log)
    trk.restore()
    stop = Stopper()
    consumer = bus.instance_name()
    last_beat = 0.0
    log.info(
        "tracker running (stride %d steps, max report age %.0fs)", s.window_stride_steps, s.max_report_age_s
    )
    while not stop.stopped:
        resp = bus.read_group(r, GROUP, consumer, {t: ">" for t in topics}, count=1000, block=1000)
        out = r.pipeline(transaction=False)
        for topic, entries in resp:
            ids = []
            for entry_id, fields in entries:
                ids.append(entry_id)
                try:
                    trk.on_raw(decode(fields, RawPosition), out)
                except Exception:
                    log.exception("bad raw entry %s", entry_id)
            if ids:
                out.xack(topic, GROUP, *ids)
        out.execute()
        trk.windows_sink.maybe_flush()
        trk.truth_sink.maybe_flush()
        now = time.time()
        if now - trk.last_state_flush >= s.state_flush_s:
            trk.flush_state()
        if now - last_beat >= 5:
            lag = sum(bus.group_lag(r, t, GROUP) for t in topics)
            clocks = {f"clock_{f}": fs.clock for f, fs in trk.feeds.items() if fs.clock}
            vessels = {f"vessels_{f}": float(len(fs.vessels)) for f, fs in trk.feeds.items()}
            bus.beat(
                r,
                "tracker",
                queue_depth=lag,
                parity_ok=True,
                last_event_ts=max([fs.clock for fs in trk.feeds.values()] or [0]) or None,
                counters={**trk.counters, **clocks, **vessels},
            )
            last_beat = now
    trk.windows_sink.flush()
    trk.truth_sink.flush()
    trk.flush_state()


if __name__ == "__main__":
    main()
