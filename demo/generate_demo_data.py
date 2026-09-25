#!/usr/bin/env python3
"""Regenerate demo/data/{predictions,scores}.json from backend/app/demo_data.py.

Run this after changing the generator so the bundled dataset (used whenever
AIS_DATA_MODE isn't forced to 'bridge') stays in sync:

    python3 demo/generate_demo_data.py
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app import demo_data  # noqa: E402


def main() -> None:
    predictions, scores = demo_data.generate()
    out_dir = REPO_ROOT / "demo" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "predictions.json").write_text(json.dumps(predictions, indent=1))
    (out_dir / "scores.json").write_text(json.dumps(scores, indent=1))
    print("wrote {} predictions and {} scores to {}".format(
        len(predictions), len(scores), out_dir))


if __name__ == "__main__":
    main()
