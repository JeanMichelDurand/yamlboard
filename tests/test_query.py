from datetime import date, timedelta

import pytest

from yamlboard import query as q
from yamlboard.dialects import DIALECTS, for_name
from yamlboard.engine import columns_for, dialect_of, engine_for, run
from yamlboard.schema import Aggregation as A
from yamlboard.schema import ChartType, Grain, Report

DUCK = DIALECTS["duckdb"]
PG = DIALECTS["postgresql"]


@pytest.fixture
def sales(ws):
    return ws.reports["sales"]


def _run(ws, report, stmt):
    return run(engine_for(ws.url(report)), stmt)


def test_aggregate_runs_and_totals_match(ws, sales, sales_params):
    stmt = q.aggregate(sales, DUCK, sales_params, rows=[q.Group("order_date", Grain.MONTH)],
                       columns=[q.Group("region")], measures=[q.Measure("amount", A.SUM), q.Measure()])
    df = _run(ws, sales, stmt)
    assert list(df.columns) == ["order_date", "region", "sum_amount", "count"]
    total = _run(ws, sales, q.count(sales, DUCK, sales_params))["n"].iloc[0]
    assert df["count"].sum() == total
    assert len(df) == df[["order_date", "region"]].drop_duplicates().shape[0]


@pytest.mark.parametrize(
    "flt, expected_sql",
    [
        (q.Filter("channel", "in", ("web", None)), "(channel IN (:yb_0) OR channel IS NULL)"),
        (q.Filter("channel", "not_in", ("web",)), "(channel IS NULL OR channel NOT IN (:yb_0))"),
        (q.Filter("channel", "not_in", ("web", None)), "(channel NOT IN (:yb_0) AND channel IS NOT NULL)"),
        (q.Filter("channel", "in", (None,)), "WHERE channel IS NULL"),
        (q.Filter("channel", "not_in", (None,)), "WHERE channel IS NOT NULL"),
        (q.Filter("amount", "between", (10, None)), "WHERE amount >= :yb_0"),
        (q.Filter("amount", "between", (10, 20)), "WHERE amount BETWEEN :yb_0 AND :yb_1"),
        (q.Filter("amount", "not_between", (10, 20)), "WHERE (amount IS NULL OR NOT (amount BETWEEN :yb_0 AND :yb_1))"),
        (q.Filter("order_date", "between", ("2026-01-01", "2026-01-31")), "order_date BETWEEN :yb_0 AND :yb_1"),
    ],
)
def test_filter_sql(sales, sales_params, flt, expected_sql):
    assert expected_sql in q.count(sales, DUCK, sales_params, [flt]).sql


def test_filters_partition_the_rows(ws, sales, sales_params):
    n = lambda *f: _run(ws, sales, q.count(sales, DUCK, sales_params, list(f)))["n"].iloc[0]
    total = n()
    assert n(q.Filter("channel", "in", ("web",))) + n(q.Filter("channel", "not_in", ("web",))) == total
    assert n(q.Filter("channel", "in", (None,))) + n(q.Filter("channel", "not_in", (None,))) == total
    assert n(q.Filter("amount", "between", (10, 50))) + n(q.Filter("amount", "not_between", (10, 50))) == total
    day = date.today() - timedelta(days=30)
    assert n(q.Filter("order_date", "between", (day, None))) + n(
        q.Filter("order_date", "not_between", (day, None))) == total


def test_values_are_bound_not_inlined(sales, sales_params):
    evil = "x'); drop table t; --"
    stmt = q.count(sales, DUCK, sales_params, [q.Filter("region", "in", (evil,))])
    assert evil not in stmt.sql and evil in stmt.binds.values()


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("order_id", "in", ("x",))]), "not filterable"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("nope", "in", ("x",))]), "not a field"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("region", "like", ("x",))]), "unknown filter operator"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("amount", "between", (1,))]), "takes 2 value"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("amount", "between", (None, None))]), "at least one bound"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("amount", "between", (20, 10))]), "20.0 is after 10.0"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("region", "in", ())]), "at least one value"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("region", "contains", ("x",))]), "unknown filter operator"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("region", "in", tuple(range(1001)))]), "at most 1000"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("amount", "in", ("abc",))]), "not a valid number"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("amount", "between", ("abc", 5))]), "not a valid number"),
        (lambda r, p: q.count(r, DUCK, p, [q.Filter("order_date", "in", ("soon",))]), "not a valid date"),
        (lambda r, p: q.aggregate(r, DUCK, p, rows=[q.Group("amount")]), "cannot be grouped on rows"),
        (lambda r, p: q.aggregate(r, DUCK, p, columns=[q.Group("order_date")]), "cannot be grouped on columns"),
        (lambda r, p: q.aggregate(r, DUCK, p, rows=[q.Group("order_date", Grain.MINUTE)]), "cannot be grouped by minute"),
        (lambda r, p: q.aggregate(r, DUCK, p, rows=[q.Group("region")], columns=[q.Group("region")]), "only once"),
        (lambda r, p: q.aggregate(r, DUCK, p, measures=[q.Measure("region", A.SUM)]), "not allowed"),
        (lambda r, p: q.aggregate(r, DUCK, p, measures=[q.Measure("*", A.SUM)]), "only supports count"),
        (lambda r, p: q.detail(r, DUCK, p, order_by="nope"), "not a field"),
        (lambda r, p: q.distinct_values(r, DUCK, p, "order_id"), "not filterable"),
    ],
)
def test_not_allowed(sales, sales_params, call, message):
    with pytest.raises(q.NotAllowed, match=message):
        call(sales, sales_params)


