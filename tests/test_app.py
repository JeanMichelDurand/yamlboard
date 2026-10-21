import json
from pathlib import Path

import pytest

from tests.conftest import EXAMPLES
from yamlboard import query as q
from yamlboard import times
from yamlboard.schema import AxisSide, ChartType, Empty, Grain, Units

APP = Path(__file__).parents[1] / "src" / "yamlboard" / "app.py"


@pytest.fixture
def app(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("YAMLBOARD_REPORTS", str(EXAMPLES))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["advanced"] = True  # most tests read the SQL; test_basic_mode turns it off
    at.session_state["report"] = "sales"  # the list opens on "Database activity", first by title
    return at


def test_default_view_renders(app):
    app.run()
    assert not app.exception and not app.error
    assert app.title[0].value == "Sales"
    assert app.get("arrow_vega_lite_chart") or app.get("vega_lite_chart")


def charts_of(app):
    return app.get("arrow_vega_lite_chart") or app.get("vega_lite_chart")


def detail_of(app):
    """The rows of the detail's table (a custom component, see sort_table), as text, None if it is not shown."""
    import pandas as pd

    tables = [e for e in app.get("bidi_component") if e.proto.component_name == "yamlboard_sort_table"]
    if not tables:
        return None
    data = json.loads(tables[0].proto.json)
    return pd.DataFrame(data["rows"], columns=[c["name"] for c in data["columns"]])


@pytest.mark.parametrize("mode", ["Pivot", "Detail"])
def test_modes(app, mode):
    app.run()
    app.segmented_control(key="sales:mode:advanced").set_value([mode]).run()
    assert not app.exception and not app.error
    assert (app.dataframe if mode == "Pivot" else detail_of(app) is not None) and not charts_of(app)


def test_views_stack_in_a_fixed_order(app):
    app.run()
    app.segmented_control(key="sales:mode:advanced").set_value(["Detail", "Chart"]).run()
    assert not app.exception and not app.error
    cards = [m.value for m in app.main.markdown if m.value.startswith("**")]
    assert cards == ["**Sum of Amount by Order date (week), split by Region**", "**Detail**"]  # 1 year: weeks
    assert charts_of(app) and detail_of(app) is not None  # the chart, then the rows under it
    app.segmented_control(key="sales:mode:advanced").set_value([]).run()
    assert app.info[0].value.startswith("Pick a view")


def test_one_row_group(app):
    app.run()
    rows = app.selectbox(key="sales:row")             # a single field, not a multiselect
    assert rows.value == "order_date"
    rows.set_value("region").run()
    assert not app.exception and not app.error
    assert 'GROUP BY "region"' in app.code[0].value.replace("\n", " ")


def test_pie_has_no_column_split(app):
    app.run()
    app.selectbox(key="sales:row").set_value("region").run()
    app.selectbox(key="sales:col").set_value("channel").run()
    assert '"channel"' in app.code[0].value
    app.selectbox(key="sales:chart").set_value(ChartType.PIE).run()
    assert not app.exception and not app.error
    assert "Split columns by" not in [s.label for s in app.selectbox]
    assert '"channel"' not in app.code[0].value       # emptied, not only hidden
    app.selectbox(key="sales:chart").set_value(ChartType.DONUT).run()
    assert not app.exception and charts_of(app)


def test_detail_pages(app):
    app.run()
    app.segmented_control(key="sales:mode:advanced").set_value(["Detail"]).run()
    app.selectbox(key="sales:page_size").set_value(5).run()
    found = next(c.value for c in app.caption if c.value.endswith("rows found"))
    total = int(found.split()[0].replace(",", ""))
    assert len(detail_of(app)) == 5
    assert app.button(key="sales:prev").disabled and not app.button(key="sales:next").disabled
    app.button(key="sales:next").click().run()
    assert not app.exception and not app.error
    assert any("Page **2** of" in m.value for m in app.markdown)
    assert "OFFSET 5" in app.code[-1].value
    app.selectbox(key="sales:page_size").set_value(200).run()   # back to the first page
    assert any(f"Page **1** of {-(-total // 200)}" in m.value for m in app.markdown)


def test_full_screen_shows_one_view_and_keeps_the_filters(app):
    app.run()
    app.segmented_control(key="sales:mode:advanced").set_value(["Chart", "Detail"]).run()
    app.button(key="sales:focus:detail").click().run()
    assert not app.exception and not app.error
    assert app.session_state["sales:focus"] == "Detail"
    assert detail_of(app) is not None and not charts_of(app)   # the chart is left out
    assert any("stSidebar" in h.proto.body for h in app.get("html"))              # the sidebar is hidden
    assert app.selectbox(key="sales:new_field") is not None                        # the filters are at hand
    app.button(key="sales:focus:detail").click().run()                            # and back
    assert app.session_state["sales:focus"] is None and charts_of(app)
    app.segmented_control(key="sales:mode:advanced").set_value(["Chart"]).run()
    app.button(key="sales:focus:aggregate").click().run()
    assert app.session_state["sales:focus"] == "Chart" and detail_of(app) is None


def test_detail_columns_can_be_picked(app):
    app.run()
    app.segmented_control(key="sales:mode:advanced").set_value(["Detail"]).run()
    picker = app.multiselect(key="sales:detail_cols")
    assert list(detail_of(app).columns) == picker.value
    picker.set_value(["amount", "region"]).run()      # shown in the order picked
    assert not app.exception and not app.error
    assert list(detail_of(app).columns) == ["amount", "region"]
    app.button(key="sales:col_down:amount").click().run()  # the arrows move a column
    assert list(detail_of(app).columns) == ["region", "amount"]
    assert app.button(key="sales:col_up:region").disabled
    picker.set_value([]).run()                        # none: the default columns, not an empty table
    assert len(detail_of(app).columns) > 2


def test_chart_names_are_plain_text(app):
    app.run()
    options = app.selectbox(key="sales:chart").options
    assert "Stacked column" in options and not any(":material/" in o for o in options)


def test_downloads_include_excel_and_the_run_id(app):
    app.run()
    labels = [b.proto.label for b in app.main.get("download_button")]
    assert labels == [":material/bookmark:", "CSV", "JSON", "Excel"]  # the saved view, then the data
    assert any(c.value.startswith("Run `") for c in app.caption)


def test_add_filter_and_switch_report(app):
    app.run()
    app.selectbox(key="report").set_value("ash-activity").run()
    assert app.title[0].value == "Database activity"
    app.selectbox(key="ash-activity:new_field").set_value("wait_class").run()
    app.multiselect(key="ash-activity:new:wait_class:values").select("CPU").run()
    app.button(key="ash-activity:add").click().run()
    assert not app.exception and not app.error
    assert app.session_state["ash-activity:filters"][0].values == ("CPU",)
    assert '"wait_class" IN (:yb_2)' in app.code[0].value  # yb_0, yb_1: the date range


def test_filters_merge_negate_and_reset(app, recwarn):
    app.run()
    app.selectbox(key="report").set_value("ash-activity").run()

    def add(field, value, exclude=False):
        app.selectbox(key="ash-activity:new_field").set_value(field).run()
        if exclude:
            app.segmented_control(key=f"ash-activity:new:{field}:not").set_value("Exclude").run()
        values = app.multiselect(key=f"ash-activity:new:{field}:values")
        assert values.value == []  # blank after the previous add
        values.select(value).run()
        app.button(key="ash-activity:add").click().run()
        assert not app.exception and not app.error

    add("wait_class", "CPU")
    add("wait_class", "Commit")  # offered although wait_class is already filtered; merged into one IN
    add("program", "batch", exclude=True)
    assert [(f.op, f.values) for f in app.session_state["ash-activity:filters"]] == [
        ("in", ("CPU", "Commit")), ("not_in", ("batch",))]
    sql = app.code[0].value
    assert '"wait_class" IN (:yb_2, :yb_3)' in sql and '("program" IS NULL OR "program" NOT IN (:yb_4))' in sql
    assert not [w for w in recwarn if "vegalite" in str(w.message)]  # the chart got rows


def test_empty_filter_on_a_timestamp(app):
    app.run()
    app.selectbox(key="report").set_value("ash-activity").run()
    app.selectbox(key="ash-activity:new_field").set_value("sample_time_utc").run()
    app.segmented_control(key="ash-activity:new:sample_time_utc:not").set_value("Exclude").run()
    app.segmented_control(key="ash-activity:new:sample_time_utc:match").set_value("Empty").run()
    app.button(key="ash-activity:add").click().run()
    assert not app.exception and not app.error
    assert app.session_state["ash-activity:filters"] == [q.Filter("sample_time_utc", "not_null")]
    assert '"sample_time_utc" IS NOT NULL' in app.code[0].value


def test_basic_mode_hides_the_pivot_and_the_sql_not_the_sort(app):
    app.session_state["advanced"] = False
    app.run()
    assert not app.exception and not app.error
    assert app.segmented_control(key="sales:mode:basic").options == ["Chart", "Detail"]
    assert not app.code                                # no SQL expander
    app.segmented_control(key="sales:mode:basic").set_value(["Detail"]).run()
    assert not app.code
    app.selectbox(key="sales:sort_add").set_value("amount").run()
    app.button(key="sales:sort_dir:amount").click().run()
    assert app.session_state["sales:order"] == [("amount", True)]
    amounts = detail_of(app)["amount"].str.replace("\u202f", "").astype(float).tolist()
    assert amounts == sorted(amounts, reverse=True) and len(amounts) > 1
    app.toggle(key="advanced").set_value(True).run()
    assert "Pivot" in app.segmented_control(key="sales:mode:advanced").options and app.code


def test_log_dir_is_written(log_dir):
    from yamlboard import logs

    logs.setup()  # once per process: the dev app's tests may have run first
    assert list(log_dir.glob("yamlboard*.log"))


def test_imported_settings_apply(app, ws):
    from yamlboard import settings

    app.run()
    doc = {"yamlboard_settings": 1, "report": "sales", "reports": {"sales": {
        "chart": "pie", "row": "region", "filters": [{"field": "region", "op": "in", "values": ["North"]}],
        "colors": {"region": {"North": "#d00000"}}, "periods": {"start": "last_month"}}}}
    keys, skipped = settings.restore(settings.dumps(doc), ws.reports)
    assert not skipped
    for k, v in keys.items():                         # what the import callback does
        app.session_state[k] = v
    app.run()
    assert not app.exception and not app.error
    assert app.selectbox(key="sales:chart").value is ChartType.PIE
    assert "Split columns by" not in [s.label for s in app.selectbox]
    assert app.selectbox(key="sales:period:start").value == "last_month"
    spec = json.loads(charts_of(app)[0].proto.spec)
    assert spec["encoding"]["color"]["scale"]["range"][0] == "#d00000"  # the pie's slices are the regions
    assert "Region is any of North" in app.caption[-1].value or any("North" in c.value for c in app.caption)


def test_pivot_keeps_the_null_group(app):
    app.session_state["sales:mode:advanced"] = ["Pivot"]
    app.session_state["sales:row"] = "region"
    app.session_state["sales:col"] = "channel"                  # channel has NULLs
    app.run()
    assert not app.exception and not app.error
    assert "(empty)" in app.dataframe[0].value.columns


def test_a_filter_on_a_field_gone_from_the_yaml_is_dropped(app):
    app.session_state["sales:filters"] = [q.Filter("removed", "in", ("x",)), q.Filter("region", "in", ("North",))]
    app.run()
    assert not app.exception and not app.error
    assert app.session_state["sales:filters"] == [q.Filter("region", "in", ("North",))]


def test_grain_follows_the_period(app):
    app.run()
    grain = lambda: app.selectbox(key="sales:grain:order_date").value.value
    assert grain() == "week"                                       # the default range: a year
    app.selectbox(key="sales:grain:order_date").set_value(Grain.QUARTER).run()
    app.selectbox(key="sales:row").set_value("region").run()
    app.selectbox(key="sales:row").set_value("order_date").run()
    assert grain() == "quarter"                                    # a grain picked holds, same period
    app.selectbox(key="sales:period:start").set_value("last_month").run()
    assert not app.exception and grain() == "day"                  # a new period: its default


def test_hide_partial_periods(app):
    app.run()
    groups = lambda: next(c.value for c in app.main.caption if " groups" in c.value)
    box = app.checkbox(key="sales:hide_partial")
    assert box.value is False and "partial" not in groups()        # off by default
    before = int(groups().split()[0].replace(",", ""))
    box.check().run()
    assert not app.exception and not app.error
    hidden = int(groups().split(" · ")[-1].split()[0])             # this week is not over: at least one
    assert hidden >= 1 and int(groups().split()[0].replace(",", "")) == before - hidden
    app.selectbox(key="sales:row").set_value("region").run()
    assert not app.exception and not [c for c in app.checkbox if c.key == "sales:hide_partial"]  # no date grain: no box


def test_periods_end_now(app):
    app.run()
    period = app.selectbox(key="sales:period:start")
    assert period.value == "last_year"                             # the longest the 366-day rule allows
    assert "last_hour" not in period.options and "Last hour" not in period.options   # date parameters
    assert not app.date_input                                      # a name, not two dates
    t = times.today()
    binds = next(c.value for c in app.caption if c.value.startswith("Binds:"))
    assert f"'end': datetime.date({t.year}, {t.month}, {t.day})" in binds         # ends today, whenever it runs
    period.set_value("custom").run()
    assert app.date_input(key="sales:range:start")                 # Custom: two dates


def test_a_database_error_is_a_message_not_a_traceback(monkeypatch, tmp_path):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    (tmp_path / "datasources.yml").write_text(f'default: "duckdb:///{tmp_path / "gone.duckdb"}?access_mode=READ_ONLY"\n')
    (tmp_path / "r.yml").write_text("id: r\ntitle: R\nsql: select 1 as a\nfields: [{name: a, type: integer}]\n")
    monkeypatch.setenv("YAMLBOARD_REPORTS", str(tmp_path))
    st.cache_resource.clear()  # the workspace of the other tests
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    st.cache_resource.clear()
    assert not at.exception
    assert "does not exist" in at.error[0].value and "Run `" in at.error[0].value


def test_a_colour_picked_applies_to_its_series(app):
    app.run()
    app.selectbox(key="sales:color_value:region").set_value("North").run()
    picker = next(c for c in app.color_picker if c.key.startswith("sales:color:region:North:"))
    picker.set_value("#d00000").run()
    assert not app.exception and not app.error
    scale = json.loads(charts_of(app)[0].proto.spec)["encoding"]["color"]["scale"]
    assert scale["range"][scale["domain"].index("North")] == "#d00000"
    app.button(key="sales:color_reset:region").click().run()
    scale = json.loads(charts_of(app)[0].proto.spec)["encoding"]["color"]["scale"]
    assert "#d00000" not in scale["range"]


def test_the_report_colours_preset_the_chart(app):
    app.session_state["report"] = "ash-activity"
    app.run()
    scale = json.loads(charts_of(app)[0].proto.spec)["encoding"]["color"]["scale"]
    assert scale["range"][scale["domain"].index("Concurrency")] == "#8b1a00"


def test_a_quick_colour_applies_in_one_click(app):
    app.run()
    app.selectbox(key="sales:color_value:region").set_value("South").run()
    pills = next(c for c in app.get("button_group") if c.key.endswith(":quick") and ":South:" in c.key)
    pills.set_value("#d00000").run()
    assert not app.exception and not app.error
    scale = json.loads(charts_of(app)[0].proto.spec)["encoding"]["color"]["scale"]
    assert scale["range"][scale["domain"].index("South")] == "#d00000"


def test_a_filter_added_from_the_chart_is_flagged_once(app):
    # What pick() leaves when a mark or a legend entry is clicked (AppTest cannot click a Vega chart).
    app.session_state["sales:filters_added"] = [q.Filter("region", "in", ("North",))]
    app.run()
    assert not app.exception and not app.error
    assert "Region is any of North" in app.toast[0].value
    css = [h.proto.body for h in app.get("html") if "NEW" in h.proto.body]
    assert css and ".st-key-sales-filters_open button" in css[0]
    app.run()  # the next run: no toast, no badge
    assert not app.toast and not [h for h in app.get("html") if "NEW" in h.proto.body]


def test_key_class_follows_the_frontend():
    from yamlboard.report_view import key_class

    assert key_class(" sales:filters_open ") == "st-key-sales-filters_open"
    assert key_class("a b/é_1-2") == "st-key-a-b--_1-2"


def test_refresh_runs_the_queries_again(app, monkeypatch):
    from yamlboard import engine

    calls = []
    real = engine.run
    monkeypatch.setattr("yamlboard.board.run", lambda *a: calls.append(1) or real(*a))
    app.run()
    before = len(calls)
    app.run()
    assert len(calls) == before  # from the cache
    app.button(key="sales:refresh").click().run()
    assert len(calls) > before and app.session_state["yb:refresh"] == 1
    assert app.selectbox(key="sales:refresh_every").options == ["Off", "30 s", "1 min", "5 min", "15 min"]


def test_auto_refresh_draws_its_timer_only_when_on(app):
    app.run()
    assert "sales:page_run" not in app.session_state
    app.selectbox(key="sales:refresh_every").set_value(60).run()
    # The page run marked itself and the timer's fragment read the mark: no refresh counted for it.
    assert not app.exception and "sales:page_run" not in app.session_state
    assert app.session_state["yb:refresh"] == 0 if "yb:refresh" in app.session_state else True


def test_several_measures_are_drawn_unsplit(app):
    app.run()
    app.multiselect(key="sales:measures").set_value(["sum_amount", "count"]).run()
    assert not app.exception and not app.error
    sql = app.code[0].value
    assert 'AS "sum_amount"' in sql and 'AS "count"' in sql and '"region"' not in sql.split("GROUP BY")[1]
    assert "Several measures: not split by Region." in [c.value for c in app.caption]
    assert charts_of(app)
    cards = [m.value for m in app.main.markdown if m.value.startswith("**")]
    assert cards[0] == "**Sum of Amount and Row count by Order date (week)**"
    app.multiselect(key="sales:measures").set_value([]).run()
    assert app.info[0].value == "Pick a measure."


def test_the_chart_format_puts_a_measure_on_the_secondary_axis(app):
    app.run()
    app.multiselect(key="sales:measures").set_value(["sum_amount", "count"]).run()
    app.selectbox(key="sales:series_chart:count").set_value(ChartType.LINE).run()
    app.segmented_control(key="sales:series_axis:count").set_value(AxisSide.SECONDARY).run()
    app.selectbox(key="sales:axis:secondary:units").set_value(Units.THOUSANDS).run()
    app.segmented_control(key="sales:empty").set_value(Empty.ZERO).run()
    assert not app.exception and not app.error
    spec = json.loads(charts_of(app)[0].proto.spec)
    assert spec["resolve"] == {"scale": {"y": "independent"}}
    assert [layer["mark"]["type"] for layer in spec["layer"]] == ["line", "line"]   # the chart is a line too



def test_trim_empty_periods_is_a_chart_format_option(app):
    app.run()
    app.checkbox(key="sales:trim_empty").check().run()   # what it trims: test_charts' fill tests
    assert not app.exception and not app.error and charts_of(app)
    assert app.session_state["sales:trim_empty"] is True


def test_an_axis_the_board_cannot_take_says_so(app):
    app.run()
    app.number_input(key="sales:axis:primary:min").set_value(10).run()
    app.number_input(key="sales:axis:primary:max").set_value(5).run()
    assert not app.exception
    assert any("Primary axis: the axis's min (10) is not below its max (5): not applied." in c.value
               for c in app.caption)


def test_the_clock_set_to_a_day_ends_the_period_and_says_so(app, monkeypatch):
    from yamlboard import times

    monkeypatch.setenv(times.TODAY_VAR, "2026-08-17")
    app.run()
    assert not app.exception
    assert "2025-08-17 to 2026-08-17, UTC: the board's clock is set to 2026-08-17" in [c.value for c in app.sidebar.caption]
    binds = next(c.value for c in app.caption if c.value.startswith("Binds:"))
    assert "'start': datetime.date(2025, 8, 17), 'end': datetime.date(2026, 8, 17)" in binds


def map_of(app):
    """The map component's data, None if it is not shown."""
    found = [e for e in app.get("bidi_component") if e.proto.component_name == "yamlboard_map"]
    return json.loads(found[0].proto.json) if found else None


def test_a_report_with_a_map_opens_on_it(app):
    app.session_state["report"] = "places"
    app.session_state["advanced"] = False
    app.run()
    assert not app.exception and not app.error
    assert app.segmented_control(key="places:mode:basic").options == ["Chart", "Map", "Detail"]
    data = map_of(app)
    assert data and data["kind"] == "sum" and sum(data["cells"]["n"]) == 20_000
    assert "**Sum of Power (kW) by place**" in [m.value for m in app.main.markdown]


def test_a_median_is_not_mapped(app):
    app.session_state["report"] = "places"
    app.run()
    app.multiselect(key="places:measures").set_value(["median_power_kw"]).run()
    assert not app.exception and map_of(app) is None
    assert any("cannot show the median" in i.value for i in app.info)
