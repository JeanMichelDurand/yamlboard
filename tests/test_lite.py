"""yamlboard lite: what runs in Python, tested on DuckDB here; the browser side was checked in Chromium."""

from __future__ import annotations

import ast
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal

import duckdb
import pandas as pd
import pytest

from yamlboard import cli
from yamlboard.lite import build, duck
from yamlboard.lite.state import localize


def _browser_like(sql: str) -> pd.DataFrame:
    """What ``duck.query`` does, with Python's DuckDB standing in for DuckDB-WASM's column arrays."""
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    described = con.sql(f"DESCRIBE {sql}").fetchall()
    names, types = [r[0] for r in described], [r[1] for r in described]
    rows = con.sql(duck.portable(sql, types)).fetchall()
    cols = [list(c) for c in zip(*rows, strict=True)] if rows else [[] for _ in types]
    return duck.frame(names, types, cols)


def test_kind():
    assert duck.kind("TIMESTAMP WITH TIME ZONE") == duck.kind("TIMESTAMP_NS") == duck.TIMESTAMP
    assert duck.kind("DATE") == duck.DATE
    assert duck.kind("DECIMAL(18,2)") == duck.kind("HUGEINT") == duck.NUMBER
    assert duck.kind("BIGINT") == duck.kind("VARCHAR") == duck.kind("BOOLEAN") == duck.PLAIN
    assert duck.kind("INTEGER[]") == duck.kind("STRUCT(a INTEGER)") == duck.kind("INTERVAL") == duck.TEXT


def test_portable_round_trip():
    df = _browser_like(
        "SELECT TIMESTAMPTZ '2026-10-02 10:00:00+02' AS ts, TIMESTAMP '2026-10-02 08:00:00.123456' AS naive, "
        "DATE '2026-10-02' AS d, 12.50::DECIMAL(10,2) AS amount, 170141183460469231731687303715884105727::HUGEINT AS big, "
        "[1, 2] AS l, {'a': 1} AS s, 'x' AS t, 7 AS n, NULL::TIMESTAMP AS empty, 1 AS dup, 2 AS dup"
    )
    assert list(df.columns) == ["ts", "naive", "d", "amount", "big", "l", "s", "t", "n", "empty", "dup", "dup"]
    row = df.iloc[0]
    assert row["ts"] == pd.Timestamp("2026-10-02 08:00:00", tz="UTC")
    assert row["naive"] == pd.Timestamp("2026-10-02 08:00:00.123456", tz="UTC")
    assert row["d"] == date(2026, 10, 2)
    assert row["amount"] == 12.5
    assert row["big"] == pytest.approx(1.7014118346046923e38)
    assert row["l"] == "[1, 2]" and row["t"] == "x" and row["n"] == 7
    assert pd.isna(row["empty"])
    assert isinstance(df["ts"].dtype, pd.DatetimeTZDtype) and isinstance(df["empty"].dtype, pd.DatetimeTZDtype)


def test_portable_no_rows():
    df = _browser_like("SELECT TIMESTAMPTZ '2026-10-02 10:00:00+02' AS ts, 'x' AS t WHERE false")
    assert list(df.columns) == ["ts", "t"] and df.empty
    assert isinstance(df["ts"].dtype, pd.DatetimeTZDtype)


def test_sql_value():
    assert duck.sql_value(None) == "NULL"
    assert duck.sql_value(True) == "TRUE"
    assert duck.sql_value(3) == "3" and duck.sql_value(Decimal("1.50")) == "1.50"
    assert duck.sql_value(float("inf")) == "CAST('inf' AS DOUBLE)"
    assert duck.sql_value(date(2026, 10, 2)) == "DATE '2026-10-02'"
    assert duck.sql_value(datetime(2026, 10, 2, 8, tzinfo=timezone.utc)) == "TIMESTAMPTZ '2026-10-02T08:00:00+00:00'"
    assert duck.sql_value(datetime(2026, 10, 2, 8)) == "TIMESTAMP '2026-10-02T08:00:00'"
    assert duck.sql_value("it's") == "'it''s'"


