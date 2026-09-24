"""jobs: scheduled DuckDB materializations (APScheduler). Each job also runs by hand:

python -m envship_core.jobs leaderboard   |   python -m envship_core.jobs sla
"""

from __future__ import annotations

import contextlib
import shutil
import sys
from pathlib import Path

import duckdb
from apscheduler.schedulers.blocking import BlockingScheduler

from .service import setup
from .settings import Settings, get_settings


def _glob(s: Settings, table: str) -> str:
    return str(Path(s.data_dir) / table / "**" / "*.parquet")


def _has_files(s: Settings, table: str) -> bool:
    return any((Path(s.data_dir) / table).rglob("*.parquet"))


def _materialize(s: Settings, name: str, sql: str) -> int:
    target = Path(s.data_dir) / name
    tmp = Path(s.data_dir) / f".{name}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    con = duckdb.connect()
    n = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]  # type: ignore[index]
    con.execute(f"COPY ({sql}) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (date))")
    old = Path(s.data_dir) / f".{name}.old"
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    tmp.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    return int(n)


def leaderboard(s: Settings) -> int:
    if not _has_files(s, "scores"):
        return 0
    b = int(s.bucket_s)
    sql = f"""
        SELECT predictor_id,
               to_timestamp(floor(anchor_ts / {b}) * {b}) AS bucket,
               strftime(to_timestamp(floor(anchor_ts / {b}) * {b}), '%Y-%m-%d') AS date,
               count(*) AS n,
               count(*) FILTER (WHERE NOT miss) AS n_scored,
               avg(served_ade) FILTER (WHERE NOT miss) AS served_ade,
               avg(oracle_ade) FILTER (WHERE NOT miss) AS oracle_ade,
               avg(served_fde) FILTER (WHERE NOT miss) AS served_fde,
               1 - avg(miss::INT) AS coverage,
               avg(served_ade) FILTER (WHERE NOT miss AND comparable) AS comparable_ade
        FROM read_parquet('{_glob(s, "scores")}', hive_partitioning = true, union_by_name = true)
        GROUP BY ALL"""
    return _materialize(s, "leaderboard_hourly", sql)


def sla(s: Settings) -> int:
    if not (_has_files(s, "predictions") and _has_files(s, "windows")):
        return 0
    b = int(s.bucket_s)
    sql = f"""
        WITH p AS (
            SELECT * FROM read_parquet('{_glob(s, "predictions")}', hive_partitioning = true, union_by_name = true)),
        w AS (
            SELECT window_id, feed, list_contains(tags, 'replay') AS replay
            FROM read_parquet('{_glob(s, "windows")}', hive_partitioning = true, union_by_name = true))
        SELECT w.feed, p.predictor_id, w.replay,
               to_timestamp(floor(p.anchor_ts / {b}) * {b}) AS bucket,
               strftime(to_timestamp(floor(p.anchor_ts / {b}) * {b}), '%Y-%m-%d') AS date,
               count(*) AS n,
               quantile_cont((p.issue_wall - p.enqueue_wall) * 1000, 0.5) AS queue_p50_ms,
               quantile_cont((p.issue_wall - p.enqueue_wall) * 1000, 0.99) AS queue_p99_ms,
               quantile_cont(p.compute_ms, 0.5) AS compute_p50_ms,
               quantile_cont(p.compute_ms, 0.99) AS compute_p99_ms,
               quantile_cont(p.enqueue_wall - p.anchor_ts, 0.5) FILTER (WHERE NOT w.replay) AS freshness_p50_s,
               quantile_cont(p.issue_wall - p.anchor_ts, 0.99) FILTER (WHERE NOT w.replay) AS service_p99_s,
               avg(((p.issue_wall - p.enqueue_wall) <= 5)::INT) AS within_budget
        FROM p JOIN w USING (window_id)
        GROUP BY ALL"""
    return _materialize(s, "sla_hourly", sql)


JOBS = {"leaderboard": leaderboard, "sla": sla}


def main() -> None:
    s = get_settings()
    log = setup("jobs", s)
    if len(sys.argv) > 1:
        print(f"{sys.argv[1]}: {JOBS[sys.argv[1]](s)} rows")
        return
    sched = BlockingScheduler()

    def run(name: str) -> None:
        try:
            log.info("%s: materialized %d rows", name, JOBS[name](s))
        except Exception:
            log.exception("job %s failed", name)

    for name in JOBS:
        sched.add_job(run, "interval", args=[name], seconds=60, id=name, max_instances=1, coalesce=True)
    log.info("jobs scheduler started: %s every 60 s (bucket %d s)", ", ".join(JOBS), s.bucket_s)
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        sched.start()


if __name__ == "__main__":
    main()
