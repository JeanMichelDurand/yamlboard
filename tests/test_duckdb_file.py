"""The query builders on real tables of a DuckDB file, not on SQL that generates its own rows."""

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from yamlboard import query as q
from yamlboard.engine import columns_for, dialect_of, run
from yamlboard.schema import Aggregation, Grain, parse_report

ORDERS = """
id: orders
title: Orders
parameters:
  - {name: start, type: date}
  - {name: end, type: date}
ranges:
  - {start: start, end: end, field: createdat}
sql: |
  select o.order_id, o.order_date, o."CreatedAt", o.region, o.channel, o.amount, o.paid, c.segment
  from sales.orders o join sales.customers c using (customer_id)
fields:
  - {name: order_id, type: integer, measures: [count_distinct]}
  - {name: order_date, type: date, filterable: true, groupable_rows: true, grains: [day, month]}
  - {name: createdat, type: timestamp, filterable: true}
  - {name: region, filterable: true, groupable: true}
  - {name: channel, filterable: true, groupable: true}
  - {name: amount, type: number, filterable: true, measures: [sum, median]}
  - {name: paid, type: boolean, filterable: true, groupable_rows: true}
  - {name: segment, filterable: true, groupable: true}
view:
  rows: [order_date]
  grains: {order_date: month}
  measures: [{field: amount, aggregation: sum}]
"""


@pytest.fixture(scope="module")
def report():
    return parse_report(ORDERS)


@pytest.fixture(scope="module")
def cmap(warehouse, report):
    return columns_for(warehouse, report, {})


def direct(engine, sql: str) -> pd.DataFrame:
    with engine.connect() as con:
        return pd.read_sql(text(sql), con)


def test_columns_resolve_ignoring_case(cmap):
    assert cmap["createdat"] == "CreatedAt"
    assert cmap["order_date"] == "order_date"


def test_default_view_matches_direct_sql(warehouse, report, cmap):
    df = run(warehouse, q.default_view(report, dialect_of(warehouse), {}, columns_map=cmap))
    expected = direct(warehouse, "select date_trunc('month', order_date) m, sum(amount) s from sales.orders "
                                 "group by 1 order by 1")
    assert len(df) == 4
    assert df["sum_amount"].astype(float).tolist() == pytest.approx(expected["s"].astype(float).tolist())


def test_filters_on_a_join(warehouse, report, cmap):
    stmt = q.aggregate(report, dialect_of(warehouse), {},
                       filters=[q.Filter("region", "in", ("North",)), q.Filter("channel", "is_null")],
                       rows=[q.Group("segment")], columns_map=cmap)
    df = run(warehouse, stmt)
    expected = direct(warehouse, "select count(*) n from sales.orders where region = 'North' and channel is null")
    assert df["count"].sum() == expected["n"].iloc[0] > 0


def test_exclusion_keeps_nulls(warehouse, report, cmap):
    stmt = q.count(report, dialect_of(warehouse), {}, [q.Filter("channel", "not_in", ("web",))], columns_map=cmap)
    expected = direct(warehouse, "select count(*) n from sales.orders where channel is distinct from 'web'")
    assert run(warehouse, stmt)["n"].iloc[0] == expected["n"].iloc[0]


def test_range_on_a_timestamp_keeps_the_whole_end_day(warehouse, report, cmap):
    stmt = q.count(report, dialect_of(warehouse), {"start": "2026-01-01", "end": "2026-01-10"}, columns_map=cmap)
    assert run(warehouse, stmt)["n"].iloc[0] == 20  # two orders a day, 08:00 and 20:00


def test_grain_boolean_group_and_median(warehouse, report, cmap):
    stmt = q.aggregate(report, dialect_of(warehouse), {}, rows=[q.Group("order_date", Grain.DAY), q.Group("paid")],
                       measures=[q.Measure("amount", Aggregation.MEDIAN)], columns_map=cmap)
    df = run(warehouse, stmt)
    assert set(df["paid"]) == {True, False}
    assert df["order_date"].nunique() == 120


def test_detail_pages_and_distinct_values(warehouse, report, cmap):
    d = dialect_of(warehouse)
    page2 = run(warehouse, q.detail(report, d, {}, order_by="createdat", limit=50, offset=50, columns_map=cmap))
    assert len(page2) == 50 and page2["order_id"].tolist() == list(range(50, 100))
    values = run(warehouse, q.distinct_values(report, d, {}, "channel", columns_map=cmap))["channel"]
    assert values.isna().sum() == 1 and set(values.dropna()) == {"store", "web"}


def test_view_and_second_schema(warehouse):
    r = parse_report("id: n\ntitle: N\nsql: select region from sales.v_orders_north\n"
                     "fields: [{name: region, groupable: true}]\n")
    df = run(warehouse, q.aggregate(r, dialect_of(warehouse), {}, rows=[q.Group("region")]))
    assert df.to_dict("records") == [{"region": "North", "count": 60}]


def test_read_only_connection_rejects_writes(warehouse):
    with pytest.raises(SQLAlchemyError), warehouse.connect() as con:
        con.execute(text("insert into main.notes values (1, 'x')"))
