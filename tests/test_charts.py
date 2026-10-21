"""Chart specs: periods on a band axis for bars, one tick per period on a time axis, the palette."""

from datetime import datetime

import pandas as pd
import pytest

from yamlboard import charts, times
from yamlboard import query as q
from yamlboard.schema import Aggregation, AxisDef, AxisSide, ChartFormat, ChartType, Empty, Grain, Series, Units

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
    assert axis["values"] == ["2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z"]   # not epoch ms: no label drawn


def test_one_series_is_drawn_in_the_first_colour(sales, df):
    spec = charts.build(df, ChartType.COLUMN, sales, "region", q.Measure("amount", Aggregation.SUM)).to_dict()
    assert spec["mark"]["color"] == charts.PALETTE[0] and "color" not in spec["encoding"]


def test_title(sales):
    m = q.Measure("amount", Aggregation.SUM)
    assert charts.title(sales, m, "order_date", Grain.MONTH, "region") == \
        "Sum of Amount by Order date (month), split by Region"
    assert charts.title(sales, q.Measure(), None) == "Row count"


def test_a_pie_slice_shows_its_share_of_the_whole(sales, df):
    spec = charts.build(df, ChartType.DONUT, sales, "region", q.Measure("amount", Aggregation.SUM)).to_dict()
    assert spec["transform"][0] == {"joinaggregate": [{"op": "sum", "field": "sum_amount", "as": charts.TOTAL}]}
    assert "format(datum['sum_amount'] / datum['__total'], '.1%')" in spec["transform"][1]["calculate"]
    assert {"field": charts.SHARE, "title": "Sum of Amount", "type": "nominal"} in spec["encoding"]["tooltip"]


def test_a_stacked_column_shows_each_value_s_share_of_its_column(sales, df):
    spec = charts.build(df, ChartType.COLUMN_STACKED, sales, "order_date", q.Measure("amount", Aggregation.SUM),
                        "region", Grain.MONTH).to_dict()
    assert spec["transform"][0]["joinaggregate"][0]["field"] == "sum_amount"
    assert spec["transform"][0]["groupby"] == ["order_date"]
    assert {"field": charts.SHARE, "title": "Sum of Amount", "type": "nominal"} in spec["encoding"]["tooltip"]


def test_unsplit_stacked_columns_show_no_share(sales, df):
    spec = charts.build(df, ChartType.COLUMN_STACKED, sales, "region", q.Measure("amount", Aggregation.SUM)).to_dict()
    assert "transform" not in spec


@pytest.mark.parametrize("kind, axis", [(ChartType.COLUMN_PERCENT, "y"), (ChartType.BAR_PERCENT, "x")])
def test_a_percent_chart_stacks_each_column_to_100_percent(sales, df, kind, axis):
    spec = charts.build(df, kind, sales, "order_date", q.Measure("amount", Aggregation.SUM), "region",
                        Grain.MONTH).to_dict()
    measure = spec["encoding"][axis]
    assert measure["stack"] == "normalize" and measure["axis"]["format"] == "%"
    assert measure["title"] == "Share of sum of Amount"
    assert spec["transform"][0]["groupby"] == ["order_date"]


def test_a_split_area_shows_each_value_s_share_of_its_period(sales, df):
    spec = charts.build(df, ChartType.AREA, sales, "order_date", q.Measure("amount", Aggregation.SUM), "region",
                        Grain.MONTH).to_dict()
    assert spec["transform"][0]["groupby"] == ["order_date"]
    assert {"field": charts.SHARE, "title": "Sum of Amount", "type": "nominal"} in spec["encoding"]["tooltip"]


def test_what_a_click_selects():
    assert charts.selections(ChartType.COLUMN_STACKED, "month", "region") == {"click": ["month", "region"],
                                                                              "legend": ["region"]}
    assert charts.selections(ChartType.DONUT, "region", "product") == {"click": ["region"], "legend": ["region"]}
    assert charts.selections(ChartType.LINE, "month", "region") == {"click": ["region"], "legend": ["region"]}
    assert charts.selections(ChartType.LINE, "month", None) == {}       # one line: nothing to pick


def test_a_selectable_chart_declares_its_selections(sales, df):
    spec = charts.build(df, ChartType.COLUMN_STACKED, sales, "order_date", q.Measure("amount", Aggregation.SUM),
                        "region", Grain.MONTH, selectable=True).to_dict()
    params = {p["name"]: p["select"] for p in spec["params"]}
    assert params["click"]["fields"] == ["order_date", "region"] and params["legend"]["fields"] == ["region"]
    assert next(p for p in spec["params"] if p["name"] == "legend")["bind"] == "legend"


def test_a_click_reads_back_the_result_values(sales, df):
    df = pd.concat([df, pd.DataFrame({"order_date": [MONTHS[0]], "region": [None], "sum_amount": [5.0]})])
    state = {"click": [{"order_date": "2026-03", "region": "(empty)"}], "legend": [{"region": "North"}]}
    assert charts.picked(df, sales, state, "order_date", "region", Grain.MONTH) == [
        ("order_date", MONTHS[0], Grain.MONTH), ("region", None, None), ("region", "North", None)]


