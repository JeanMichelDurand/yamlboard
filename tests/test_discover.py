from datetime import date

import pandas as pd
import pytest

from yamlboard import discover
from yamlboard import query as q
from yamlboard.engine import columns_for, dialect_of, run
from yamlboard.schema import FieldType, parse_report


@pytest.mark.parametrize("conn, url", [
    ("duckdb:///data/w.duckdb", "duckdb:///data/w.duckdb"),
    ("postgresql+psycopg://u:${PW}@h/db", "postgresql+psycopg://u:${PW}@h/db"),
    ("jdbc:duckdb:/data/w.duckdb", "duckdb:////data/w.duckdb?access_mode=read_only"),
    ("jdbc:duckdb:", "duckdb:///:memory:"),
    ("data/w.duckdb", "duckdb:///data/w.duckdb?access_mode=read_only"),
    (":memory:", "duckdb:///:memory:"),
    ("jdbc:postgresql://db:5432/wh", "postgresql+psycopg://db:5432/wh"),
    ("jdbc:postgresql://db/wh?user=rep&password=${PGPASSWORD}&sslmode=require",
     "postgresql+psycopg://rep:${PGPASSWORD}@db/wh?sslmode=require"),
    ("jdbc:postgresql://db/wh?user=rep&password=p@ss:w/rd", "postgresql+psycopg://rep:p%40ss%3Aw%2Frd@db/wh"),
])
def test_to_url(conn, url):
    assert discover.to_url(conn) == url


@pytest.mark.parametrize("conn, error", [
    ("", "empty"), ("jdbc:oracle:thin:@h:1521:db", "unsupported JDBC"), ("warehouse", "not a connection"),
])
def test_to_url_rejects(conn, error):
    with pytest.raises(ValueError, match=error):
        discover.to_url(conn)


def test_tables(warehouse):
    df = discover.tables(warehouse)
    rows = set(df.itertuples(index=False, name=None))
    assert {("sales", "orders", "table"), ("sales", "customers", "table"), ("sales", "v_orders_north", "view"),
            ("main", "notes", "table")} <= rows
    assert not df["schema"].isin(discover.SYSTEM_SCHEMAS).any()


def test_columns(warehouse):
    df = discover.columns(warehouse, "sales", "orders")
    assert df["column"].tolist() == ["order_id", "customer_id", "order_date", "CreatedAt", "region", "channel",
                                     "amount", "paid"]
    assert df.set_index("column").loc["order_date", "type"] == "DATE"
    assert discover.columns(warehouse, "sales", "nope").empty


def test_select_all_quotes(warehouse):
    sql = discover.select_all(warehouse, "sales", "v_orders_north")
    assert sql == 'SELECT *\nFROM "sales"."v_orders_north"'
    assert len(discover.preview(warehouse, sql)) == 60


def test_preview_limit_binds_and_trailing_comment(warehouse):
    sql = "select * from sales.orders where order_date >= :since -- since a date\n;"
    df = discover.preview(warehouse, sql, {"since": date(2026, 4, 30), "unused": 1}, limit=5)
    assert len(df) == 2
    assert len(discover.preview(warehouse, "select * from sales.orders", limit=7)) == 7


def test_preview_rejects(warehouse):
    with pytest.raises(ValueError, match="no value for :since"):
        discover.preview(warehouse, "select * from sales.orders where order_date >= :since")
    with pytest.raises(ValueError, match="limit"):
        discover.preview(warehouse, "select 1", limit=0)


def test_infer_type(warehouse):
    df = discover.preview(warehouse, "select * from sales.orders")
    types = {c: discover.infer_type(df[c]) for c in df.columns}
    assert types == {
        "order_id": FieldType.INTEGER, "customer_id": FieldType.INTEGER, "order_date": FieldType.DATE,
        "CreatedAt": FieldType.TIMESTAMP, "region": FieldType.STRING, "channel": FieldType.STRING,
        "amount": FieldType.NUMBER, "paid": FieldType.BOOLEAN,
    }
    assert discover.infer_type(pd.Series([None, None], dtype=object)) is FieldType.STRING


def test_draft_field_permissions():
    assert discover.draft_field("customer_id", FieldType.INTEGER)["measures"] == ["count_distinct"]
    assert discover.draft_field("valid", FieldType.INTEGER)["measures"] == discover.NUMBER_MEASURES
    assert discover.draft_field("region", FieldType.STRING)["groupable"] is True
    assert discover.draft_field("CreatedAt", FieldType.TIMESTAMP)["label"] == "Created at"


def test_drafted_report_loads_and_runs(warehouse):
    sql = "select * from sales.orders where order_date >= :since"
    since = date(2026, 3, 1)
    result = discover.preview(warehouse, sql, {"since": since}, limit=1000)  # every row, to compare sums
    draft = discover.draft_report(sql, result, "Orders since", {"since": FieldType.DATE})
    text = discover.to_yaml(draft)
    assert "sql: |\n" in text and "grains: [day, week, month, quarter, year]" in text

    report = parse_report(text)
    assert report.id == "orders-since"
    assert [(p.name, p.type, p.required) for p in report.parameters] == [("since", FieldType.DATE, True)]
    assert report.view.rows == ("order_date",) and report.view.columns == ("region",)
    assert report.view.chart.value == "line"

    params = q.coerce_parameters(report, {"since": since.isoformat()})
    cmap = columns_for(warehouse, report, params)
    df = run(warehouse, q.default_view(report, dialect_of(warehouse), params, columns_map=cmap))
    assert len(df) == 2 * 4                            # March and April, by 4 regions
    assert df["sum_amount"].astype(float).sum() == pytest.approx(result["amount"].astype(float).sum())


