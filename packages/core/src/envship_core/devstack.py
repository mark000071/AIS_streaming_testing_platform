"""``envship dev``: run the whole platform on a laptop without Docker.

Starts redis-server if nothing answers on REDIS_URL, then every service as a child process with
prefixed logs. Ctrl-C stops everything.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import redis

from .settings import get_settings

COLORS = ["36", "33", "35", "32", "34", "31", "96", "93", "95", "92"]
ROOT = Path(__file__).resolve().parents[4]


def _pump(name: str, color: str, proc: subprocess.Popen) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(f"\033[{color}m{name:>16}\033[0m | {line}")
        sys.stdout.flush()


MCMNET_CPU_MAX_SPEED = 5.0


def _mcmnet_ready() -> bool:
    """MCM-Net runs when MCM_ROOT and MCM_WEIGHTS are set and torch is importable."""
    if not (os.environ.get("MCM_ROOT") and os.environ.get("MCM_WEIGHTS")):
        return False
    try:
        import importlib.util

        missing = [m for m in ("torch", "scipy") if importlib.util.find_spec(m) is None]
    except ValueError:
        missing = ["torch"]
    if missing:
        print(f"MCM_ROOT/MCM_WEIGHTS are set but {', '.join(missing)} is not installed: skipping MCM-Net")
        return False
    return True


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser(prog="envship dev")
    ap.add_argument("--live", action="store_true", help="use the live Digitraffic feed instead of replay")
    ap.add_argument("--speed", type=float, default=s.replay_speed, help="replay speed multiplier")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--fresh", action="store_true", help="wipe Redis db and the data dir first")
    ap.add_argument("--no-example", action="store_true", help="do not run the SDK example predictor")
    ap.add_argument(
        "--no-mcmnet", action="store_true", help="do not run MCM-Net even if MCM_ROOT and MCM_WEIGHTS are set"
    )
    ap.add_argument(
        "--mcmnet-replicas", type=int, default=1, help="MCM-Net predictor processes (share the load)"
    )
    args = ap.parse_args()
    mcmnet = _mcmnet_ready() if not args.no_mcmnet else False

    procs: list[tuple[str, subprocess.Popen]] = []
    url = urllib.parse.urlparse(s.redis_url)
    r = redis.Redis.from_url(s.redis_url)
    try:
        r.ping()
    except redis.ConnectionError:
        if not shutil.which("redis-server"):
            sys.exit(f"nothing answers on {s.redis_url} and redis-server is not installed")
        print(f"starting redis-server on port {url.port or 6379}")
        rp = subprocess.Popen(
            ["redis-server", "--port", str(url.port or 6379), "--save", "", "--appendonly", "no"],
            stdout=subprocess.DEVNULL,
        )
        procs.append(("redis", rp))
        for _ in range(50):
            try:
                r.ping()
                break
            except redis.ConnectionError:
                time.sleep(0.1)
    if args.fresh:
        r.flushdb()
        shutil.rmtree(s.data_dir, ignore_errors=True)
        print(f"wiped Redis db and {s.data_dir}")

    env = {**os.environ, "PYTHONUNBUFFERED": "1", "REDIS_URL": s.redis_url}
    dist = ROOT / "web" / "dist"
    if dist.exists():
        env["ENVSHIP_WEB_DIST"] = str(dist)
    py = [sys.executable, "-m"]
    source = ["ingest-fi"] if args.live else ["replay", "--speed", str(args.speed)]
    cmds = [
        ("tracker", py + ["envship_core.cli", "tracker"]),
        ("reconcile", py + ["envship_core.cli", "reconcile"]),
        ("jobs", py + ["envship_core.cli", "jobs"]),
        ("api", py + ["envship_core.cli", "api", str(args.port)]),
        ("predictor-cv", py + ["envship_predictors", "cv"]),
        ("predictor-kalman", py + ["envship_predictors", "kalman"]),
        ("predictor-imm", py + ["envship_predictors", "imm"]),
    ]
    if not args.no_example:
        cmds.append(("predictor-example", [sys.executable, str(ROOT / "packages/sdk/examples/minimal.py")]))
    if mcmnet:
        for i in range(max(1, args.mcmnet_replicas)):
            cmds.append((f"predictor-mcmnet{i or ''}", py + ["envship_predictors.mcmnet"]))
        if (
            not args.live
            and os.environ.get("MCM_DEVICE", "cpu") == "cpu"
            and args.speed > MCMNET_CPU_MAX_SPEED
        ):
            print(
                f"note: replay {args.speed:g}x produces windows faster than MCM-Net on CPU can answer them "
                f"(~0.3-0.8 s each); expect stale/missed windows. Use --speed {MCMNET_CPU_MAX_SPEED:g}, "
                "--mcmnet-replicas, or MCM_DEVICE=cuda."
            )
    cmds.append((source[0], py + ["envship_core.cli", *source]))

    for i, (name, cmd) in enumerate(cmds):
        p = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT
        )
        procs.append((name, p))
        threading.Thread(target=_pump, args=(name, COLORS[i % len(COLORS)], p), daemon=True).start()
        if name.startswith("predictor") or name == "tracker":
            time.sleep(0.3)

    ui = "web UI" if dist.exists() else "API (build the web UI with: cd web && npm ci && npm run build)"
    print(
        f"\n  {ui}: http://localhost:{args.port}\n  mode: {'live Digitraffic' if args.live else f'replay {args.speed:g}x'}\n"
    )

    def shutdown(*_):
        for _, p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        deadline = time.time() + 10
        for _, p in procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    while True:
        for name, p in procs:
            if p.poll() is not None and name != "redis":
                print(f"!! {name} exited with code {p.returncode}")
                procs.remove((name, p))
                break
        time.sleep(1)


if __name__ == "__main__":
    main()
