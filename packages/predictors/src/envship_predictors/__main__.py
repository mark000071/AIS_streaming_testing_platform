"""Run a built-in predictor container: ``envship-predictor cv|kalman|imm`` (or PREDICTOR_KIND)."""

import argparse
import os

from envship_sdk import run_predictor

from .baselines import REGISTRY


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "kind", nargs="?", default=os.environ.get("PREDICTOR_KIND", "cv"), choices=sorted(REGISTRY)
    )
    args = ap.parse_args()
    run_predictor(REGISTRY[args.kind]())


if __name__ == "__main__":
    main()
