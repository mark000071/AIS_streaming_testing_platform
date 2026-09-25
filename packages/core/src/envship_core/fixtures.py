"""Build a ``raw_ais`` replay fixture from a JSONL recording of the Digitraffic feed.

    python -m envship_core.fixtures recording.jsonl fixtures/raw_ais [--minutes 60]

Each JSONL line: {"mmsi", "recv_ms", "lat", "lon", "sog", "cog", "heading", "nav_status"}.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .archive import ParquetSink, date_hour


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--feed", default="fi")
    args = ap.parse_args()

    rows = []
    for line in args.jsonl.read_text().splitlines():
        d = json.loads(line)
        if not 100_000_000 <= d["mmsi"] <= 999_999_999:
            continue
        rows.append(d)
    rows.sort(key=lambda d: d["recv_ms"])
    # The first polls return each vessel's latest (possibly stale) position; start once the stream is steady.
    t0 = rows[0]["wall_ms"] + 30_000 if "wall_ms" in rows[0] else rows[0]["recv_ms"]
    t_end = t0 + args.minutes * 60_000
    sink = ParquetSink(args.out.parent, "raw_ais", max_rows=10**9)
    sink.dir = args.out
    seen = set()
    n = 0
    for d in rows:
        if not t0 <= d["recv_ms"] < t_end or (d["mmsi"], d["recv_ms"]) in seen:
            continue
        seen.add((d["mmsi"], d["recv_ms"]))
        ts = d["recv_ms"] / 1000.0
        day, hour = date_hour(ts)
        sink.add(
            {
                "mmsi": d["mmsi"],
                "recv_ts": ts,
                "ingest_wall": d.get("wall_ms", d["recv_ms"]) / 1000.0,
                "lat": d["lat"],
                "lon": d["lon"],
                "sog": d["sog"],
                "cog": d["cog"],
                "heading": d.get("heading"),
                "nav_status": d.get("nav_status"),
                "ais_class": "A",
            },
            feed=args.feed,
            date=day,
            hour=hour,
        )
        n += 1
    sink.flush()
    print(f"wrote {n} positions from {len({d['mmsi'] for d in rows})} vessels to {args.out}")


if __name__ == "__main__":
    main()
