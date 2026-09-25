"""Write JSON Schemas for every message type so non-Python predictors can validate payloads.

Usage: python -m envship_contracts.export_schemas contracts/schemas
"""

import json
import sys
from pathlib import Path

from .models import MESSAGE_TYPES


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "schemas")
    out.mkdir(parents=True, exist_ok=True)
    for name, cls in MESSAGE_TYPES.items():
        (out / f"{name}.schema.json").write_text(json.dumps(cls.model_json_schema(), indent=2) + "\n")
    print(f"wrote {len(MESSAGE_TYPES)} schemas to {out}")


if __name__ == "__main__":
    main()
