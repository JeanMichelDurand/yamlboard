"""Connections: a DuckDB file is not held open between statements and its statements take turns, and a failed
run names its log entry."""

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import duckdb
import pytest
from sqlalchemy.pool import NullPool

from yamlboard import engine as engine_module
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


def test_a_duckdb_files_statements_take_turns(tmp_path, monkeypatch):
    # The connections one process opens to a file share its memory_limit: four heavy dashboard cells at once
    # ran it out of memory. Threads queue for the file instead.
    path = tmp_path / "t.duckdb"
    duckdb.connect(str(path)).execute("create table t as select 1 as a").close()
    engine = engine_for(f"duckdb:///{path}?access_mode=READ_ONLY")
    real, guard, inside, most = engine_module.pd.read_sql, threading.Lock(), [0], [0]

    def read_sql(*args, **kwargs):
        with guard:
            inside[0] += 1
            most[0] = max(most[0], inside[0])
        time.sleep(0.05)
        try:
            return real(*args, **kwargs)
        finally:
            with guard:
                inside[0] -= 1

    monkeypatch.setattr(engine_module.pd, "read_sql", read_sql)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run(engine, Statement("select a from t", {})), range(4)))
    assert [r["a"].tolist() for r in results] == [[1]] * 4
    assert most[0] == 1


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


def test_a_slice_is_not_a_bind():
    """SQLAlchemy reads ``:2`` as a bind, ``schema.sql_binds`` does not: the report validates, so it runs."""
    df = run(engine_for("duckdb:///:memory:"), Statement("select [1,2,3][:2] as x, :v as v", {"v": 1}))
    assert df.iloc[0].tolist()[1] == 1 and list(df.iloc[0]["x"]) == [1, 2]


def test_two_spellings_of_one_file_share_its_lock(tmp_path, monkeypatch):
    path = tmp_path / "same.duckdb"
    duckdb.connect(str(path)).close()
    monkeypatch.chdir(tmp_path)
    absolute, relative = engine_for(f"duckdb:///{path}"), engine_for("duckdb:///same.duckdb")
    assert absolute is not relative
    assert engine_module._ONE_AT_A_TIME[absolute] is engine_module._ONE_AT_A_TIME[relative]


def test_a_statement_of_a_page_that_is_gone_is_dropped(tmp_path):
    path = tmp_path / "gone.duckdb"
    duckdb.connect(str(path)).close()
    engine, gone = engine_for(f"duckdb:///{path}"), threading.Event()
    with engine_module.abandoned_when(gone):
        assert run(engine, Statement("select 1 as a", {}))["a"].tolist() == [1]
        gone.set()
        with pytest.raises(engine_module.Abandoned):
            run(engine, Statement("select 1 as a", {}))
    assert run(engine, Statement("select 1 as a", {}))["a"].tolist() == [1]   # out of the block: run
