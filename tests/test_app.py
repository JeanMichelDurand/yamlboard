import json
from pathlib import Path

import pytest

from tests.conftest import EXAMPLES
from yamlboard import query as q
from yamlboard import times
from yamlboard.schema import ChartType, Grain

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


@pytest.mark.parametrize("mode", ["Pivot", "Detail"])
def test_modes(app, mode):
    app.run()
    app.segmented_control(key="sales:mode:advanced").set_value([mode]).run()
    assert not app.exception and not app.error
    assert app.dataframe and not charts_of(app)


def test_views_stack_in_a_fixed_order(app):
    app.run()
    app.segmented_control(key="sales:mode:advanced").set_value(["Detail", "Chart"]).run()
    assert not app.exception and not app.error
    cards = [m.value for m in app.main.markdown if m.value.startswith("**")]
    assert cards == ["**Sum of Amount by Order date (week), split by Region**", "**Detail**"]  # 1 year: weeks
    assert charts_of(app) and app.dataframe        # the chart, then the rows under it
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
    assert len(app.dataframe[0].value) == 5
    assert app.button(key="sales:prev").disabled and not app.button(key="sales:next").disabled
    app.button(key="sales:next").click().run()
    assert not app.exception and not app.error
    assert any("Page **2** of" in m.value for m in app.markdown)
    assert "OFFSET 5" in app.code[-1].value
    app.selectbox(key="sales:page_size").set_value(200).run()   # back to the first page
    assert any(f"Page **1** of {-(-total // 200)}" in m.value for m in app.markdown)


def test_detail_columns_can_be_picked(app):
    app.run()
    app.segmented_control(key="sales:mode:advanced").set_value(["Detail"]).run()
    picker = app.multiselect(key="sales:detail_cols")
    assert list(app.dataframe[0].value.columns) == picker.value
    picker.set_value(["amount", "region"]).run()      # shown in the order picked
    assert not app.exception and not app.error
    assert list(app.dataframe[0].value.columns) == ["amount", "region"]
    app.button(key="sales:col_down:amount").click().run()  # the arrows move a column
    assert list(app.dataframe[0].value.columns) == ["region", "amount"]
    assert app.button(key="sales:col_up:region").disabled
    picker.set_value([]).run()                        # none: the default columns, not an empty table
    assert len(app.dataframe[0].value.columns) > 2


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
    app.selectbox(key="sales:sort").set_value("amount").run()
    app.toggle(key="sales:desc").set_value(True).run()
    amounts = list(app.dataframe[0].value["amount"])
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
