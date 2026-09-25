"""Causal 20 s grid resampling and window feature construction, with a byte-parity self-check.

``python -m envship_core.features --write-golden`` regenerates the golden sample; services refuse to
start when the current environment does not reproduce it (train–serve byte-parity invariant).
"""

from __future__ import annotations

import argparse
import os
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from envship_contracts import streams
from envship_sdk.geo import KN, to_enu

GOLDEN = Path(
    os.environ.get("ENVSHIP_GOLDEN") or Path(__file__).resolve().parents[4] / "fixtures/golden/features.npz"
)


@dataclass
class Track:
    ts: list[float] = field(default_factory=list)
    lat: list[float] = field(default_factory=list)
    lon: list[float] = field(default_factory=list)
    sog: list[float] = field(default_factory=list)
    cog: list[float] = field(default_factory=list)

    def append(self, ts: float, lat: float, lon: float, sog: float | None, cog: float | None) -> None:
        self.ts.append(ts)
        self.lat.append(lat)
        self.lon.append(lon)
        self.sog.append(np.nan if sog is None else sog)
        self.cog.append(np.nan if cog is None else cog)

    def trim_before(self, t: float) -> None:
        i = bisect_right(self.ts, t)
        if i > 1:
            i -= 1  # keep one point before t so sampling at t still works
            for arr in (self.ts, self.lat, self.lon, self.sog, self.cog):
                del arr[:i]

    def as_lists(self) -> list[list[float]]:
        return [self.ts, self.lat, self.lon, self.sog, self.cog]

    @classmethod
    def from_lists(cls, data: list[list[float]]) -> Track:
        return cls(*[list(a) for a in data])


def causal_sample(tr: Track, t: float, max_age: float):
    """Last report at or before t, dead-reckoned to t. Returns (lat, lon, sog, cog, age) or None."""
    i = bisect_right(tr.ts, t) - 1
    if i < 0:
        return None
    age = t - tr.ts[i]
    if age > max_age:
        return None
    lat, lon, sog, cog = tr.lat[i], tr.lon[i], tr.sog[i], tr.cog[i]
    if age > 0 and np.isfinite(sog) and np.isfinite(cog):
        d = sog * KN * age
        c = np.radians(cog)
        lat = lat + np.degrees(d * np.cos(c) / 6371008.8)
        lon = lon + np.degrees(d * np.sin(c) / (6371008.8 * np.cos(np.radians(lat))))
    return lat, lon, sog, cog, age


def history(tr: Track, anchor: float, max_age: float) -> tuple[np.ndarray | None, str]:
    """(30, 5) samples [lat, lon, sog, cog, age] ending at ``anchor``, or None with the reason."""
    start = anchor - (streams.HIST_STEPS - 1) * streams.GRID_S
    if not tr.ts or tr.ts[0] > start:
        return None, "warmup"
    rows = []
    for k in range(streams.HIST_STEPS):
        s = causal_sample(tr, start + k * streams.GRID_S, max_age)
        if s is None:
            return None, "gap"
        rows.append(s)
    return np.array(rows, dtype=np.float64), "ok"


def build_tokens(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
    """tokens (30, 8) float32, abs_xy (30, 2) float64 [lat, lon], anchor lat, anchor lon."""
    lat, lon, sog, cog, age = (samples[:, i] for i in range(5))
    lat0, lon0 = float(lat[-1]), float(lon[-1])
    x, y = to_enu(lat, lon, lat0, lon0)
    dx = np.diff(x, prepend=np.nan)
    dy = np.diff(y, prepend=np.nan)
    dx[0], dy[0] = (dx[1], dy[1]) if len(dx) > 1 else (0.0, 0.0)
    derived_sog = np.hypot(dx, dy) / streams.GRID_S / KN
    derived_cog = np.degrees(np.arctan2(dx, dy)) % 360.0
    sog = np.where(np.isfinite(sog), sog, derived_sog)
    cog = np.where(np.isfinite(cog), cog, derived_cog)
    c = np.radians(cog)
    tokens = np.stack(
        [x, y, sog, cog, np.sin(c), np.cos(c), age, (age > streams.GRID_S / 2).astype(np.float64)], axis=1
    ).astype(np.float32)
    abs_xy = np.stack([lat, lon], axis=1)
    return tokens, abs_xy, lat0, lon0


def _golden_track() -> tuple[Track, float]:
    tr = Track()
    t0 = 1_780_000_000.0
    for i in range(200):
        t = t0 + i * 7.3
        heading = 40.0 + 25.0 * np.sin(i / 40.0)
        tr.append(t, 60.1 + i * 1.1e-4, 24.9 + i * 1.7e-4, 11.0 + 0.01 * i, heading if i % 9 else None)
    return tr, t0 + 1200.0


def compute_golden() -> dict[str, np.ndarray]:
    tr, anchor = _golden_track()
    samples, reason = history(tr, anchor, 60.0)
    assert samples is not None, reason
    tokens, abs_xy, _, _ = build_tokens(samples)
    return {"tokens": tokens, "abs_xy": abs_xy}


def parity_check(path: Path = GOLDEN) -> tuple[bool, str]:
    if not path.exists():
        return False, f"golden sample {path} missing"
    expected = np.load(path)
    got = compute_golden()
    if got["tokens"].tobytes() != expected["tokens"].tobytes():
        return False, "tokens differ from golden sample (byte parity broken)"
    err = float(np.max(np.abs(got["abs_xy"] - expected["abs_xy"])))
    err_m = err * 111_320.0
    if err_m > 1e-12:
        return False, f"abs_xy differs from golden by {err_m:.3e} m"
    return True, "ok"


def require_parity() -> None:
    ok, why = parity_check()
    if not ok:
        print(f"features parity check FAILED: {why}; refusing to start", file=sys.stderr)
        sys.exit(3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-golden", action="store_true")
    args = ap.parse_args()
    if args.write_golden:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        golden = compute_golden()
        np.savez(GOLDEN, tokens=golden["tokens"], abs_xy=golden["abs_xy"])
        print(f"wrote {GOLDEN}")
    ok, why = parity_check()
    print("parity:", "ok" if ok else why)
    sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
