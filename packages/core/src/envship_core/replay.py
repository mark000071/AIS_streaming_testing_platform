"""replay: publish an archived ``raw_ais`` partition set to ``ais.raw.{feed}`` at N× speed.

Event timestamps are preserved (shifted by a constant per loop so a looping replay keeps moving forward),
so downstream services behave exactly as they do on the live feed.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.dataset as ds
from envship_contracts import RawPosition, streams
from envship_sdk import bus

from .service import Stopper, setup
from .settings import get_settings


def load(source: Path):
    dataset = ds.dataset(str(source), format="parquet", partitioning="hive")
    table = dataset.to_table()
    if "feed" not in table.column_names:
        raise SystemExit(f"{source} has no feed=... partitions")
    table = table.sort_by([("recv_ts", "ascending")])
    table = table.filter(pc.is_valid(table["lat"]))  # pyright: ignore[reportAttributeAccessIssue]
    return table


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=s.replay_source)
    ap.add_argument("--speed", type=float, default=s.replay_speed)
    ap.add_argument("--once", action="store_true", help="do not loop")
    args = ap.parse_args()
    log = setup("replay", s)
    r = bus.connect(s.redis_url)
    stop = Stopper()

    table = load(args.source)
    cols = {
        c: table[c].to_pylist()
        for c in ("mmsi", "recv_ts", "lat", "lon", "sog", "cog", "heading", "nav_status", "ais_class", "feed")
    }
    n = len(cols["mmsi"])
    t_first, t_last = cols["recv_ts"][0], cols["recv_ts"][-1]
    span = t_last - t_first
    log.info(
        "replaying %d positions (%.1f min of %s) at %.0fx",
        n,
        span / 60,
        sorted(set(cols["feed"])),
        args.speed,
    )

    offset = 0.0
    loop = 0
    published = 0.0
    last_beat = 0.0
    while not stop.stopped:
        wall0 = time.time()
        i = 0
        while i < n and not stop.stopped:
            now = time.time()
            event_now = t_first + (now - wall0) * args.speed
            pipe = r.pipeline(transaction=False)
            k = 0
            while i < n and cols["recv_ts"][i] <= event_now and k < 5000:
                msg = RawPosition(
                    mmsi=cols["mmsi"][i],
                    feed=cols["feed"][i],
                    recv_ts=cols["recv_ts"][i] + offset,
                    ingest_wall=now,
                    lat=cols["lat"][i],
                    lon=cols["lon"][i],
                    sog=cols["sog"][i],
                    cog=cols["cog"][i],
                    heading=cols["heading"][i],
                    nav_status=cols["nav_status"][i],
                    ais_class=cols["ais_class"][i] or "A",
                    replay=True,
                )
                bus.publish(pipe, streams.raw(msg.feed), msg)
                i += 1
                k += 1
            if k:
                pipe.execute()
                published += k
            if now - last_beat >= 5:
                bus.beat(
                    r,
                    "replay",
                    last_event_ts=event_now + offset,
                    counters={"published": published, "progress": i / n, "loop": loop, "speed": args.speed},
                )
                last_beat = now
            if k < 5000:
                time.sleep(0.05)
        if args.once:
            break
        loop += 1
        offset += span + 900.0
        log.info("replay loop %d done; restarting with event-time offset %.0fs", loop, offset)


if __name__ == "__main__":
    main()
