import pandas as pd
import pytest

from yamlboard import maps
from yamlboard import query as q
from yamlboard.dialects import DIALECTS
from yamlboard.engine import engine_for, run
from yamlboard.schema import Aggregation as A
from yamlboard.schema import ReportError, parse_report

DUCK, PG = DIALECTS["duckdb"], DIALECTS["postgresql"]


@pytest.fixture
def places(ws):
    return ws.reports["places"]


def _run(ws, report, stmt):
    return run(engine_for(ws.url(report)), stmt)


MAP_YAML = """
id: m
title: M
sql: select 1.0 as lat, 2.0 as lon, 'a' as name
result_columns: [{name: lat, type: number}, {name: lon, type: number}, {name: name}]
"""


@pytest.mark.parametrize("section, problem", [
    ("map: {lat: name, lon: lon}", "map.lat: 'name' is not a number field"),
    ("map: {lat: lat, lon: nowhere}", "map.lon: 'nowhere' is not a number field"),
    ("map: {lat: lat, lon: lon, label: nowhere}", "map.label: 'nowhere' is not a field"),
    ("map: {lat: lat, lon: lon, tiles: 'ftp://x/{z}/{x}/{y}'}", "tiles: an http(s) URL"),
    ("map: {lat: lat, lon: lon, tiles: 'https://x/tile.png'}", "tiles: an http(s) URL"),
])
def test_a_map_needs_number_fields_and_a_tile_url(section, problem):
    with pytest.raises(ReportError, match=problem.replace("(", r"\(").replace(")", r"\)")):
        parse_report(MAP_YAML + section)


def test_a_report_without_map_has_no_map_sql(ws):
    with pytest.raises(q.NotAllowed, match="has no map"):
        q.map_levels(ws.reports["sales"], DUCK, {})


def test_levels_count_fewer_cells_on_coarser_grids(ws, places):
    levels = _run(ws, places, q.map_levels(places, DUCK, {})).iloc[0]
    counts = [int(levels[f"yb_{i}"]) for i in range(len(q.GRID))]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] > 19_900 and counts[-1] < 10    # nearly every site has a place of its own; 5° cells: a few
    assert q.map_grid(levels.to_dict()) == q.GRID[0]           # 20,000 places fit: no grid needed


def test_the_grid_is_the_finest_under_the_cap(monkeypatch):
    monkeypatch.setattr(q, "MAX_CELLS", 100)
    levels = {f"yb_{i}": n for i, n in enumerate([5000, 900, 300, 100, 40] + [1] * (len(q.GRID) - 5))}
    assert q.map_grid(levels) == q.GRID[3]
    assert q.map_grid({f"yb_{i}": 10**6 for i in range(len(q.GRID))}) == q.GRID[-1]


@pytest.mark.parametrize("agg", [A.SUM, A.AVG, A.MAX])
def test_cells_add_up_to_the_whole(ws, places, agg):
    m = q.Measure("power_kw", agg)
    cells = _run(ws, places, q.map_cells(places, DUCK, {}, 0.5, measure=m))
    whole = _run(ws, places, q.aggregate(places, DUCK, {}, measures=[m]))[m.alias].iloc[0]
    assert cells["yb_n"].sum() == 20_000
    value = {A.SUM: cells.get("yb_s", pd.Series(dtype=float)).sum(),
             A.AVG: cells.get("yb_s", pd.Series(dtype=float)).sum() / cells.get("yb_c", pd.Series([1])).sum(),
             A.MAX: cells.get("yb_max", pd.Series(dtype=float)).max()}[agg]
    assert value == pytest.approx(float(whole))


def test_cells_keep_the_filters_and_the_label(ws, places):
    flt = [q.Filter("city", "in", ("Lille",))]
    cells = _run(ws, places, q.map_cells(places, DUCK, {}, q.GRID[0], flt))
    assert (cells["yb_lat"] > 49).all() and cells["yb_label"].str.startswith("S").all()


@pytest.mark.parametrize("agg", [A.MEDIAN])
def test_a_map_refuses_what_clusters_cannot_combine(places, agg):
    with pytest.raises(q.NotAllowed, match="cannot show a median"):
        q.map_cells(places, DUCK, {}, 0.5, measure=q.Measure("power_kw", agg))


def test_grid_sizes_are_constants_not_binds(places):
    stmt = q.map_cells(places, PG, {}, 0.01, measure=q.Measure("power_kw", A.SUM))
    assert "floor(lat / 0.01)" in stmt.sql and not stmt.binds
    with pytest.raises(q.NotAllowed):
        q.map_cells(places, PG, {}, 0.0123)


def test_payload_carries_columns_and_an_average_colour_scale():
    cells = pd.DataFrame({"yb_lat": [45.0, 46.0], "yb_lon": [1.0, 2.0], "yb_n": [1, 3], "yb_s": [10.0, 90.0],
                          "yb_c": [1, 3], "yb_label": ["a", None]})
    spec = parse_report(MAP_YAML + "map: {lat: lat, lon: lon}").map
    data = maps.payload(cells, spec, maps.map_measure("x", A.AVG, "Average of x"), "Rows", "#2a78d6", "sig")
    assert data["cells"]["n"] == [1, 3] and data["cells"]["label"] == ["a", None]
    assert data["range"] == [10.0, 30.0] and not data["additive"]
    assert data["bounds"] == [[1.0, 45.0], [2.0, 46.0]]
    assert maps.map_measure("*", A.COUNT, "Count").kind == "count"
    assert maps.map_measure("x", A.COUNT, "Count of x").kind == "count_field"
