"""Settings files: what is exported, and what an import keeps or skips."""

import json
from datetime import date, datetime

import pandas as pd
import pytest

from yamlboard import query as q
from yamlboard import settings
from yamlboard.schema import ChartType


@pytest.fixture
def reports(ws):
    return ws.reports


def test_export_reads_each_report_keys(reports):
    state = {
        "advanced": True, "report": "sales",
        "sales:mode:advanced": ["Chart", "Detail"], "sales:chart": ChartType.BAR, "sales:row": "region",
        "sales:col": None, "sales:measure": "sum_amount", "sales:page_size": 10, "sales:detail_cols": ["region"],
        "sales:filters": [q.Filter("order_date", "between", (date(2026, 1, 1), None)),
                          q.Filter("quantity", "in", (pd.Series([3]).iloc[0],))],
        "unrelated": 1,
    }
    doc = settings.export(state, reports, "sales")
    assert doc["report"] == "sales" and doc["advanced"] is True
    sales = doc["reports"]["sales"]
    assert sales["views"] == {"advanced": ["Chart", "Detail"]}
    assert sales["chart"] == "bar" and sales["column"] is None and sales["page_size"] == 10
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