def test_draft_without_dates_or_numbers(warehouse):
    sql = "select region, channel from sales.orders"
    report = parse_report(discover.to_yaml(discover.draft_report(sql, discover.preview(warehouse, sql), "Regions")))
    assert report.view.rows == ("region",) and report.view.measures[0].field == "*"
    assert report.parameters == ()


def test_draft_from_an_empty_result(warehouse):
    sql = "select * from main.notes"
    report = parse_report(discover.to_yaml(discover.draft_report(sql, discover.preview(warehouse, sql), "Notes")))
    assert {f.type for f in report.fields} == {FieldType.STRING}


def test_with_view_rewrites_the_view_section_only():
    text = ("# header comment\nid: r\nsql: |\n  select 1 as a\n  -- view: not a key\nview:   # old\n  rows: [a]\n"
            "\n  # inside\n  chart: pie\n\n# about the readme\nreadme: hi\n")
    out = discover.with_view(text, {"rows": ["a"], "chart": "bar", "filters": [{"field": "a", "values": ["x"]}]})
    assert out == ("# header comment\nid: r\nsql: |\n  select 1 as a\n  -- view: not a key\n"
                   "view:\n  rows: [a]\n  chart: bar\n  filters:\n  - field: a\n    values: [x]\n"
                   "\n# about the readme\nreadme: hi\n")
    assert discover.with_view("id: r\nview: {rows: [a]}\n", {"chart": "bar"}) == "id: r\nview:\n  chart: bar\n"
    assert discover.with_view("id: r", {"chart": "bar"}) == "id: r\nview:\n  chart: bar\n"   # none: at the end


def test_with_fields_writes_the_permissions_in_canonical_keys():
    text = ("id: r\ntitle: R\nsql: select a, n, d from t\nresult_columns:   # the columns\n"
            "  - {mapping: a, label: A, filtrage: true, groupable: true, colors: {x: '#000000'}}\n"
            "  - name: n\n    type: integer\n    measures: [sum]\n  - {name: d, type: date, exclude: true}\n"
            "view:\n  rows: [a]\n")
    edits = {"a": {"type": "string", "visible": False, "filterable": False, "groupable_rows": True,
                   "groupable_columns": False, "grains": [], "measures": ["count"]},
             "n": {"type": "number", "visible": True, "filterable": True, "groupable_rows": True,
                   "groupable_columns": True, "grains": [], "measures": ["sum", "avg"]}}
    out = discover.with_fields(text, edits)
    assert out.split("result_columns:")[0] == "id: r\ntitle: R\nsql: select a, n, d from t\n"
    assert out.endswith("view:\n  rows: [a]\n")
    r = parse_report(out)
    a, n = r.field_map["a"], r.field_map["n"]
    assert (a.label, a.visible, a.filterable, a.groupable_rows, a.groupable_columns, a.colors) == (
        "A", False, False, True, False, {"x": "#000000"})
    assert [m.value for m in a.measures] == ["count"]
    assert (n.type, n.filterable, n.groupable_columns, [m.value for m in n.measures]) == (
        FieldType.NUMBER, True, True, ["sum", "avg"])
    assert "d" not in r.field_map and r.fields[2].exclude                 # an excluded field as it was
    assert "filtrage" not in out and "mapping: a" in out and "groupable: true" in out


def test_with_section_takes_the_unindented_list_items_with_it():
    text = discover.to_yaml({"id": "r", "result_columns": [{"name": "a"}, {"name": "b"}], "view": {"rows": ["a"]}})
    assert "\n- name: a\n" in text                                         # to_yaml does not indent a list
    out = discover.with_section(text, "result_columns", [{"name": "a", "filterable": True}, {"name": "b"}])
    assert out == "id: r\nresult_columns:\n- name: a\n  filterable: true\n- name: b\nview:\n  rows: [a]\n"
    assert discover.with_section("a: 1\nview: x\n---\nb: 2\n", "view", "y") == "a: 1\nview: y\n---\nb: 2\n"


def test_a_draft_places_rows_named_as_coordinates_and_never_sums_them():
    result = pd.DataFrame({"site_lat": [48.8], "lng": [2.3], "name": ["A"], "power": [5.0]})
    draft = discover.draft_report("select 1", result, "Sites")
    assert draft["map"] == {"lat": "site_lat", "lon": "lng", "label": "name"}
    assert draft["view"]["measures"] == [{"field": "power", "aggregation": "sum"}]
    assert parse_report(discover.to_yaml(draft)).map.lat == "site_lat"
    assert "map" not in discover.draft_report("select 1", result[["site_lat", "name"]], "Half")
    assert discover.draft_map([{"name": "lat", "type": "string"}, {"name": "lon", "type": "number"}]) is None


def test_with_section_none_takes_the_section_out():
    text = "a: 1\nmap:\n  lat: x\n  lon: y\n# about b\nb: 2\n"
    assert discover.with_section(text, "map", None) == "a: 1\n# about b\nb: 2\n"
    assert discover.with_section("a: 1\n", "map", None) == "a: 1\n"
