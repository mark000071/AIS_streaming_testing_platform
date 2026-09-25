"""Wire codec: msgpack control fields + Arrow IPC for numeric arrays.

A stream entry has fields ``v`` (protocol version), ``t`` (type name), ``h`` (msgpack header)
and, when the message carries arrays, ``a`` (one Arrow IPC record batch, one list column per array).
"""

from __future__ import annotations

from typing import TypeVar, cast

import msgpack
import numpy as np
import pyarrow as pa

from .models import MESSAGE_TYPES, _Msg
from .streams import PROTOCOL_VERSION

M = TypeVar("M", bound=_Msg)


def encode(msg: _Msg) -> dict[str, bytes]:
    header: dict = {}
    arrays: dict[str, np.ndarray] = {}
    for name in type(msg).model_fields:
        value = getattr(msg, name)
        if isinstance(value, np.ndarray):
            arrays[name] = value
        elif isinstance(value, tuple):
            header[name] = list(value)
        else:
            header[name] = value
    out = {"v": PROTOCOL_VERSION.encode(), "t": type(msg).__name__.encode()}
    if arrays:
        header["_shapes"] = {k: list(a.shape) for k, a in arrays.items()}
        columns = {}
        for k, a in arrays.items():
            flat = pa.array(np.ascontiguousarray(a).ravel())
            columns[k] = pa.ListArray.from_arrays(pa.array([0, len(flat)], pa.int32()), flat)
        batch = pa.RecordBatch.from_pydict(columns)
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, batch.schema) as writer:
            writer.write_batch(batch)
        out["a"] = sink.getvalue().to_pybytes()
    out["h"] = cast(bytes, msgpack.packb(header, use_bin_type=True))
    return out


def _get(fields: dict, key: str) -> bytes | None:
    v = fields.get(key)
    if v is None:
        v = fields.get(key.encode())
    return v


def decode(fields: dict, expect: type[M] | None = None) -> M:
    type_name = _get(fields, "t")
    if type_name is None:
        raise ValueError("stream entry has no type field")
    cls = MESSAGE_TYPES[type_name.decode() if isinstance(type_name, bytes) else type_name]
    if expect is not None and cls is not expect:
        raise TypeError(f"expected {expect.__name__}, got {cls.__name__}")
    header = msgpack.unpackb(_get(fields, "h"), raw=False)
    shapes = header.pop("_shapes", {})
    payload = _get(fields, "a")
    if payload:
        batch = pa.ipc.open_stream(pa.py_buffer(payload)).read_next_batch()
        for name, shape in shapes.items():
            flat = batch.column(name)[0].values.to_numpy(zero_copy_only=False)
            header[name] = flat.reshape(shape)
    return cls.model_validate(header)  # type: ignore[return-value]
