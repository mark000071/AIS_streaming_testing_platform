from __future__ import annotations

import numpy as np
from envship_contracts import streams
from envship_sdk import PredictorInfo, TileStore, Window, make_candidates
from envship_sdk.geo import velocity_en

from .filters import KalmanFilter, gaussian_likelihood, initial_state, transition

DT = float(streams.GRID_S)
STEPS = np.arange(1, streams.FUT_STEPS + 1, dtype=np.float64)[:, None]


def _measurement_scale(tokens: np.ndarray) -> np.ndarray:
    # Dead-reckoned history steps are trusted less than steps backed by a fresh report.
    return np.where(tokens[:, 7] > 0.5, 4.0, 1.0)


class ConstantVelocity:
    """Dead reckoning from the anchor's last reported SOG/COG."""

    def info(self) -> PredictorInfo:
        return PredictorInfo(id="cv", version="1.0.0", k=1, description="Constant velocity from last SOG/COG")

    def warmup(self, tiles: TileStore) -> None:
        pass

    def predict(self, w: Window):
        x, y, sog, cog = (float(v) for v in w.tokens[-1, :4])
        vx, vy = velocity_en(sog, cog)
        path = np.array([x, y]) + STEPS * DT * np.array([vx, vy])
        return make_candidates(w, path)


class Kalman:
    """CV Kalman filter over the 30-step history; K=3 (mean, and ±1σ heading)."""

    def __init__(self, sigma_a: float = 0.03, sigma_z: float = 8.0):
        self.kf = KalmanFilter(DT, sigma_a, sigma_z)

    def info(self) -> PredictorInfo:
        return PredictorInfo(
            id="kalman", version="1.0.0", k=3, description="Constant-velocity Kalman filter, ±1σ heading fan"
        )

    def warmup(self, tiles: TileStore) -> None:
        pass

    def filter(self, tokens: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        x, P = initial_state(tokens)
        scale = _measurement_scale(tokens)
        for k in range(1, len(tokens)):
            x, P = self.kf.predict(x, P)
            x, P, _, _ = self.kf.update(x, P, tokens[k, :2].astype(np.float64), scale[k])
        return x, P

    def predict(self, w: Window):
        x, P = self.filter(w.tokens)
        mean = self.kf.rollout(x, streams.FUT_STEPS)
        vx, vy = x[2], x[3]
        speed2 = vx * vx + vy * vy
        if speed2 > 1e-6:
            j = np.array([-vy, vx]) / speed2
            sigma_heading = float(np.sqrt(max(j @ P[2:, 2:] @ j, 1e-8)))
        else:
            sigma_heading = 0.0
        sigma_heading = min(max(sigma_heading, np.radians(2.0)), np.radians(30.0))
        paths = [mean]
        for sign in (1, -1):
            c, s = np.cos(sign * sigma_heading), np.sin(sign * sigma_heading)
            xr = x.copy()
            xr[2], xr[3] = c * vx - s * vy, s * vx + c * vy
            paths.append(self.kf.rollout(xr, streams.FUT_STEPS))
        return make_candidates(w, np.stack(paths), scores=np.array([1.0, 0.5, 0.5]), selected=0)


class IMM:
    """Interacting Multiple Model: CV + coordinated-turn left/right. Candidates are per-mode forecasts,
    scored by posterior mode probability; the served candidate is the most probable mode."""

    def __init__(
        self,
        turn_rate_deg_s: float = 0.4,
        p_stay: float = 0.92,
        sigma_a: float = 0.03,
        sigma_z: float = 8.0,
        turn_decay_s: float = 90.0,
    ):
        w = np.radians(turn_rate_deg_s)
        self.omegas = (0.0, w, -w)
        self.turn_decay_s = turn_decay_s
        self.filters = [KalmanFilter(DT, sigma_a, sigma_z, omega) for omega in self.omegas]
        m = len(self.filters)
        self.PI = np.full((m, m), (1 - p_stay) / (m - 1))
        np.fill_diagonal(self.PI, p_stay)

    def info(self) -> PredictorInfo:
        return PredictorInfo(
            id="imm",
            version="1.1.0",
            k=3,
            description="IMM (CV, CT ±0.4°/s decaying over 90 s); served = most probable mode",
        )

    def warmup(self, tiles: TileStore) -> None:
        pass

    def predict(self, w: Window):
        tokens = w.tokens
        x0, P0 = initial_state(tokens)
        m = len(self.filters)
        xs = [x0.copy() for _ in range(m)]
        Ps = [P0.copy() for _ in range(m)]
        mu = np.array([0.8, 0.1, 0.1])
        scale = _measurement_scale(tokens)
        for k in range(1, len(tokens)):
            c = self.PI.T @ mu
            mix = (self.PI * mu[:, None]) / np.maximum(c[None, :], 1e-300)
            x_mixed, P_mixed = [], []
            for j in range(m):
                xj = sum(mix[i, j] * xs[i] for i in range(m))
                Pj = sum(mix[i, j] * (Ps[i] + np.outer(xs[i] - xj, xs[i] - xj)) for i in range(m))
                x_mixed.append(xj)
                P_mixed.append(Pj)
            like = np.empty(m)
            z = tokens[k, :2].astype(np.float64)
            for j, f in enumerate(self.filters):
                xp, Pp = f.predict(x_mixed[j], P_mixed[j])
                xs[j], Ps[j], innov, S = f.update(xp, Pp, z, scale[k])
                like[j] = gaussian_likelihood(innov, S)
            mu = c * like
            total = mu.sum()
            mu = mu / total if total > 0 and np.isfinite(total) else np.full(m, 1.0 / m)
        paths = np.stack([self._rollout(xs[j], omega) for j, omega in enumerate(self.omegas)])
        return make_candidates(w, paths, scores=mu, selected=int(np.argmax(mu)))

    def _rollout(self, x: np.ndarray, omega: float) -> np.ndarray:
        # Filtering assumes a steady turn, but ships rarely hold one for 10 minutes: the forecast lets the
        # turn rate decay (time constant turn_decay_s), so a turning mode ends as a bend, not a circle.
        out = np.empty((streams.FUT_STEPS, 2))
        for k in range(streams.FUT_STEPS):
            x = transition(DT, omega * np.exp(-k * DT / self.turn_decay_s)) @ x
            out[k] = x[:2]
        return out


REGISTRY = {"cv": ConstantVelocity, "kalman": Kalman, "imm": IMM}
