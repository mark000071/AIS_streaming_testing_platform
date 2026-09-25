import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from envship_core.api.query import QueryError, Snapshot, check_user_sql


@pytest.fixture
def snap(tmp_path):
    d = tmp_path / "scores" / "predictor_id=cv" / "date=2026-09-24"
    d.mkdir(parents=True)
    pq.write_table(pa.table({"window_id": ["a", "b"], "served_ade": [1.0, 3.0]}), d / "part-0.parquet")
    s = Snapshot(tmp_path)
    s.refresh()
    return s


def test_select_works(snap):
    cols, rows, truncated = snap.run(
        check_user_sql("SELECT predictor_id, avg(served_ade) FROM scores GROUP BY 1")
    )
    assert rows == [("cv", 2.0)] and not truncated


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "DROP TABLE scores",
        "COPY scores TO 'x.csv'",
        "ATTACH 'x.db'",
        "SET enable_external_access = true",
    ],
)
def test_non_select_rejected(sql):
    with pytest.raises(QueryError):
        check_user_sql(sql)


def test_files_are_unreachable(snap, tmp_path):
    (tmp_path / "secret.csv").write_text("a\n1\n")
    with pytest.raises(QueryError):
        snap.run(check_user_sql(f"SELECT * FROM read_csv('{tmp_path / 'secret.csv'}')"))


def test_row_limit(snap):
    _, rows, truncated = snap.run("SELECT * FROM range(100)", max_rows=10)
    assert len(rows) == 10 and truncated