def test_parameters(sales):
    today = date.today()
    assert q.coerce_parameters(sales, {"start": "2026-01-01", "end": "2026-02-01"})["start"] == date(2026, 1, 1)
    with pytest.raises(q.InvalidParameters, match="From is required"):
        q.coerce_parameters(sales, {"end": today})
    with pytest.raises(q.InvalidParameters, match="spans more than 366 day"):
        q.coerce_parameters(sales, {"start": today - timedelta(days=400), "end": today})
    with pytest.raises(q.InvalidParameters, match="must not be after"):
        q.coerce_parameters(sales, {"start": today, "end": today - timedelta(days=1)})
    with pytest.raises(q.InvalidParameters, match="not a valid date"):
        q.coerce_parameters(sales, {"start": "yesterday", "end": today})
    with pytest.raises(q.InvalidParameters, match="unknown parameters: x"):
        q.coerce_parameters(sales, {"start": today, "end": today, "x": 1})


def test_detail_pages_and_distinct(ws, sales, sales_params):
    page = _run(ws, sales, q.detail(sales, DUCK, sales_params, order_by="order_id", limit=5, offset=5))
    assert list(page["order_id"]) == sorted(page["order_id"]) and len(page) == 5
    values = _run(ws, sales, q.distinct_values(sales, DUCK, sales_params, "region"))["region"].tolist()
    assert values == ["East", "North", "South", "West"]


def test_ash_example_runs(ws):
    ash = ws.reports["ash-activity"]
    today = date.today()
    stmt = q.aggregate(ash, DUCK, {"datedeb": today - timedelta(days=1), "datefin": today},
                       rows=[q.Group("sample_time_utc", Grain.HOUR)], columns=[q.Group("wait_class")])
    df = _run(ws, ash, stmt)
    # Both days whole: the range is applied to the timestamp field, not "between :datedeb and :datefin".
    assert df["sample_time_utc"].nunique() == 48 and "CPU" in set(df["wait_class"])
    assert "sample_time_utc >= :yb_0 AND sample_time_utc < :yb_1" in stmt.sql


def test_postgres_sql(sales, sales_params):
    stmt = q.aggregate(sales, PG, sales_params, rows=[q.Group("order_date", Grain.WEEK)],
                       measures=[q.Measure("amount", A.MEDIAN), q.Measure("order_id", A.COUNT_DISTINCT)])
    assert "date_trunc('week', order_date)" in stmt.sql
    assert "percentile_cont(0.5) WITHIN GROUP (ORDER BY amount)" in stmt.sql
    assert "count(DISTINCT order_id)" in stmt.sql
    assert DUCK.aggregate(A.MEDIAN, "amount") == "median(amount)"


def test_identifiers(sales):
    assert DUCK.ref("dateDebut") == "dateDebut" and DUCK.ref("my col") == '"my col"'
    assert DUCK.quote('a"b') == '"a""b"'
    assert dialect_of(engine_for("duckdb:///:memory:")) is DUCK
    with pytest.raises(ValueError, match="unsupported database 'oracle'"):
        for_name("oracle")


def test_trailing_semicolon_is_dropped(sales, sales_params):
    report = sales.model_copy(update={"sql": sales.sql + ";\n"})
    assert ";" not in q.count(report, DUCK, sales_params).sql


# --- names: SQL ignores case, output keys keep the YAML spelling --------------


