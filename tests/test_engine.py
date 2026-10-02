"""Connections: a DuckDB file is not held open between statements, and a failed run names its log entry."""

import subprocess
import sys

import duckdb
import pytest
from sqlalchemy.pool import NullPool

from yamlboard.engine import DB_ERRORS, engine_for, error_text, run, sql_text
from yamlboard.query import Statement

WRITER = "import duckdb, sys; duckdb.connect(sys.argv[1]).close(); print('opened')"


def test_a_duckdb_file_is_free_between_statements(tmp_path):
    path = tmp_path / "w.duckdb"
    duckdb.connect(str(path)).execute("create table t as select 1 as a").close()
    engine = engine_for(f"duckdb:///{path}?access_mode=READ_ONLY")
    assert isinstance(engine.pool, NullPool)
    assert run(engine, Statement("select a from t", {}))["a"].tolist() == [1]
    # Another process (a notebook kernel, a loader) can open it for writing while the app idles.
    writer = subprocess.run([sys.executable, "-c", WRITER, str(path)], capture_output=True, text=True, check=False)
    assert writer.stdout.strip() == "opened", writer.stderr


def test_memory_keeps_its_pool():
    assert not isinstance(engine_for("duckdb:///:memory:").pool, NullPool)


def test_a_failed_run_names_its_log_entry(tmp_path):
    missing = engine_for(f"duckdb:///{tmp_path / 'missing.duckdb'}?access_mode=READ_ONLY")
    with pytest.raises(DB_ERRORS) as failed:   # fails on connect, before any SQL
        run(missing, Statement("select 1", {}))
    assert len(failed.value.run_id) == 36 and "does not exist" in error_text(failed.value)
    with pytest.raises(DB_ERRORS) as failed:
        run(engine_for("duckdb:///:memory:"), Statement("select * from nowhere", {}))
    assert failed.value.run_id and "nowhere" in error_text(failed.value) and "select" not in error_text(failed.value)


def test_a_colon_in_a_literal_or_a_comment_is_not_a_bind():
    """As ``schema.sql_binds`` reads the SQL: a report that validates also runs."""
    sql = "select ':a' as s, 'x :b y' as t, :v as v -- :todo\n/* :c */"
    df = run(engine_for("duckdb:///:memory:"), Statement(sql, {"v": 1}))
    assert df.iloc[0].tolist() == [":a", "x :b y", 1]
    assert sql_text("select '12:30', 'a::int'").text == "select '12:30', 'a::int'"   # no bind there: unchanged
