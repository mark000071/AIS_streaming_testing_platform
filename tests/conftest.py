from __future__ import annotations

import math

import pytest
from envship_contracts import RawPosition, decode


class FakePipe:
    """Stands in for a Redis pipeline: records XADDs, ignores state writes."""

    def __init__(self):
        self.entries: list[tuple[str, dict]] = []

    def xadd(self, topic, fields, **_):
        self.entries.append((topic, fields))

    def hset(self, *a, **k):
        pass

    def hdel(self, *a, **k):
        pass

    def decoded(self, topic: str, cls):
        return [decode(f, cls) for t, f in self.entries if t == topic]


def straight_track(
    mmsi: int,
    t0: float,
    minutes: float,
    sog_kn: float,
    cog_deg: float,
    every_s: float = 10.0,
    lat0: float = 60.0,
    lon0: float = 24.0,
    turn_deg_s: float = 0.0,
):
    """Synthetic vessel moving at constant speed (optionally turning at a constant rate)."""
    out = []
    lat, lon, cog = lat0, lon0, cog_deg
    v = sog_kn * 1852 / 3600
    n = int(minutes * 60 / every_s)
    for i in range(n):
        out.append(
            RawPosition(
                mmsi=mmsi,
                feed="fi",
                recv_ts=t0 + i * every_s,
                ingest_wall=0.0,
                lat=lat,
                lon=lon,
                sog=sog_kn,
                cog=cog % 360,
            )
        )
        d = v * every_s
        c = math.radians(cog)
        lat += math.degrees(d * math.cos(c) / 6371008.8)
        lon += math.degrees(d * math.sin(c) / (6371008.8 * math.cos(math.radians(lat))))
        cog += turn_deg_s * every_s
    return out


@pytest.fixture
def pipe():
    return FakePipe()
