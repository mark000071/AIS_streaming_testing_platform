"""Local east/north tangent-plane conversions (equirectangular; error < 0.1 % within 20 km)."""

import numpy as np

EARTH_R = 6371008.8
KN = 1852.0 / 3600.0


def to_enu(lat, lon, lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    lat = np.asarray(lat, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)
    x = np.radians(lon - lon0) * EARTH_R * np.cos(np.radians(lat0))
    y = np.radians(lat - lat0) * EARTH_R
    return x, y


def from_enu(x, y, lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    lat = lat0 + np.degrees(y / EARTH_R)
    lon = lon0 + np.degrees(x / (EARTH_R * np.cos(np.radians(lat0))))
    return lat, lon


def velocity_en(sog_kn: float, cog_deg: float) -> tuple[float, float]:
    v = sog_kn * KN
    c = np.radians(cog_deg)
    return float(v * np.sin(c)), float(v * np.cos(c))
