"""Shared plumbing for platform services: logging, metrics endpoint, graceful stop."""

from __future__ import annotations

import logging
import os
import signal

from prometheus_client import start_http_server

from .settings import Settings


class Stopper:
    def __init__(self) -> None:
        self.stopped = False
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

    def _stop(self, *_) -> None:
        self.stopped = True


def setup(name: str, s: Settings) -> logging.Logger:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format=f"%(asctime)s {name} %(levelname)s %(message)s"
    )
    if s.metrics_port:
        start_http_server(s.metrics_port)
    return logging.getLogger(f"envship.{name}")
