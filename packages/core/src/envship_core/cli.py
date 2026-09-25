"""``envship <service>`` — one entry point per container, plus ``envship dev`` to run the whole stack locally."""

from __future__ import annotations

import sys

SERVICES = {
    "ingest-fi": "envship_core.ingest_fi",
    "replay": "envship_core.replay",
    "tracker": "envship_core.tracker",
    "reconcile": "envship_core.reconcile",
    "jobs": "envship_core.jobs",
    "features": "envship_core.features",
    "dev": "envship_core.devstack",
    "benchmark": "envship_core.benchmark",
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: envship {" + ",".join([*SERVICES, "api", "predictor"]) + "} [args...]")
        sys.exit(0 if len(sys.argv) >= 2 else 2)
    name, rest = sys.argv[1], sys.argv[2:]
    sys.argv = [f"envship {name}", *rest]
    if name == "api":
        import uvicorn

        port = int(rest[0]) if rest else 8000
        uvicorn.run("envship_core.api.app:app", host="0.0.0.0", port=port, workers=1, log_level="info")
    elif name == "predictor":
        from envship_predictors.__main__ import main as predictor_main

        predictor_main()
    elif name in SERVICES:
        import importlib

        importlib.import_module(SERVICES[name]).main()
    else:
        print(f"unknown service {name}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
