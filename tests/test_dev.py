"""The dev app, headless, on the DuckDB warehouse fixture."""

from pathlib import Path

import pytest

from tests.conftest import EXAMPLES

DEV = Path(__file__).parents[1] / "src" / "yamlboard" / "dev.py"


@pytest.fixture
def dev(monkeypatch, warehouse_url):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("YAMLBOARD_DEV_URL", warehouse_url)
    monkeypatch.delenv("YAMLBOARD_DEV_YAML", raising=False)
    return AppTest.from_file(str(DEV), default_timeout=60)


def button(at, label):
    return next(b for b in at.button if b.label == label)


def test_schema_page_lists_tables(dev):
    dev.run()
    assert not dev.exception and not dev.error
    assert "Connected to" in dev.sidebar.caption[0].value
    tables = dev.dataframe[0].value
    assert {"orders", "v_orders_north", "notes"} <= set(tables["table"])


def test_bad_connection_is_reported(dev, monkeypatch):
    monkeypatch.setenv("YAMLBOARD_DEV_URL", "jdbc:oracle:thin:@h:1521:db")
    dev.run()
    assert not dev.exception
    assert "unsupported JDBC" in dev.sidebar.error[0].value
    assert dev.info[0].value.startswith("Open a database or a file")


def test_query_then_draft_then_run_the_view(dev):
    dev.run()
    dev.switch_page("dev_pages/query.py").run()
    dev.text_area[0].set_value("select * from sales.orders where order_date >= :since").run()
    dev.selectbox(key="bind_type:since").set_value("date").run()
    dev.text_input(key="bind_value:since").set_value("2026-04-01").run()
    button(dev, "Run").click().run()
    assert not dev.exception and not dev.error
    assert len(dev.dataframe[0].value) == 60

    next(t for t in dev.text_input if t.label == "Title").set_value("April orders")
    button(dev, "Draft the YAML").click().run()
    assert not dev.exception and not dev.error
    dev.switch_page("dev_pages/report.py").run()       # AppTest does not follow the app's own switch_page
    assert "April orders" in dev.success[0].value

    assert not dev.exception and not dev.error         # the board's view, under the editor, at once
    assert dev.date_input(key="april-orders:param:since").value.isoformat() == "2026-04-01"  # the tested value
    assert dev.title[0].value == "April orders"
    assert dev.get("arrow_vega_lite_chart") or dev.get("vega_lite_chart")


def test_sql_error_is_shown(dev):
    dev.run()
    dev.switch_page("dev_pages/query.py").run()
    dev.text_area[0].set_value("select nope from sales.orders").run()
    button(dev, "Run").click().run()
    assert not dev.exception
    assert "nope" in dev.error[0].value


def test_loaded_yaml_is_checked(dev, monkeypatch):
    monkeypatch.setenv("YAMLBOARD_DEV_YAML", str(EXAMPLES / "sales.yml"))
    dev.run()
    dev.switch_page("dev_pages/report.py").run()
    assert "Sales" in dev.success[0].value
    assert not dev.exception and not dev.error
    assert dev.selectbox(key="sales:chart")                            # the board's controls, as `run` shows them
    dev.segmented_control(key="sales:mode:basic").set_value(["Chart", "Detail"]).run()
    assert not dev.exception and not dev.error
    assert dev.selectbox(key="sales:page_size")                        # and its detail, paged


def test_invalid_yaml_is_explained(dev):
    dev.run()
    dev.switch_page("dev_pages/report.py").run()
    dev.text_area[0].set_value("id: x\ntitle: X\nsql: select 1 as a\nfields: [{name: a, grains: [day]}]\n").run()
    assert not dev.exception
    assert "grains need a date" in dev.error[0].value


@pytest.fixture
def dev_file(monkeypatch):
    from streamlit.testing.v1 import AppTest

    def open_(path):
        monkeypatch.delenv("YAMLBOARD_DEV_URL", raising=False)
        monkeypatch.delenv("YAMLBOARD_DEV_YAML", raising=False)
        monkeypatch.setenv("YAMLBOARD_DEV_FILE", str(path))
        return AppTest.from_file(str(DEV), default_timeout=60)

    return open_


def test_csv_file_then_query(dev_file, tmp_path):
    p = tmp_path / "sales.csv"
    p.write_text("region,amount\nNorth,1.5\nSouth,2\n")
    dev = dev_file(p)
    dev.run()
    assert not dev.exception and not dev.error
    assert "Reading" in dev.sidebar.caption[-1].value
    assert dev.dataframe[1].value["region"].tolist() == ["North", "South"]
    button(dev, "Query this file").click().run()
    assert "read_csv_auto" in dev.session_state["dev_sql"]


def test_log_file_with_a_format_then_a_custom_pattern(dev_file, tmp_path):
    from tests.test_files import ACCESS

    p = tmp_path / "access.log"
    p.write_text(ACCESS)
    dev = dev_file(p)
    dev.run()
    assert not dev.exception and not dev.error
    assert "3 of 4 lines match" in dev.markdown[0].value
    assert dev.dataframe[-1].value["status"].tolist() == [200, 302, 404]

    dev.selectbox(key="log_format").set_value("custom").run()
    dev.text_input(key="log_pattern:custom").set_value(r"^(?P<client>\S+) .*\" (?P<status>\d{3}) ").run()
    assert not dev.exception and not dev.error
    assert list(dev.dataframe[-1].value.columns) == ["client", "status"]
    dev.text_input(key="log_pattern:custom").set_value(r"(?<=x)(?P<a>y)").run()  # Python accepts it, RE2 does not
    assert not dev.exception
    assert dev.error


def test_missing_file_is_reported(dev_file, tmp_path):
    dev = dev_file(tmp_path / "nope.log")
    dev.run()
    assert not dev.exception
    assert "no file matches" in dev.sidebar.error[0].value


def test_report_drafted_from_a_log_runs(dev_file, tmp_path):
    from tests.test_files import ACCESS

    p = tmp_path / "access.log"
    p.write_text(ACCESS)
    dev = dev_file(p)
    dev.run()
    button(dev, "Query this file").click().run()
    dev.switch_page("dev_pages/query.py").run()
    button(dev, "Run").click().run()
    assert not dev.exception and not dev.error
    next(t for t in dev.text_input if t.label == "Title").set_value("Access")
    button(dev, "Draft the YAML").click().run()
    dev.switch_page("dev_pages/report.py").run()
    assert "time:" in dev.session_state["yaml_text"] and "grains:" in dev.session_state["yaml_text"]
    assert not dev.exception and not dev.error
    assert dev.get("arrow_vega_lite_chart") or dev.get("vega_lite_chart")


def test_typed_path_opens_on_enter(dev, tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("region\nNorth\n")
    dev.run()
    dev.segmented_control(key="source_kind").set_value("File").run()
    dev.text_input(key="file_path").set_value(str(p)).run()
    assert not dev.exception
    assert dev.session_state["file"]["path"] == str(p)
    assert any(b.help == "Browse this machine's files" for b in dev.sidebar.button)


def test_a_tested_value_of_another_type_is_not_the_default(dev, monkeypatch):
    monkeypatch.setenv("YAMLBOARD_DEV_YAML", str(EXAMPLES / "sales.yml"))
    dev.session_state["tested_params"] = {"start": "100"}       # run as text on the Query page
    dev.run()
    dev.switch_page("dev_pages/report.py").run()
    assert not dev.exception and not dev.error
