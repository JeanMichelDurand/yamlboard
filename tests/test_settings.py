"""Settings files: what is exported, and what an import keeps or skips."""

import json
from datetime import date, datetime

import pandas as pd
import pytest

from yamlboard import query as q
from yamlboard import settings
from yamlboard.schema import AxisDef, AxisSide, ChartType, Empty, Series, Units


@pytest.fixture
def reports(ws):
    return ws.reports


def test_export_reads_each_report_keys(reports):
    state = {
        "advanced": True, "report": "sales",
        "sales:mode:advanced": ["Chart", "Detail"], "sales:chart": ChartType.BAR, "sales:row": "region",
        "sales:col": None, "sales:measures": ["sum_amount"], "sales:page_size": 10, "sales:detail_cols": ["region"],
        "sales:filters": [q.Filter("order_date", "between", (date(2026, 1, 1), None)),
                          q.Filter("quantity", "in", (pd.Series([3]).iloc[0],))],
        "unrelated": 1,
    }
    doc = settings.export(state, reports, "sales")
    assert doc["report"] == "sales" and doc["advanced"] is True
    sales = doc["reports"]["sales"]
    assert sales["views"] == {"advanced": ["Chart", "Detail"]}
    assert sales["chart"] == "bar" and sales["column"] is None and sales["page_size"] == 10
    assert sales["measures"] == ["sum_amount"]
    assert sales["filters"][0]["values"] == ["2026-01-01", None]
    assert json.loads(settings.dumps(doc))["reports"]["sales"]["filters"][1]["values"] == [3]  # numpy -> JSON
    assert set(doc["reports"]) == {"sales"}                       # a report never opened has nothing


def test_restore_types_values_back(reports):
    doc = {"yamlboard_settings": 1, "report": "sales", "reports": {"sales": {
        "chart": "donut", "row": "region", "measure": "count", "views": {"basic": ["Detail", "Chart"]},
        "filters": [{"field": "order_date", "op": "between", "values": ["2026-01-01", "2026-02-01"]}],
    }}}
    keys, skipped = settings.restore(json.dumps(doc), reports)
    assert not skipped
    assert keys["report"] == "sales" and keys["sales:chart"] is ChartType.DONUT
    assert keys["sales:mode:basic"] == ["Chart", "Detail"]        # the board's order
    assert keys["sales:filters"][0].values == (date(2026, 1, 1), date(2026, 2, 1))


def test_restore_skips_what_a_report_no_longer_allows(reports):
    doc = {"yamlboard_settings": 1, "reports": {
        "gone": {"chart": "pie"},
        "sales": {"chart": "radar", "row": "nope", "measure": "sum_region", "page_size": 7,
                  "views": {"basic": ["Pivot"]}, "columns": ["region", "nope"],
                  "filters": [{"field": "nope", "op": "in", "values": [1]},
                              {"field": "order_date", "op": "in", "values": ["not a date"]},
                              {"field": "region", "op": "in", "values": ["North"]}]},
    }}
    keys, skipped = settings.restore(json.dumps(doc), reports)
    assert len(skipped) == 9
    assert keys == {"sales:filters": [q.Filter("region", "in", ("North",))]}


def test_timestamps_round_trip(reports):
    ts = pd.Timestamp("2026-03-01 08:00", tz="UTC")
    report = reports["ash-activity"]
    assert report.field_map["sample_time_utc"].type.value == "timestamp"
    doc = settings.export({"ash-activity:filters": [q.Filter("sample_time_utc", "in", (ts,))]}, reports)
    assert doc["reports"]["ash-activity"]["filters"][0]["values"] == ["2026-03-01T08:00:00Z"]
    keys, _ = settings.restore(settings.dumps(doc), reports)
    assert keys["ash-activity:filters"][0].values == (datetime(2026, 3, 1, 8),)  # naive UTC, as the queries bind