# --- empty cells ---------------------------------------------------------------------------------------------

DAYS = pd.to_datetime(["2026-03-02", "2026-03-04"]).tz_localize("UTC")
COUNT, AMOUNT, AVG_QTY = q.Measure(), q.Measure("amount", Aggregation.SUM), q.Measure("quantity", Aggregation.AVG)


def test_fill_adds_the_periods_with_no_rows_from_the_start_of_the_range_to_now(sales):
    df = pd.DataFrame({"order_date": DAYS, "count": [3, 4], "avg_quantity": [2.0, 5.0]})
    out = charts.fill(df, ChartType.LINE, sales, "order_date", [COUNT, AVG_QTY], x_grain=Grain.DAY,
                      span=(datetime(2026, 3, 1), datetime(2026, 3, 31)), at=datetime(2026, 3, 5, 9))
    assert out["order_date"].dt.strftime("%d").tolist() == ["01", "02", "03", "04", "05"]   # not past now
    assert out["count"].isna().tolist() == [True, False, True, False, True]                # gaps
    zero = charts.fill(df, ChartType.LINE, sales, "order_date", [COUNT, AVG_QTY], x_grain=Grain.DAY,
                       empty=Empty.ZERO)                                                   # no range: the data's
    assert zero["count"].tolist() == [3, 0, 4] and zero["avg_quantity"].isna().tolist() == [False, True, False]


def test_fill_trimmed_runs_over_the_data_s_periods_only(sales):
    df = pd.DataFrame({"order_date": DAYS, "count": [3, 4], "avg_quantity": [2.0, 5.0]})
    out = charts.fill(df, ChartType.LINE, sales, "order_date", [COUNT, AVG_QTY], x_grain=Grain.DAY,
                      span=(datetime(2026, 1, 1), datetime(2026, 12, 31)), at=datetime(2026, 3, 30), trim=True)
    assert out["order_date"].dt.strftime("%d").tolist() == ["02", "03", "04"]   # the gap between is kept
    assert out["count"].isna().tolist() == [False, True, False]


def test_fill_puts_each_split_value_at_each_row_group(sales):
    df = pd.DataFrame({"region": ["North", "South", None], "product": ["Widget", "Gizmo", "Widget"],
                       "sum_amount": [1.0, 2.0, 3.0]})
    out = charts.fill(df, ChartType.COLUMN, sales, "region", [AMOUNT], "product", empty=Empty.ZERO)
    assert len(out) == 6 and out["sum_amount"].sum() == 6 and (out["sum_amount"] == 0).sum() == 3
    assert out["region"].isna().sum() == 2                                                 # the empty group too


def test_fill_leaves_a_pie_and_a_chart_with_nothing_to_fill_as_they_are(sales, df):
    assert charts.fill(df, ChartType.PIE, sales, "region", [AMOUNT], "order_date") is df
    one = df.drop(columns="order_date").drop_duplicates("region")
    assert charts.fill(one, ChartType.COLUMN, sales, "region", [AMOUNT]) is one


def test_fill_gives_up_past_the_groups_a_chart_holds(sales):
    df = pd.DataFrame({"order_date": DAYS, "count": [3, 4]})
    assert charts.fill(df, ChartType.LINE, sales, "order_date", [COUNT], x_grain=Grain.MINUTE,
                       span=(datetime(2026, 1, 1), datetime(2026, 3, 1))) is None


def test_an_empty_period_keeps_its_band_and_a_line_breaks_or_connects(sales):
    df = charts.fill(pd.DataFrame({"order_date": DAYS, "count": [3, 4]}), ChartType.LINE, sales, "order_date",
                     [COUNT], x_grain=Grain.DAY)
    for kind, channel in ((ChartType.COLUMN, "x"), (ChartType.BAR_STACKED, "y")):
        spec = charts.build(df, kind, sales, "order_date", COUNT, x_grain=Grain.DAY).to_dict()
        assert spec["encoding"][channel]["scale"]["domain"] == ["2026-03-02", "2026-03-03", "2026-03-04"]
    assert "invalid" not in charts.build(df, ChartType.LINE, sales, "order_date", COUNT, x_grain=Grain.DAY).to_dict()[
        "mark"]                                                                             # Vega-Lite's: it breaks
    connect = ChartFormat(empty=Empty.CONNECT)
    assert charts.build(df, ChartType.LINE, sales, "order_date", COUNT, x_grain=Grain.DAY,
                        fmt=connect).to_dict()["mark"]["invalid"] == "filter"


# --- several measures ----------------------------------------------------------------------------------------

@pytest.fixture
def two(df):
    return df.drop(columns="region").assign(count=[1, 2, 3, 4])