def test_inline_skips_strings_and_comments():
    sql = "SELECT ':a' AS s, :a AS v -- :b\nWHERE x >= :since /* :c */"
    out = duck.inline(sql, {"a": 1, "since": date(2026, 1, 1)})
    assert out == "SELECT ':a' AS s, 1 AS v -- :b\nWHERE x >= DATE '2026-01-01' /* :c */"
    assert duck.inline("SELECT x::INT, '{\"k\": 1}'", {}) == "SELECT x::INT, '{\"k\": 1}'"
    with pytest.raises(ValueError, match=":missing"):
        duck.inline("SELECT :missing", {})


def test_inlined_binds_run():
    sql = duck.inline("SELECT count(*) FROM range(10) t(i) WHERE TIMESTAMPTZ '2026-01-01 00:00:00+00' + to_days(i) "
                      ">= :since AND :label = 'a''b'",
                      {"since": datetime(2026, 1, 5, tzinfo=timezone.utc), "label": "a'b"})
    assert duckdb.sql(sql).fetchone()[0] == 6


def test_error_text():
    assert duck.error_text(RuntimeError("Error: Binder Error: no column x\nLINE 1")) == "Binder Error: no column x"


def test_localize():
    sql = "SELECT * FROM read_csv('/var/log/nginx/access.log') JOIN read_csv('C:\\logs\\access.log') USING (x)"
    assert localize(sql, "access.log", "/data/access.log") == (
        "SELECT * FROM read_csv('/data/access.log') JOIN read_csv('/data/access.log') USING (x)")
    assert localize("FROM read_csv('/var/log/app-*.log')", "app-1.log", "/data/app-1.log") == (
        "FROM read_csv('/data/app-1.log')")
    untouched = "SELECT 'access.log.1', '*', 'other.log' FROM read_csv('/var/log/other.log')"
    assert localize(untouched, "access.log", "/data/access.log") == untouched


def test_build(tmp_path):
    index = build.build(tmp_path / "site")
    html = index.read_text(encoding="utf-8")
    listed = json.loads(re.search(r"const files = (\[.*?\]);", html).group(1))
    app = tmp_path / "site" / "app"
    assert all((app / f).is_file() for f in listed)
    assert "streamlit_app.py" in listed and "assets/yamlboard.svg" in listed
    assert not {f"yamlboard/{m}" for m in build.SERVER_ONLY} & set(listed)
    assert '"theme.primaryColor": "#174D98"' in html and f"@stlite/browser@{build.STLITE}" in html
    assert (tmp_path / "site" / ".nojekyll").exists()

    # every yamlboard module a shipped file imports is shipped too
    for f in listed:
        if not f.endswith(".py"):
            continue
        tree = compile((app / f).read_text(encoding="utf-8"), f, "exec",
                       flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        for node in ast.walk(tree):
            names = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                     [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            if isinstance(node, ast.ImportFrom) and node.module in ("yamlboard", "yamlboard.lite"):
                names = [f"{node.module}.{a.name}" for a in node.names]
            for name in names:
                if name.startswith("yamlboard."):
                    path = name.replace(".", "/")
                    assert f"{path}.py" in listed or f"{path}/__init__.py" in listed, f"{f} imports {name}"


def test_lite_scripts_compile():
    """The entry script and pages await at top level, which stlite allows and plain Python does not."""
    for rel, src in build.sources().items():
        if rel.endswith(".py"):
            compile(src.read_text(encoding="utf-8"), rel, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def test_cli_lite(tmp_path, capsys):
    assert cli.main(["lite", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "index.html").exists()
    assert "http.server" in capsys.readouterr().out


def test_negative_binds_are_not_comments():
    assert duck.sql_value(-5) == "(-5)" and duck.sql_value(-0.5) == "(-0.5)"
    assert duck.sql_value(Decimal("-1.5")) == "(-1.5)" and duck.sql_value(float("-inf")) == "CAST('-inf' AS DOUBLE)"
    assert duckdb.sql(duck.inline("SELECT 10 -:n + 1", {"n": -5})).fetchone()[0] == 16   # not `10 --5 + 1`
