import json

import numpy as np
from envship_contracts import Candidates, HealthBeat, RawPosition, Score, Truth, Window, decode, encode
from envship_contracts.models import MESSAGE_TYPES


def _window():
    return Window(
        window_id="fi-1-100",
        mmsi=230000001,
        feed="fi",
        anchor_ts=100.0,
        enqueue_wall=5.0,
        anchor_lat=60.1,
        anchor_lon=24.9,
        tokens=np.random.default_rng(0).random((30, 8), dtype=np.float32),
        abs_xy=np.random.default_rng(1).random((30, 2)),
        geom=np.zeros(19, np.float32),
        tags=["moving"],
        tile_offset=(3, 4),
    )


def test_window_roundtrip_is_bit_exact():
    w = _window()
    back = decode(encode(w), Window)
    assert back.tokens.tobytes() == w.tokens.tobytes()
    assert back.abs_xy.tobytes() == w.abs_xy.tobytes()
    assert back.tile_offset == (3, 4) and back.tags == ["moving"]


def test_optional_arrays_and_bytes_keys():
    c = Candidates(window_id="w", paths=np.ones((3, 30, 2), np.float32), scores=None, selected=2)
    fields = {k.encode(): v for k, v in encode(c).items()}  # redis-py returns bytes keys
    back = decode(fields, Candidates)
    assert back.paths.shape == (3, 30, 2) and back.scores is None and back.selected == 2


def test_every_type_roundtrips():
    msgs = [
        RawPosition(mmsi=1, feed="fi", recv_ts=1.0, ingest_wall=2.0, lat=60, lon=24, sog=None),
        Truth(
            window_id="w",
            mmsi=1,
            feed="fi",
            anchor_ts=1,
            future_xy=np.full((30, 2), np.nan, np.float32),
            valid=np.zeros(30, bool),
            coverage=0.0,
        ),
        Score(window_id="w", predictor_id="cv", mmsi=1, feed="fi", anchor_ts=1, scored_wall=2, miss=True),
        HealthBeat(service="x", instance="y", ts=1.0, counters={"a": 1.0}),
    ]
    for m in msgs:
        assert type(decode(encode(m))) is type(m)


def test_json_schemas_export():
    for cls in MESSAGE_TYPES.values():
        json.dumps(cls.model_json_schema())
