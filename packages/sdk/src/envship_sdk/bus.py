"""Thin Redis Streams helpers shared by the SDK runner and platform services."""

from __future__ import annotations

import os
import socket
import time
from typing import Any, cast

import redis
from envship_contracts import HealthBeat, encode, streams
from envship_contracts.models import _Msg
from redis.client import Pipeline

Entry = tuple[str, dict[bytes, bytes]]
Batch = list[tuple[str, list[Entry]]]


def connect(url: str | None = None) -> redis.Redis:
    return redis.Redis.from_url(url or os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


def instance_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def publish(r: redis.Redis | Pipeline | Any, topic: str, msg: _Msg) -> None:
    minid = int((time.time() - streams.retention_for(topic)) * 1000)
    r.xadd(topic, cast(Any, encode(msg)), minid=str(minid), approximate=True)


def _text(v: bytes | str) -> str:
    return v.decode() if isinstance(v, bytes) else v


def _normalise(resp: Any) -> Batch:
    return [
        (_text(topic), [(_text(eid), fields) for eid, fields in entries]) for topic, entries in resp or []
    ]


def read_group(
    r: redis.Redis, group: str, consumer: str, cursors: dict[str, str], count: int, block: int
) -> Batch:
    """XREADGROUP with topic names and entry ids as str."""
    return _normalise(r.xreadgroup(group, consumer, cast(Any, cursors), count=count, block=block))


def read(r: redis.Redis, cursors: dict[str, str], count: int, block: int) -> Batch:
    """XREAD (no consumer group) with topic names and entry ids as str."""
    return _normalise(r.xread(cast(Any, cursors), count=count, block=block))


def registry(r: redis.Redis) -> dict[str, dict]:
    import json

    raw = cast(dict[bytes, bytes], r.hgetall(streams.PREDICTOR_REGISTRY))
    return {k.decode(): json.loads(v) for k, v in raw.items()}


def ensure_group(r: redis.Redis, topic: str, group: str, start: str = "$") -> None:
    try:
        r.xgroup_create(topic, group, id=start, mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def group_lag(r: redis.Redis, topic: str, group: str) -> int:
    try:
        for g in cast(list[dict[str, Any]], r.xinfo_groups(topic)):
            name = g["name"].decode() if isinstance(g["name"], bytes) else g["name"]
            if name == group:
                return int(g.get("lag") or 0)
    except redis.ResponseError:
        pass
    return 0


def beat(r: redis.Redis, service: str, **kw) -> None:
    publish(r, streams.HEALTH, HealthBeat(service=service, instance=instance_name(), ts=time.time(), **kw))