@pytest.mark.parametrize("text", ["nope", "[]", '{"reports": {}}', '{"yamlboard_settings": 2, "reports": {}}'])
def test_not_a_settings_file(text, reports):
    with pytest.raises(settings.SettingsError):
        settings.restore(text, reports)


def test_malformed_settings_are_skipped_not_raised(reports):
    doc = {"yamlboard_settings": 1, "reports": {
        "sales": {"views": ["Chart"], "grains": 3, "filters": [
            {"field": "order_date", "op": "between", "values": ["2026-01-01"]},   # one bound of two
            "region",
            {"field": "region", "op": "in", "values": ["North"]}]},
        "ash-activity": {"chart": ["pie"], "measure": "count"}}}                  # unhashable: none applied
    keys, skipped = settings.restore(json.dumps(doc), reports)
    assert keys == {"sales:filters": [q.Filter("region", "in", ("North",))]}
    assert len(skipped) == 5 and any("takes 2 values" in s for s in skipped)
    assert any(s.startswith("ash-activity: malformed settings") for s in skipped)


def test_restore_skips_a_value_the_field_cannot_hold(reports):
    doc = {"yamlboard_settings": 1, "reports": {"sales": {"filters": [
        {"field": "amount", "op": "in", "values": ["abc"]},
        {"field": "amount", "op": "between", "values": [1, 5]}]}}}
    keys, skipped = settings.restore(json.dumps(doc), reports)
    assert [f.op for f in keys["sales:filters"]] == ["between"]
    assert len(skipped) == 1 and "not a valid number" in skipped[0]


def test_colours_and_periods_round_trip(reports):
    state = {"sales:colors": {"region": {"North": "#d00000"}, "*": {"*": "#174d98"}, "product": {}},
             "sales:period:start": "last_month"}
    doc = settings.export(state, reports)
    assert doc["reports"]["sales"]["colors"] == {"region": {"North": "#d00000"}, "*": {"*": "#174d98"}}
    assert doc["reports"]["sales"]["periods"] == {"start": "last_month"}
    keys, skipped = settings.restore(settings.dumps(doc), reports)
    assert not skipped
    assert keys["sales:colors"] == {"region": {"North": "#d00000"}, "*": {"*": "#174d98"}}
    assert keys["sales:period:start"] == "last_month"


def test_bad_colours_and_periods_are_skipped(reports):
    doc = {"yamlboard_settings": 1, "reports": {"sales": {
        "colors": {"region": {"North": "red", "South": "#00f"}, "nope": {"x": "#000000"}},
        "periods": {"start": "last_century", "nope": "last_day"}}}}
    keys, skipped = settings.restore(json.dumps(doc), reports)
    assert keys["sales:colors"] == {"region": {"South": "#0000ff"}}
    assert "sales:period:start" not in keys
    assert len(skipped) == 4


def test_the_number_format_round_trips_and_a_bad_one_is_skipped(reports):
    doc = settings.export({"sales:decimals": 0, "sales:number_style": "1 234,56"}, reports)
    assert doc["reports"]["sales"] == {"decimals": 0, "number_style": "1 234,56"}
    keys, skipped = settings.restore(settings.dumps(doc), reports)
    assert keys == {"sales:decimals": 0, "sales:number_style": "1 234,56"} and not skipped
    bad = {"yamlboard_settings": 1, "reports": {"sales": {"decimals": 9, "number_style": "1.234,56"}}}
    keys, skipped = settings.restore(json.dumps(bad), reports)
    assert not keys and len(skipped) == 2


def test_hide_partial_round_trips_and_a_bad_one_is_skipped(reports):
    doc = settings.export({"sales:hide_partial": True}, reports)
    assert doc["reports"]["sales"] == {"hide_partial": True}
    keys, skipped = settings.restore(settings.dumps(doc), reports)
    assert keys == {"sales:hide_partial": True} and not skipped
    bad = {"yamlboard_settings": 1, "reports": {"sales": {"hide_partial": "yes"}}}
    keys, skipped = settings.restore(json.dumps(bad), reports)
    assert not keys and len(skipped) == 1


