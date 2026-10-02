import json
from pathlib import Path

import pytest

from tests.conftest import EXAMPLES
from yamlboard import dashboards, settings
from yamlboard.dashboards import Cell, DashboardError
from yamlboard.engine import Workspace
from yamlboard.schema import ChartType, Grain

APP = Path(__file__).parents[1] / "src" / "yamlboard" / "app.py"
DASHBOARDS = EXAMPLES / "dashboards"


@pytest.fixture(scope="module")
def reports():
    return Workspace.open(EXAMPLES).reports


def doc(**over) -> str:
    return json.dumps({"yamlboard_dashboard": 1, "name": "D", "cells": [{"report": "sales"}], **over})


def test_the_example_dir_holds_one_dashboard_its_view_files_are_not_dashboards():
    found, errors = dashboards.load_dir(DASHBOARDS)
    assert errors == {}
    assert [d.label for d in found] == ["Overview"]
    first, second, third = found[0].cells
    assert first.report == "sales" and first.view["chart"] == "column-stacked"   # read from views/
    assert second.title == "Orders by product" and third.width == 2


def test_a_missing_directory_has_no_dashboard(tmp_path):
    assert dashboards.load_dir(tmp_path / "none") == ([], {})


def test_sub_directories_are_folders_and_a_bad_file_an_error(tmp_path):
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "a.json").write_text(doc(name="Costs"))
    (tmp_path / "b.json").write_text("{not json")
    found, errors = dashboards.load_dir(tmp_path)
    assert [d.label for d in found] == ["team - Costs"]
    assert list(errors) == [str(tmp_path / "b.json")]


@pytest.mark.parametrize("text, message", [
    ("[]", "not a yamlboard dashboard"),
    (json.dumps({"yamlboard_dashboard": 2, "cells": [{}]}), "not a yamlboard dashboard"),
    (doc(cells=[]), "no cells"),
    (doc(grid={"columns": 0}), "grid columns"),
    (doc(grid={"columns": True}), "grid columns"),
    (doc(grid={"height": 50}), "grid height"),
    (doc(grid=[2]), "not a mapping"),
])
def test_what_is_not_a_dashboard(text, message):
    with pytest.raises(DashboardError, match=message):
        dashboards.load(text)


def test_a_bad_cell_is_an_error_in_its_cell_only(tmp_path):
    (tmp_path / "secret.json").write_text("{}")
    d = dashboards.load(doc(grid={"columns": 2}, cells=[
        {"report": "sales", "width": 9}, "x", {"view": "../secret.json"}, {"view": "missing.json"},
        {"view": 3}, {"title": "no report"}]), base=tmp_path / "sub")
    sales, x, outside, missing, bad, none = d.cells
    assert sales.error is None and sales.width == 2                    # clamped to the grid
    assert "not a cell" in x.error
    assert "outside" in outside.error
    assert "missing.json" in missing.error
    assert "neither a file nor a view" in bad.error
    assert none.error == "no report"


def test_a_dashboard_opened_from_a_disk_reads_no_view_file():
    cell = dashboards.load(doc(cells=[{"view": "views/sales-by-region.json"}])).cells[0]
    assert "written in" in cell.error


def test_cells_fill_the_grid_row_by_row():
    a, b, c, d = Cell("a"), Cell("b"), Cell("c", width=2), Cell("d")
    assert dashboards.grid_rows([a, b, c, d], 2) == [[a, b], [c], [d]]
    assert dashboards.grid_rows([a, c, b], 3) == [[a, c], [b]]


def test_the_plan_takes_the_view_over_the_report(reports):
    cell = dashboards.load_dir(DASHBOARDS)[0][0].cells[0]
    p = dashboards.plan(cell, reports["sales"])
    assert (p.kind, p.row, p.column, p.measure.alias) == (ChartType.COLUMN_STACKED, "order_date", "region",
                                                          "sum_amount")
    assert p.grains == {"order_date": Grain.MONTH} and p.colors == {"region": {"North": "#d00000"}}
    assert p.period == "Last year" and p.skipped == []
    assert p.keys["sales:chart"] is ChartType.COLUMN_STACKED                  # to open it on the Reports page


def test_no_view_is_the_report_own_and_a_pie_has_no_split(reports):
    r = reports["sales"]
    p = dashboards.plan(Cell("sales"), r)
    assert (p.kind, p.row, p.column) == (r.view.chart, r.view.rows[0], r.view.columns[0])
    pie = dashboards.plan(Cell("sales", view={"chart": "pie", "row": "region"}), r)
    assert pie.column is None


def test_what_the_report_no_longer_allows_is_skipped(reports):
    p = dashboards.plan(Cell("sales", view={"row": "gone", "measure": "nope", "periods": {"start": "custom"}}),
                        reports["sales"])
    assert len(p.skipped) == 2 and p.row == reports["sales"].view.rows[0]
    assert p.period  # custom dates are not saved: the period the report opens on


def test_the_grain_follows_the_period_when_the_view_sets_none(reports):
    r = reports["sales"]
    p = dashboards.plan(Cell("sales", view={"row": "order_date", "periods": {"start": "last_7_days"}}), r)
    assert p.grains == {"order_date": None}
    from yamlboard import query as q
    params = q.coerce_parameters(r, p.raw_params)
    assert dashboards.with_grains(p, r, params).grains == {"order_date": Grain.DAY}


def test_a_dashboard_made_from_saved_views():
    view = (DASHBOARDS / "views" / "sales-by-region.json").read_bytes()
    made, skipped = dashboards.skeleton("Mine", [("a.json", view), ("b.json", b"{}"), ("c.json", b"no")], 3)
    assert made["grid"]["columns"] == 3 and [c["report"] for c in made["cells"]] == ["sales"]
    assert [s.split(":")[0] for s in skipped] == ["b.json", "c.json"]
    d = dashboards.load(settings.dumps(made))                                  # it opens as it is
    assert d.name == "Mine" and d.cells[0].view["row"] == "order_date"


# --- the page ------------------------------------------------------------------

@pytest.fixture
def board(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("YAMLBOARD_REPORTS", str(EXAMPLES))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.switch_page("board_pages/dashboard.py")
    return at


def charts_of(app):
    return app.get("arrow_vega_lite_chart") or app.get("vega_lite_chart")


def test_the_dashboard_page_draws_every_cell(board):
    board.run()
    assert not board.exception and not board.error and not board.warning
    assert board.title[0].value == "Overview"
    specs = [json.loads(c.proto.spec) for c in charts_of(board)]
    assert len(specs) == 3
    scale = specs[0]["encoding"]["color"]["scale"]
    assert scale["range"][scale["domain"].index("North")] == "#d00000"        # the view's colour
    assert all(s["height"] == 300 for s in specs)


def test_a_cell_opens_its_view_on_the_reports_page(board):
    board.run()
    board.button(key="dashboard:1:open").click().run()
    assert board.session_state["report"] == "sales"
    assert board.session_state["sales:chart"] is ChartType.DONUT


def test_no_dashboard_says_how_to_make_one(board, monkeypatch, tmp_path):
    (tmp_path / "datasources.yml").write_text((EXAMPLES / "datasources.yml").read_text())
    monkeypatch.setenv("YAMLBOARD_REPORTS", str(tmp_path))
    board.run()
    assert not board.exception
    assert board.info[0].value.startswith("No dashboard")