MIXED = Report.model_validate({
    "id": "mixed", "title": "Mixed",
    "sql": 'select range as "OrderId", range % 3 as KIND, range * 1.5 as amount from range(0, 30)',
    "fields": [
        {"name": "orderid", "filterable": True, "groupable": True, "type": "integer"},
        {"name": "Kind", "filterable": True, "groupable": True, "type": "integer"},
        {"name": "AMOUNT", "type": "number", "filterable": True, "measures": ["sum"]},
    ],
})


def test_resolve_columns_ignores_case():
    assert q.resolve_columns(MIXED, ["OrderId", "KIND", "amount"]) == {
        "orderid": "OrderId", "Kind": "KIND", "AMOUNT": "amount"}
    with pytest.raises(q.ColumnMismatch, match="not in the SQL result: AMOUNT"):
        q.resolve_columns(MIXED, ["OrderId", "KIND"])
    with pytest.raises(q.ColumnMismatch, match=r"several columns: Kind \(KIND, kind\)"):
        q.resolve_columns(MIXED, ["OrderId", "KIND", "kind", "amount"])


def test_output_keys_are_the_yaml_names():
    engine = engine_for("duckdb:///:memory:")
    cmap = columns_for(engine, MIXED, {})
    agg = run(engine, q.aggregate(MIXED, DUCK, {}, filters=[q.Filter("orderid", "between", (0, 9))],
                                  rows=[q.Group("Kind")], measures=[q.Measure("AMOUNT", A.SUM)], columns_map=cmap))
    assert list(agg.columns) == ["Kind", "sum_AMOUNT"] and agg["sum_AMOUNT"].sum() == 1.5 * sum(range(10))
    page = run(engine, q.detail(MIXED, DUCK, {}, order_by="orderid", limit=2, columns_map=cmap))
    assert list(page.columns) == ["orderid", "Kind", "AMOUNT"]
    assert 'WHERE "OrderId" BETWEEN :yb_0 AND :yb_1' in q.count(
        MIXED, DUCK, {}, [q.Filter("orderid", "between", (0, 9))], columns_map=cmap).sql
    assert list(run(engine, q.distinct_values(MIXED, DUCK, {}, "Kind", columns_map=cmap)).columns) == ["Kind"]


# --- SQL traps: timestamps, lists, negation, numbers, and all of them at once ---


TRAPS = Report.model_validate({
    "id": "traps", "title": "Traps",
    "parameters": [{"name": "since", "type": "date"}, {"name": "until", "type": "date"}],
    "ranges": [{"start": "since", "end": "until", "field": "ts"}],
    "sql": """
        select * from (values
          (timestamp '2026-03-01 00:00:00', 'a', 1, 10.0),
          (timestamp '2026-03-01 23:59:59', 'b', 2, 20.0),
          (timestamp '2026-03-02 12:00:00', 'c', 3, 30.0),
          (timestamp '2026-03-03 00:00:00', null, 4, null),
          (timestamp '2026-03-04 08:00:00', 'a', 5, 50.0),
          (null, 'b', 6, 60.0)
        ) t(ts, kind, n, amount)""",
    "fields": [
        {"name": "ts", "type": "timestamp", "filterable": True, "groupable": True, "grains": ["day"]},
        {"name": "kind", "filterable": True, "groupable": True},
        {"name": "n", "type": "integer", "filterable": True, "measures": ["sum", "max", "count"]},
        {"name": "amount", "type": "number", "filterable": True, "measures": ["sum", "avg", "min", "max"]},
    ],
})


def _traps(filters=(), params=None, **kw):
    engine = engine_for("duckdb:///:memory:")
    params = params or {}
    if "measures" in kw or "rows" in kw:
        return run(engine, q.aggregate(TRAPS, DUCK, params, filters, **kw))
    return run(engine, q.detail(TRAPS, DUCK, params, filters, order_by="n"))["n"].tolist()


def test_timestamp_keeps_the_whole_end_day():
    assert _traps([q.Filter("ts", "between", ("2026-03-01", "2026-03-01"))]) == [1, 2]
    assert _traps([q.Filter("ts", "between", (date(2026, 3, 2), date(2026, 3, 3)))]) == [3, 4]
    assert _traps([q.Filter("ts", "not_between", ("2026-03-01", "2026-03-02"))]) == [4, 5, 6]
    # A time of day is taken as written, in UTC: 13:00+01:00 is 12:00Z.
    assert _traps([q.Filter("ts", "between", ("2026-03-01T00:00:00Z", "2026-03-01T13:00:00+01:00"))]) == [1]
    assert _traps([q.Filter("ts", "between", ("2026-03-01T23:00:00-01:00", None))]) == [3, 4, 5]


