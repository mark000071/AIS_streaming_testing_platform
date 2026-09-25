"""Linear Kalman machinery for state [x, y, vx, vy] in metres and m/s."""

from __future__ import annotations

import numpy as np

H = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0]])


def transition(dt: float, omega: float = 0.0) -> np.ndarray:
    if abs(omega) < 1e-9:
        return np.array([[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
    s, c = np.sin(omega * dt), np.cos(omega * dt)
    return np.array(
        [
            [1, 0, s / omega, -(1 - c) / omega],
            [0, 1, (1 - c) / omega, s / omega],
            [0, 0, c, -s],
            [0, 0, s, c],
        ],
        dtype=float,
    )


def process_noise(dt: float, sigma_a: float) -> np.ndarray:
    g = np.array([[dt * dt / 2, 0], [0, dt * dt / 2], [dt, 0], [0, dt]])
    return g @ g.T * sigma_a**2


def initial_state(tokens: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    from envship_sdk.geo import velocity_en

    x0, y0, sog, cog = (float(v) for v in tokens[0, :4])
    vx, vy = velocity_en(sog, cog)
    return np.array([x0, y0, vx, vy]), np.diag([25.0**2, 25.0**2, 1.0, 1.0])


class KalmanFilter:
    def __init__(self, dt: float, sigma_a: float, sigma_z: float, omega: float = 0.0):
        self.F = transition(dt, omega)
        self.Q = process_noise(dt, sigma_a)
        self.R = np.eye(2) * sigma_z**2

    def predict(self, x: np.ndarray, P: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.F @ x, self.F @ P @ self.F.T + self.Q

    def update(self, x: np.ndarray, P: np.ndarray, z: np.ndarray, r_scale: float = 1.0):
        S = H @ P @ H.T + self.R * r_scale
        K = P @ H.T @ np.linalg.inv(S)
        innov = z - H @ x
        x = x + K @ innov
        P = (np.eye(4) - K @ H) @ P
        return x, P, innov, S

    def rollout(self, x: np.ndarray, steps: int) -> np.ndarray:
        out = np.empty((steps, 2))
        for k in range(steps):
            x = self.F @ x
            out[k] = x[:2]
        return out


def gaussian_likelihood(innov: np.ndarray, S: np.ndarray) -> float:
    d = float(innov @ np.linalg.solve(S, innov))
    return float(np.exp(-0.5 * d) / (2 * np.pi * np.sqrt(np.linalg.det(S))))
