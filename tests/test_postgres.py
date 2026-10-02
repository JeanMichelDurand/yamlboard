"""Runs only with YAMLBOARD_TEST_PG_URL set, e.g. postgresql+psycopg://postgres:pw@localhost:55432/postgres."""

import os
from datetime import date

import pytest

from yamlboard import query as q
from yamlboard.engine import columns_for, dialect_of, engine_for, run
from yamlboard.schema import Aggregation as A
from yamlboard.schema import Grain, Report

URL = os.environ.get("YAMLBOARD_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not URL, reason="YAMLBOARD_TEST_PG_URL not set")

REPORT = Report.model_validate({
    "id": "pg", "title": "PG", "datasource": "pg",
    "parameters": [{"name": "since", "type": "date", "required": True}],
    # orderDate folds to orderdate; "Kind" stays case-sensitive: both must resolve from the YAML names.
    "sql": """
        select d::date as orderDate, (array['a','b',null])[1 + i % 3] as "Kind", i::int as qty
        from generate_series(1, 90) i, lateral (select date '2026-01-01' + i as d) x
        where d >= cast(:since as date)
    """,
    "fields": [
        {"name": "orderDate", "type": "date", "filterable": True, "groupable_rows": True, "grains": ["month"]},
        {"name": "KIND", "filterable": True, "groupable": True},
        {"name": "qty", "type": "integer", "filterable": True, "measures": ["sum", "median"]},
    ],
})
P = {"since": date(2026, 1, 1)}


@pytest.fixture(scope="module")
def pg():
    engine = engine_for(URL)
    assert dialect_of(engine).name == "postgresql"
    return engine, dialect_of(engine), columns_for(engine, REPORT, P)


def test_aggregate(pg):
    engine, d, cmap = pg
    df = run(engine, q.aggregate(REPORT, d, P, rows=[q.Group("orderDate", Grain.MONTH)], columns=[q.Group("KIND")],
                                 measures=[q.Measure("qty", A.SUM)], columns_map=cmap))
    assert list(df.columns) == ["orderDate", "KIND", "sum_qty"]
    assert df["sum_qty"].sum() == sum(range(1, 91))
    df = run(engine, q.aggregate(REPORT, d, P, measures=[q.Measure("qty", A.MEDIAN)], columns_map=cmap))
    assert df["median_qty"].iloc[0] == 45.5


def test_filters_and_distinct(pg):
    engine, d, cmap = pg
    n = lambda *f: run(engine, q.count(REPORT, d, P, list(f), columns_map=cmap))["n"].iloc[0]
    assert n(q.Filter("KIND", "in", ("a", None))) + n(q.Filter("KIND", "not_in", ("a", None))) == 90
    assert n(q.Filter("qty", "between", (10, 20))) + n(q.Filter("qty", "not_between", (10, 20))) == 90
    assert n(q.Filter("orderDate", "between", ("2026-02-01", "2026-02-28"))) == 28
    values = run(engine, q.distinct_values(REPORT, d, P, "KIND", columns_map=cmap))["KIND"].tolist()
    assert values[:2] == ["a", "b"] and len(values) == 3


def test_detail(pg):
    engine, d, cmap = pg
    df = run(engine, q.detail(REPORT, d, P, order_by="qty", descending=True, limit=3, offset=1, columns_map=cmap))
    assert df["qty"].tolist() == [89, 88, 87]


def test_sessions_stay_in_utc(pg):
    engine, _, _ = pg
    for _ in range(2):  # the SET must survive run()'s rollback on a pooled connection
        df = run(engine, q.Statement("select current_setting('TimeZone') as tz, now() as t", {}))
        assert df["tz"].iloc[0] == "UTC" and str(df["t"].dt.tz) == "UTC"
