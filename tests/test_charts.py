"""Chart specs: periods on a band axis for bars, one tick per period on a time axis, the palette."""

import pandas as pd
import pytest

from yamlboard import charts, times
from yamlboard import query as q
from yamlboard.schema import Aggregation, ChartType, Grain

MONTHS = pd.to_datetime(["2026-03-01", "2026-04-01", "2026-03-01", "2026-04-01"]).tz_localize("UTC")


@pytest.mark.parametrize("grain, expected", [
    (Grain.MONTH, "2026-03"), (Grain.QUARTER, "2026-Q1"), (Grain.WEEK, "2026-W09"), (Grain.YEAR, "2026"),
    (Grain.DAY, "2026-03-01"), (Grain.HOUR, "2026-03-01 00:00"),
])
def test_period_label(grain, expected):
    assert times.period_label(pd.Series(MONTHS[:1]), grain).tolist() == [expected]


def test_period_label_of_an_empty_value():
    assert times.period_label(pd.Series([MONTHS[0], pd.NaT]), "month").tolist() == ["2026-03", "(empty)"]


@pytest.fixture
def sales(ws):
    return ws.reports["sales"]


@pytest.fixture
def df():
    return pd.DataFrame({"order_date": MONTHS, "region": ["North", "North", "South", "South"],
                         "sum_amount": [1.0, 2.0, 3.0, 4.0]})


def test_stacked_columns_by_month_sit_on_a_band_axis(sales, df):
    spec = charts.build(df, ChartType.COLUMN_STACKED, sales, "order_date", q.Measure("amount", Aggregation.SUM),
                        "region", Grain.MONTH).to_dict()
    assert spec["encoding"]["x"]["type"] == "ordinal"
    assert sorted({r["order_date"] for r in spec["datasets"][spec["data"]["name"]]}) == ["2026-03", "2026-04"]
    assert spec["encoding"]["color"]["scale"] == {"domain": ["North", "South"], "range": charts.PALETTE[:2]}
    assert spec["encoding"]["color"]["legend"]["orient"] == "top"


def test_a_line_by_month_has_one_tick_per_month(sales, df):
    spec = charts.build(df, ChartType.LINE, sales, "order_date", q.Measure("amount", Aggregation.SUM), "region",
                        Grain.MONTH).to_dict()
    axis = spec["encoding"]["x"]["axis"]
    assert spec["encoding"]["x"]["type"] == "temporal" and axis["format"] == "%Y-%m"
    assert axis["values"] == [1772323200000, 1775001600000]   # 2026-03-01, 2026-04-01 UTC


def test_one_series_is_drawn_in_the_first_colour(sales, df):
    spec = charts.build(df, ChartType.COLUMN, sales, "region", q.Measure("amount", Aggregation.SUM)).to_dict()
    assert spec["mark"]["color"] == charts.PALETTE[0] and "color" not in spec["encoding"]


def test_title(sales):
    m = q.Measure("amount", Aggregation.SUM)
    assert charts.title(sales, m, "order_date", Grain.MONTH, "region") == \
        "Sum of Amount by Order date (month), split by Region"
    assert charts.title(sales, q.Measure(), None) == "Row count"