def test_range_parameters_become_a_between_on_their_field():
    stmt = q.count(TRAPS, DUCK, {"since": "2026-03-02", "until": "2026-03-03"})
    assert "ts >= :yb_0 AND ts < :yb_1" in stmt.sql and ":since" not in stmt.sql
    assert _traps(params={"since": "2026-03-02", "until": "2026-03-03"}) == [3, 4]
    assert _traps(params={"since": "2026-03-04"}) == [5]
    assert _traps() == [1, 2, 3, 4, 5, 6]


def test_list_filters_on_one_field_are_alternatives():
    assert _traps([q.Filter("kind", "in", ("a",)), q.Filter("kind", "in", ("b",))]) == [1, 2, 5, 6]
    assert _traps([q.Filter("kind", "in", ("a", "a", None))]) == [1, 4, 5]
    # An inclusion and a range on one field: either matches.
    assert _traps([q.Filter("n", "in", (1,)), q.Filter("n", "between", (4, None))]) == [1, 4, 5, 6]
    stmt = q.count(TRAPS, DUCK, {}, [q.Filter("kind", "in", ("a",)), q.Filter("kind", "in", ("b", "a"))])
    assert "kind IN (:yb_0, :yb_1)" in stmt.sql and len(stmt.binds) == 4  # since, until, a, b


def test_exclusions_all_apply_and_keep_nulls():
    assert _traps([q.Filter("kind", "not_in", ("a",)), q.Filter("kind", "not_in", ("b",))]) == [3, 4]
    assert _traps([q.Filter("kind", "not_in", ("a", None))]) == [2, 3, 6]
    assert _traps([q.Filter("amount", "not_between", (15, 35))]) == [1, 4, 5, 6]
    # Include a, b, c but not b.
    assert _traps([q.Filter("kind", "in", ("a", "b", "c")), q.Filter("kind", "not_in", ("b",))]) == [1, 3, 5]


def test_aggregations_other_than_count_need_numbers():
    for agg in ("sum", "avg", "min", "max", "median"):
        with pytest.raises(ValueError, match=f"{agg} need a numeric field"):
            Report.model_validate({"id": "x", "title": "x", "sql": "select 1 as d",
                                   "fields": [{"name": "d", "type": "date", "measures": [agg]}]})
    Report.model_validate({"id": "x", "title": "x", "sql": "select 1 as d",
                           "fields": [{"name": "d", "type": "date", "measures": ["count", "count_distinct"]}]})
    with pytest.raises(q.NotAllowed, match="avg of 'n' is not allowed"):
        q.aggregate(TRAPS, DUCK, {}, measures=[q.Measure("n", A.AVG)])


def test_all_of_it_at_once():
    filters = [
        q.Filter("ts", "between", ("2026-03-01", "2026-03-03")),   # timestamp, whole end day
        q.Filter("kind", "in", ("a",)), q.Filter("kind", "in", ("b", None)),  # alternatives
        q.Filter("kind", "not_in", ("b",)),                         # then an exclusion
        q.Filter("amount", "not_between", (40, 60)),                # negated range, NULL kept
    ]
    df = _traps(filters, params={"since": "2026-03-01", "until": "2026-03-04"},
                rows=[q.Group("ts", Grain.DAY)], measures=[q.Measure("n", A.SUM)])
    assert dict(zip(df["ts"].dt.day, df["sum_n"], strict=True)) == {1: 1, 3: 4}
    df = _traps(filters, rows=[q.Group("kind")], measures=[q.Measure("amount", A.MAX)])
    assert df.fillna("-").values.tolist() == [["a", 10.0], ["-", "-"]]


def test_null_and_not_null_on_every_kind_of_field():
    for field in ("ts", "kind", "amount"):
        empty = _traps([q.Filter(field, "is_null")])
        assert empty and sorted(empty + _traps([q.Filter(field, "not_null")])) == [1, 2, 3, 4, 5, 6]
    assert _traps([q.Filter("ts", "is_null")]) == [6]
    assert "WHERE amount IS NULL" in q.count(TRAPS, DUCK, {}, [q.Filter("amount", "is_null")]).sql
    # Empty or in a range: alternatives on one field.
    assert _traps([q.Filter("ts", "is_null"), q.Filter("ts", "between", ("2026-03-04", None))]) == [5, 6]
    # Not empty and not in a range: both exclusions apply.
    assert _traps([q.Filter("ts", "not_null"), q.Filter("ts", "not_between", ("2026-03-02", None))]) == [1, 2]
    assert _traps([q.Filter("amount", "not_null"), q.Filter("kind", "is_null")]) == []
    with pytest.raises(q.NotAllowed, match="takes no value"):
        q.count(TRAPS, DUCK, {}, [q.Filter("kind", "is_null", ("a",))])