def test_several_measures_are_series_of_one_legend_side_by_side(sales, two):
    spec = charts.build(two, ChartType.COLUMN, sales, "order_date", [AMOUNT, COUNT], x_grain=Grain.MONTH).to_dict()
    assert len(spec["layer"]) == 1 and "resolve" not in spec
    enc = spec["layer"][0]["encoding"]
    assert enc["color"]["scale"]["domain"] == ["Sum of Amount", "Row count"]
    assert enc["xOffset"]["field"] == charts.MEASURE and enc["y"]["title"] == "Sum of Amount and Row count"
    rows = spec["datasets"][spec["data"]["name"]]
    assert len(rows) == 8 and {r[charts.MEASURE] for r in rows} == {"Sum of Amount", "Row count"}
    assert charts.legend(two, ChartType.COLUMN, sales, "order_date", None, Grain.MONTH, None, [AMOUNT, COUNT]) == (
        charts.MEASURE, ["Sum of Amount", "Row count"])


def test_a_combo_draws_a_measure_as_a_line_on_the_secondary_axis(sales, two):
    fmt = ChartFormat(series={"count": Series(ChartType.LINE, AxisSide.SECONDARY)},
                      axes={AxisSide.SECONDARY: AxisDef(units=Units.THOUSANDS)})
    spec = charts.build(two, ChartType.COLUMN, sales, "order_date", [AMOUNT, COUNT], x_grain=Grain.MONTH,
                        selectable=True, fmt=fmt).to_dict()
    assert spec["resolve"] == {"scale": {"y": "independent"}}
    bars, line = spec["layer"]
    assert bars["mark"]["type"] == "bar" and line["mark"]["type"] == "line"
    assert line["encoding"]["y"]["axis"]["labelExpr"] == "format(datum.value / 1000, ',.2~f') + \" k\""
    assert "xOffset" not in bars["encoding"]                                  # one column measure: no offset
    assert spec["params"] == [{"name": "click", "select": {"type": "point", "clear": False, "fields": ["order_date"],
                                                           "on": "click"}, "views": [bars["name"]]}]


def test_a_secondary_axis_needs_a_primary_one(sales, two):
    fmt = ChartFormat(series={a: Series(axis=AxisSide.SECONDARY) for a in ("sum_amount", "count")})
    spec = charts.build(two, ChartType.LINE, sales, "order_date", [AMOUNT, COUNT], x_grain=Grain.MONTH,
                        fmt=fmt).to_dict()
    assert "resolve" not in spec


def test_a_bar_chart_keeps_its_bars_and_a_pie_its_first_measure(sales, two):
    fmt = ChartFormat(series={"count": Series(ChartType.LINE)})
    marks = charts.marks(ChartType.BAR_STACKED, [AMOUNT, COUNT], fmt)
    assert marks == {"sum_amount": ChartType.BAR, "count": ChartType.BAR}
    assert charts.selections(ChartType.BAR_STACKED, "region", None, marks) == {"click": ["region"]}
    assert charts.selections(ChartType.LINE, "region", None, charts.marks(ChartType.LINE, [AMOUNT, COUNT])) == {}
    pie = charts.build(two, ChartType.PIE, sales, "order_date", [AMOUNT, COUNT], x_grain=Grain.MONTH).to_dict()
    assert pie["mark"]["type"] == "arc" and pie["encoding"]["theta"]["field"] == "sum_amount"


def test_title_of_several_measures():
    assert charts._and(["A"]) == "A" and charts._and(["A", "B", "C"]) == "A, B and C"


# --- axes ----------------------------------------------------------------------------------------------------

def test_axis_bounds_and_a_log_scale(sales, df):
    fmt = ChartFormat(axes={AxisSide.PRIMARY: AxisDef(min=1, max=10, log=True)})
    spec = charts.build(df.assign(sum_amount=[0.0, 2.0, 3.0, -4.0]), ChartType.COLUMN, sales, "region", AMOUNT,
                        fmt=fmt).to_dict()
    assert spec["encoding"]["y"]["scale"] == {"domainMin": 1.0, "domainMax": 10.0, "type": "log"}
    assert spec["mark"]["clip"] is True
    assert [r["sum_amount"] for r in spec["datasets"][spec["data"]["name"]]] == [None, 2.0, 3.0, None]  # no zero


def test_a_percent_chart_keeps_its_axis(sales, df):
    fmt = ChartFormat(axes={AxisSide.PRIMARY: AxisDef(max=5, units=Units.MILLIONS)})
    spec = charts.build(df, ChartType.COLUMN_PERCENT, sales, "order_date", AMOUNT, "region", Grain.MONTH,
                        fmt=fmt).to_dict()
    assert spec["encoding"]["y"]["axis"] == {"format": "%"} and "scale" not in spec["encoding"]["y"]


@pytest.mark.parametrize("axis, expected", [
    (AxisDef(), None),
    (AxisDef(prefix="$"), '"$" + datum.label'),
    (AxisDef(decimals=0), "format(datum.value, ',.0f')"),
    (AxisDef(units=Units.MILLIONS, decimals=1, suffix=" €"), "format(datum.value / 1000000, ',.1f') + \" M\" + \" €\""),
    (AxisDef(units=Units.BILLIONS), "format(datum.value / 1000000000, ',.2~f') + \" bn\""),
    (AxisDef(units=Units.PERCENT), "format(datum.value, '.2~%')"),
])
def test_axis_labels(axis, expected):
    assert charts.label_expr(axis) == expected
