"""A complete external predictor in under 30 lines: extrapolate the mean velocity of the last 5 steps.

Run against a live platform:  REDIS_URL=redis://localhost:6379/0 python minimal.py
"""

import numpy as np
from envship_sdk import PredictorInfo, TileStore, Window, make_candidates, run_predictor


class MeanVelocity:
    def info(self) -> PredictorInfo:
        return PredictorInfo(
            id="example-meanvel",
            version="0.1.0",
            k=1,
            description="SDK example: mean velocity of the last 5 history steps",
        )

    def warmup(self, tiles: TileStore) -> None:
        pass

    def predict(self, w: Window):
        xy = w.tokens[:, :2].astype(np.float64)
        v = (xy[-1] - xy[-6]) / 5.0  # metres per 20 s step
        steps = np.arange(1, 31)[:, None]
        return make_candidates(w, xy[-1] + steps * v)


if __name__ == "__main__":
    run_predictor(MeanVelocity())