def test_trim_empty_round_trips_and_a_bad_one_is_skipped(reports):
    doc = settings.export({"sales:trim_empty": True}, reports)
    assert doc["reports"]["sales"] == {"trim_empty": True}
    keys, skipped = settings.restore(settings.dumps(doc), reports)
    assert keys == {"sales:trim_empty": True} and not skipped
    bad = {"yamlboard_settings": 1, "reports": {"sales": {"trim_empty": 1}}}
    keys, skipped = settings.restore(json.dumps(bad), reports)
    assert not keys and len(skipped) == 1


def test_the_sort_keys_round_trip_and_an_old_single_sort_still_opens(reports):
    doc = settings.export({"sales:order": [("region", False), ("amount", True)]}, reports)
    assert doc["reports"]["sales"] == {"sort": [{"field": "region", "descending": False},
                                                {"field": "amount", "descending": True}]}
    keys, skipped = settings.restore(settings.dumps(doc), reports)
    assert keys == {"sales:order": [("region", False), ("amount", True)]} and not skipped
    old = {"yamlboard_settings": 1, "reports": {"sales": {"sort": "amount", "descending": True}}}
    assert settings.restore(json.dumps(old), reports) == ({"sales:order": [("amount", True)]}, [])
    for bad in ([{"field": "nope"}], [{"field": "amount", "descending": "yes"}], [{"field": "amount"}] * 2):
        keys, skipped = settings.restore(json.dumps({"yamlboard_settings": 1, "reports": {"sales": {"sort": bad}}}),
                                         reports)
        assert not keys and len(skipped) == 1


def test_view_of_writes_the_shown_view_and_view_keys_read_it_back(reports):
    sales = reports["sales"]
    state = {"sales:chart": ChartType.BAR, "sales:row": "region", "sales:col": "order_date",
             "sales:measures": ["count"], "sales:grain:order_date": "week", "sales:hide_partial": True,
             "sales:colors": {settings.SERIES: {settings.SERIES: "#112233"}, "region": {"North": "#000000"}},
             "sales:filters": [q.Filter("order_date", "between", (pd.Timestamp("2026-01-01"), None)),
                               q.Filter("channel", "is_null")]}
    view = settings.view_of(state, sales)
    assert view == {"rows": ["region"], "columns": ["order_date"], "grains": {"order_date": "week"},
                    "measures": [{"field": "*"}], "chart": "bar", "color": "#112233",
                    "filters": [{"field": "order_date", "op": "between", "values": [date(2026, 1, 1), None]},
                                {"field": "channel", "op": "is_null"}],
                    "hide_partial": True}
    written = sales.model_copy(update={"view": type(sales.view).model_validate(view)})
    keys = settings.view_keys(written)
    assert {k: keys[k] for k in ("sales:chart", "sales:row", "sales:col", "sales:measures", "sales:hide_partial")} == {
        "sales:chart": ChartType.BAR, "sales:row": "region", "sales:col": "order_date", "sales:measures": ["count"],
        "sales:hide_partial": True}
    assert keys["sales:filters"] == [q.Filter("order_date", "between", (date(2026, 1, 1), None)),
                                     q.Filter("channel", "is_null")]
    assert settings.view_of(keys, written) == view | {"color": "#112233"}  # the series colour is the view's now


def test_view_of_keeps_what_the_board_never_drew(reports):
    sales = reports["sales"]
    view = settings.view_of({}, sales)
    assert view == {"rows": ["order_date"], "columns": ["region"], "grains": {"order_date": "month"},
                    "measures": [{"field": "amount", "aggregation": "sum"}], "chart": "line"}
    assert "filters" not in settings.view_of({"sales:filters": []}, sales)
    assert "columns" not in settings.view_of({"sales:col": None}, sales)   # the split cleared on the board


