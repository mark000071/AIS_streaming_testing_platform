"""Buffered, Hive-partitioned zstd-parquet writer with atomic rename."""

from __future__ import annotations

import os
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from envship_contracts.parquet_schemas import PARTITIONS, TABLES


def date_hour(ts: float) -> tuple[str, str]:
    d = datetime.fromtimestamp(ts, tz=UTC)
    return d.strftime("%Y-%m-%d"), d.strftime("%H")


class ParquetSink:
    def __init__(self, root: Path, table: str, flush_s: float = 15.0, max_rows: int = 50_000):
        self.dir = Path(root) / table
        self.schema = TABLES[table]
        self.partitions = PARTITIONS[table]
        self.flush_s = flush_s
        self.max_rows = max_rows
        self.buf: dict[tuple, list[dict]] = defaultdict(list)
        self.rows = 0
        self.last_flush = time.time()

    def add(self, row: dict, **partition: str) -> None:
        key = tuple(partition[p] for p in self.partitions)
        self.buf[key].append(row)
        self.rows += 1
        if self.rows >= self.max_rows:
            self.flush()

    def maybe_flush(self) -> None:
        if self.rows and time.time() - self.last_flush >= self.flush_s:
            self.flush()

    def flush(self) -> None:
        for key, rows in self.buf.items():
            part_dir = self.dir.joinpath(*(f"{p}={v}" for p, v in zip(self.partitions, key, strict=True)))
            part_dir.mkdir(parents=True, exist_ok=True)
            name = f"part-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.parquet"
            tmp = part_dir / f".{name}.tmp"
            table = pa.Table.from_pylist(rows, schema=self.schema)
            pq.write_table(table, tmp, compression="zstd")
            os.replace(tmp, part_dir / name)
        self.buf.clear()
        self.rows = 0
        self.last_flush = time.time()