def test_default_view_is_what_the_board_opens_on(sales):
    stmt = q.default_view(sales, DUCK, {"start": date(2026, 1, 1), "end": date(2026, 2, 1)})
    assert "region" in stmt.sql.split("GROUP BY")[1] and "sum_amount" in stmt.sql
    pie = sales.model_copy(update={"view": sales.view.model_copy(update={"chart": ChartType.PIE})})
    pie_sql = q.default_view(pie, DUCK, {"start": date(2026, 1, 1), "end": date(2026, 2, 1)}).sql
    assert "region" not in pie_sql.split("GROUP BY")[1]                   # a pie has one dimension: no column split


def test_empty_string_is_a_value_not_null():
    stmt = q.count(TRAPS, DUCK, {}, [q.Filter("kind", "in", ("",))])
    assert "IS NULL" not in stmt.sql and "" in stmt.binds.values()
    stmt = q.count(TRAPS, DUCK, {}, [q.Filter("kind", "not_in", ("",))])
    assert "kind IS NULL OR kind NOT IN" in stmt.sql and "" in stmt.binds.values()
    assert "amount IS NULL" in q.count(TRAPS, DUCK, {}, [q.Filter("amount", "in", ("",))]).sql  # typed: empty


TS_RANGE = Report.model_validate({
    "id": "ts", "title": "TS", "sql": "select timestamp '2026-03-01' as ts, date '2026-03-01' as d",
    "parameters": [{"name": "start", "type": "date"}, {"name": "end", "type": "date"}],
    "ranges": [{"start": "start", "end": "end", "field": "ts"}],
    "fields": [{"name": "ts", "type": "timestamp", "filterable": True, "groupable": True, "grains": "all"},
               {"name": "d", "type": "date", "filterable": True, "groupable": True, "grains": ["month", "year"]}],
    "view": {"rows": ["ts"], "grains": {"ts": "month"}},
})
MARCH = {"start": date(2026, 3, 1), "end": date(2026, 3, 31)}


def test_timeframe_from_the_range_then_a_filter():
    first, last = q.timeframe(TS_RANGE, "ts", MARCH)
    assert (first.isoformat(), last.isoformat()) == ("2026-03-01T00:00:00", "2026-03-31T23:59:59.999999")
    one_day = [q.Filter("ts", "between", (date(2026, 3, 10), date(2026, 3, 10)))]
    assert q.default_grain(TS_RANGE, "ts", MARCH) is Grain.DAY
    assert q.default_grain(TS_RANGE, "ts", MARCH, one_day) is Grain.HOUR           # the filter narrows the range
    hour = [q.Filter("ts", "between", ("2026-03-10T10:00Z", "2026-03-10T10:30Z"))]
    assert q.default_grain(TS_RANGE, "ts", MARCH, hour) is Grain.MINUTE
    either = [*one_day, q.Filter("ts", "is_null")]                                 # alternatives: no narrowing
    assert q.default_grain(TS_RANGE, "ts", MARCH, either) is Grain.DAY


def test_default_grain_without_a_period():
    assert q.timeframe(TS_RANGE, "ts", {"start": None, "end": None}) is None
    assert q.default_grain(TS_RANGE, "ts", {}) is Grain.MONTH                      # the view's grain
    no_field = TS_RANGE.model_copy(update={"ranges": ()})
    assert q.default_grain(no_field, "d", MARCH) is Grain.MONTH                     # the field's first grain
    assert q.default_grain(TS_RANGE, "d", MARCH) is Grain.MONTH                     # the range is on another field
    in_sql = TS_RANGE.model_copy(update={"ranges": (TS_RANGE.ranges[0].model_copy(update={"field": None}),)})
    assert q.default_grain(in_sql, "ts", {"start": date(2020, 1, 1), "end": date(2024, 12, 31)}) is Grain.MONTH  # 60 months


def test_default_view_opens_on_the_period_grain(sales):
    stmt = q.default_view(sales, DUCK, {"start": date(2026, 1, 1), "end": date(2026, 1, 31)})
    assert "date_trunc('day'" in stmt.sql
