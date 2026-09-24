"""Message models (roadmap §4.2). All timestamps are UTC epoch seconds (float).

Array fields travel as Arrow IPC on the wire (see codec.py); everything else as msgpack.
"""

from __future__ import annotations

from typing import Annotated, Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, PlainValidator, WithJsonSchema

from .streams import PROTOCOL_VERSION


def _as(dtype: str) -> PlainValidator:
    def validate(v: Any) -> np.ndarray:
        return np.asarray(v, dtype=dtype)

    return PlainValidator(validate)


_TO_LIST = PlainSerializer(lambda a: np.asarray(a).tolist(), when_used="json")


def _schema(dtype: str, shape: str) -> WithJsonSchema:
    return WithJsonSchema(
        {
            "type": "array",
            "description": f"{dtype} array of shape {shape}; Arrow IPC list<{dtype}> column on the wire",
            "x-dtype": dtype,
            "x-shape": shape,
        }
    )


F32_Tx8 = Annotated[np.ndarray, _as("float32"), _TO_LIST, _schema("float32", "(30, 8)")]
F64_Tx2 = Annotated[np.ndarray, _as("float64"), _TO_LIST, _schema("float64", "(30, 2)")]
F32_G = Annotated[np.ndarray, _as("float32"), _TO_LIST, _schema("float32", "(19,)")]
F32_KxTx2 = Annotated[np.ndarray, _as("float32"), _TO_LIST, _schema("float32", "(K, 30, 2)")]
F32_K = Annotated[np.ndarray, _as("float32"), _TO_LIST, _schema("float32", "(K,)")]
F32_T = Annotated[np.ndarray, _as("float32"), _TO_LIST, _schema("float32", "(30,)")]
F32_Tx2 = Annotated[np.ndarray, _as("float32"), _TO_LIST, _schema("float32", "(30, 2)")]
BOOL_T = Annotated[np.ndarray, _as("bool"), _TO_LIST, _schema("bool", "(30,)")]


class _Msg(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=False)


class RawPosition(_Msg):
    """One AIS position report as published on ``ais.raw.{feed}``."""

    mmsi: int
    feed: str
    recv_ts: float = Field(description="receiver/feed timestamp (event time)")
    ingest_wall: float = Field(description="wall clock when ingest published it")
    lat: float
    lon: float
    sog: float | None = None
    cog: float | None = None
    heading: int | None = None
    nav_status: int | None = None
    ais_class: str = "A"
    replay: bool = False


class Window(_Msg):
    """An eligible 30-step history window published on ``windows.eligible``.

    tokens columns: x, y (m east/north of anchor), sog (kn), cog (deg), sin cog, cos cog,
    report age (s), dead-reckoned flag.
    """

    window_id: str
    mmsi: int
    feed: str
    anchor_ts: float
    enqueue_wall: float
    anchor_lat: float
    anchor_lon: float
    tokens: F32_Tx8
    abs_xy: F64_Tx2 = Field(description="(lat, lon) of each history step")
    tile_id: str = "none"
    tile_offset: tuple[int, int] = (0, 0)
    geom: F32_G
    tags: list[str]
    protocol_version: str = PROTOCOL_VERSION


class Candidates(_Msg):
    """K candidate futures from one predictor for one window (``predictions.{id}``)."""

    window_id: str
    predictor_id: str = ""
    model_revision: str = ""
    mmsi: int = 0
    anchor_ts: float = 0.0
    enqueue_wall: float = 0.0
    issue_wall: float = 0.0
    paths: F32_KxTx2 = Field(description="displacement (east, north) in metres from the anchor")
    scores: F32_K | None = None
    selected: int = 0
    compute_ms: float = 0.0


class Truth(_Msg):
    """Realized future for a window (``truth.realized``), published once the horizon has passed."""

    window_id: str
    mmsi: int
    feed: str
    anchor_ts: float
    future_xy: F32_Tx2 = Field(description="displacement (east, north) in metres; NaN where invalid")
    valid: BOOL_T
    coverage: float
    fill_method: str = "linear"


class Score(_Msg):
    """Per-(window, predictor) verdict emitted by reconcile on ``scores``."""

    window_id: str
    predictor_id: str
    mmsi: int
    feed: str
    anchor_ts: float
    scored_wall: float
    miss: bool
    served_ade: float | None = None
    served_fde: float | None = None
    oracle_ade: float | None = None
    per_horizon: F32_T | None = None
    k: int = 0
    comparable: bool = False
    coverage: float = 0.0
    scene: str = "straight"
    speed_band: str = "slow"
    queue_ms: float | None = None
    compute_ms: float | None = None


class PredictorInfo(_Msg):
    id: str
    version: str
    k: int
    description: str = ""
    required_tags: list[str] = Field(default_factory=list)
    cpu: float = 1.0
    mem_mb: int = 512


class HealthBeat(_Msg):
    service: str
    instance: str
    ts: float
    queue_depth: int = 0
    last_event_ts: float | None = None
    parity_ok: bool | None = None
    counters: dict[str, float] = Field(default_factory=dict)


MESSAGE_TYPES: dict[str, type[_Msg]] = {
    cls.__name__: cls for cls in (RawPosition, Window, Candidates, Truth, Score, PredictorInfo, HealthBeat)
}