def test_several_measures_and_the_chart_format_round_trip(reports):
    state = {"sales:measures": ["sum_amount", "count"], "sales:empty": Empty.ZERO,
             "sales:series_chart:count": ChartType.LINE, "sales:series_axis:count": AxisSide.SECONDARY,
             "sales:axis:secondary:units": Units.THOUSANDS, "sales:axis:secondary:prefix": "$",
             "sales:axis:primary:min": 0.0, "sales:axis:primary:log": False}
    doc = settings.export(state, reports)
    sales = doc["reports"]["sales"]
    assert sales["measures"] == ["sum_amount", "count"] and sales["empty"] == "zero"
    assert sales["series"] == {"count": {"chart": "line", "axis": "secondary"}}
    assert sales["axes"] == {"primary": {"min": 0.0}, "secondary": {"units": "thousands", "prefix": "$"}}
    keys, skipped = settings.restore(settings.dumps(doc), reports)
    assert not skipped
    fmt, problems = settings.format_of(keys, reports["sales"])
    assert fmt == settings.format_of(state, reports["sales"])[0] and not problems
    assert fmt.series["count"] == Series(ChartType.LINE, AxisSide.SECONDARY) and fmt.empty is Empty.ZERO
    assert fmt.axes == {AxisSide.PRIMARY: AxisDef(min=0), AxisSide.SECONDARY: AxisDef(units=Units.THOUSANDS, prefix="$")}


def test_a_file_with_one_measure_opens_on_it(reports):
    doc = {"yamlboard_settings": 1, "reports": {"sales": {"measure": "count"}}}
    assert settings.restore(json.dumps(doc), reports)[0] == {"sales:measures": ["count"]}


def test_bad_measures_and_formats_are_skipped(reports):
    doc = {"yamlboard_settings": 1, "reports": {"sales": {
        "measures": ["count", "sum_region"], "empty": "blank",
        "series": {"count": {"chart": "pie"}, "nope": {"axis": "primary"}, "sum_amount": {"axis": "left"}},
        "axes": {"primary": {"min": 5, "max": 1}, "secondary": {"log": True}, "middle": {}}}}}
    keys, skipped = settings.restore(json.dumps(doc), reports)
    assert len(skipped) == 7
    assert "sales:measures" not in keys and "sales:empty" not in keys
    assert not [k for k in keys if ":series_" in k]
    assert keys["sales:axis:secondary:log"] is True and keys["sales:axis:primary:min"] is None  # the default


def test_an_axis_the_board_cannot_take_is_drawn_from_the_data_and_said_so(reports):
    fmt, problems = settings.format_of({"sales:axis:primary:min": 10.0, "sales:axis:primary:max": 2.0},
                                       reports["sales"])
    assert AxisSide.PRIMARY not in fmt.axes and problems == ["Primary axis: the axis's min (10) is not below its max (2)"]


def test_view_of_writes_the_measures_their_charts_and_the_format(reports):
    sales = reports["sales"]
    state = {"sales:measures": ["sum_amount", "count"], "sales:col": None, "sales:empty": Empty.CONNECT,
             "sales:trim_empty": True, "sales:series_chart:count": ChartType.LINE, "sales:series_axis:count": AxisSide.SECONDARY,
             "sales:axis:secondary:decimals": 1}
    view = settings.view_of(state, sales)
    assert view["measures"] == [{"field": "amount", "aggregation": "sum"},
                                {"field": "*", "chart": "line", "axis": "secondary"}]
    assert view["empty"] == "connect" and view["trim_empty"] is True and view["axes"] == {"secondary": {"decimals": 1}}
    written = sales.model_copy(update={"view": type(sales.view).model_validate(view)})
    keys = settings.view_keys(written)
    assert keys["sales:measures"] == ["sum_amount", "count"] and keys["sales:empty"] is Empty.CONNECT
    assert keys["sales:trim_empty"] is True
    assert keys["sales:series_axis:count"] is AxisSide.SECONDARY and keys["sales:axis:secondary:decimals"] == 1
    assert settings.view_of(keys, written) == view
