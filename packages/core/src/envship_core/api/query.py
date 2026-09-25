"""DuckDB over the parquet archive.

Every ``snapshot_refresh_s`` the archive is loaded into a fresh in-memory database, after which external
access is disabled and the configuration locked. User SQL (``POST /v1/query``) therefore sees only
these tables and cannot read files, attach databases or change settings.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import duckdb

log = logging.getLogger("envship.api.query")

TABLES = {
    "scores": "*",
    "predictions": "* EXCLUDE (paths)",
    "windows": "* EXCLUDE (tokens)",
    "truth": "*",
    "raw_ais": "*",
    "leaderboard_hourly": "*",
    "sla_hourly": "*",
}
RAW_KEEP_S = 6 * 3600


class QueryError(Exception):
    pass


class Snapshot:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.lock = threading.Lock()
        self.con: duckdb.DuckDBPyConnection | None = None
        self.tables: dict[str, int] = {}
        self.built_at = 0.0

    def refresh(self) -> None:
        con = duckdb.connect(":memory:")
        tables: dict[str, int] = {}
        for name, cols in TABLES.items():
            files = list((self.data_dir / name).rglob("*.parquet"))
            if not files:
                continue
            src = f"read_parquet('{self.data_dir / name}/**/*.parquet', hive_partitioning = true, union_by_name = true)"
            where = ""
            if name == "raw_ais":
                where = f" WHERE recv_ts > (SELECT max(recv_ts) FROM {src}) - {RAW_KEEP_S}"
            try:
                con.execute(f"CREATE TABLE {name} AS SELECT {cols} FROM {src}{where}")
                tables[name] = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]  # type: ignore[index]
            except duckdb.Error as e:
                log.warning("could not load %s: %s", name, e)
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        with self.lock:
            old, self.con, self.tables, self.built_at = self.con, con, tables, time.time()
        if old is not None:
            old.close()

    def cursor(self) -> duckdb.DuckDBPyConnection:
        with self.lock:
            if self.con is None:
                raise QueryError("archive snapshot not ready yet")
            return self.con.cursor()

    def has(self, table: str) -> bool:
        return table in self.tables

    def run(self, sql: str, params: list | None = None, timeout_s: float = 10.0, max_rows: int = 10_000):
        cur = self.cursor()
        timer = threading.Timer(timeout_s, cur.interrupt)
        timer.start()
        try:
            rel = cur.execute(sql, params or [])
            cols = [d[0] for d in rel.description or []]
            rows = rel.fetchmany(max_rows + 1)
        except duckdb.InterruptException as e:
            raise QueryError(f"query exceeded {timeout_s:.0f} s") from e
        except duckdb.Error as e:
            raise QueryError(str(e).splitlines()[0]) from e
        finally:
            timer.cancel()
            cur.close()
        return cols, rows[:max_rows], len(rows) > max_rows


def check_user_sql(sql: str) -> str:
    try:
        stmts = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        raise QueryError(str(e).splitlines()[0]) from e
    if len(stmts) != 1:
        raise QueryError("exactly one statement is allowed")
    if stmts[0].type != duckdb.StatementType.SELECT:
        raise QueryError("only SELECT queries are allowed")
    return stmts[0].query
