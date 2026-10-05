import pytest

from tests.conftest import EXAMPLES, FIXTURES
from yamlboard import query as q
from yamlboard.schema import (
    Aggregation,
    ChartType,
    FieldType,
    Grain,
    Report,
    ReportError,
    Rule,
    load_report,
    load_reports,
    sql_binds,
)


def test_legacy_keys_load_unchanged():
    r = load_report(FIXTURES / "legacy_report.yml")
    assert r.id == "LEGACY-01_Activite-Sessions" and r.active and r.rights == ("dba",)
    ts = r.field_map["sample_time_utc"]
    assert ts.type is FieldType.TIMESTAMP and ts.filterable and ts.groupable_rows and ts.groupable_columns
    assert ts.grains == (Grain.MINUTE, Grain.HOUR, Grain.DAY)
    text = r.field_map["sql_text"]
    assert not (text.filterable or text.groupable_rows or text.groupable_columns) and text.visible
    assert [p.name for p in r.parameters] == ["datedeb", "datefin"] and r.parameters[0].required
    assert r.ranges[0].start == "datedeb" and r.ranges[0].end == "datefin"
    assert r.rules[0].rule is Rule.MAX_DAYS and r.rules[0].value == 1
    assert r.view.rows == ("sample_time_utc",) and r.view.columns == ("wait_class",)
    assert r.view.measures[0].field == "*" and r.view.measures[0].aggregation is Aggregation.COUNT
    assert r.view.chart is ChartType.COLUMN_STACKED


def test_examples_load(ws):
    assert set(ws.reports) == {"ash-activity", "sales", "pg-activity", "app-log", "duckdb-catalog"} and not ws.errors


def test_groupable_shortcut_and_measures():
    sales = load_report(EXAMPLES / "sales.yml")
    region = sales.field_map["region"]
    assert region.groupable_rows and region.groupable_columns
    assert sales.field_map["amount"].measures == (Aggregation.SUM, Aggregation.AVG, Aggregation.MEDIAN, Aggregation.MAX)
    assert sales.default_grain("order_date") is Grain.MONTH


def _report(**over):
    base = {
        "id": "r", "title": "R", "sql": "select a, n from t where a = :p",
        "parameters": [{"name": "p"}],
        "fields": [{"name": "a", "groupable": True}, {"name": "n", "type": "integer", "measures": ["sum"]}],
    }
    return Report.model_validate(base | over)


@pytest.mark.parametrize(
    "over, message",
    [
        ({"sql": "select a from t where a = :p and b = :q"}, "undeclared parameters: q"),
        ({"sql": "select a from t"}, "never used in sql: p"),
        ({"fields": [{"name": "a"}, {"name": "a"}]}, "duplicate field names: a"),
        ({"view": {"rows": ["n"]}}, "'n' is not a field with groupable_rows"),
        ({"view": {"measures": [{"field": "n", "aggregation": "avg"}]}}, "avg of 'n' is not allowed"),
        ({"view": {"measures": [{"field": "*", "aggregation": "sum"}]}}, "'*' only supports count"),
        ({"fields": [{"name": "a", "grains": ["day"]}]}, "grains need a date or timestamp"),
        ({"fields": [{"name": "a", "measures": ["sum"]}]}, "sum need a numeric field"),
        ({"rules": [{"param": "x", "rule": "max", "value": 1}]}, "unknown parameter 'x'"),
        ({"parameters": [{"name": "p", "type": "date"}], "rules": [{"param": "p", "rule": "min", "value": 3}]},
         "3 is not an ISO-8601 date"),
        ({"parameters": [{"name": "p", "type": "integer"}], "rules": [{"param": "p", "rule": "max", "value": "9"}]},
         "'9' is not a number"),
        ({"rules": [{"param": "p", "rule": "min", "value": 3}]}, "needs a number, date or timestamp parameter"),
        ({"rules": [{"param": "p", "rule": "max_days", "value": 3}]}, "needs date or timestamp parameters"),
        ({"sql": "select a, n from t where a between :p and :q",
          "parameters": [{"name": "p", "type": "date"}, {"name": "q", "type": "timestamp"}],
          "rules": [{"param": "p", "other": "q", "rule": "max_days", "value": 3}]}, "both ends must be the same type"),
        ({"rules": [{"param": "p", "rule": "pattern", "value": "("}]}, "invalid pattern"),
        ({"rules": [{"param": "p", "rule": "min_length", "value": "x"}]}, "'x' is not a length"),
    ],
)
def test_invalid_reports(over, message):
    with pytest.raises(ValueError, match=message):
        _report(**over)


def test_binds_ignore_casts_literals_and_comments():
    sql = "select x::int, '12:30 :no' as t -- :nope\n/* :none */ from t where a = :yes and b=:also"
    assert sql_binds(sql) == {"yes", "also"}
    assert sql_binds("where d >= :since::date") == set()   # SQLAlchemy does not bind it either


def test_load_reports_collects_errors(tmp_path):
    (tmp_path / "ok.yml").write_text((EXAMPLES / "sales.yml").read_text())
    (tmp_path / "dup.yaml").write_text((EXAMPLES / "sales.yml").read_text())
    (tmp_path / "bad.yml").write_text("id: x\n")
    (tmp_path / "datasources.yml").write_text('default: "duckdb:///:memory:"\n')
    reports, errors = load_reports(tmp_path)
    assert list(reports) == ["sales"]
    assert len(errors) == 2 and any("duplicate report id" in e for e in errors.values())


def test_not_a_mapping(tmp_path):
    (tmp_path / "x.yml").write_text("- a\n")
    with pytest.raises(ReportError, match="expected a mapping"):
        load_report(tmp_path / "x.yml")


def test_rule_bounds_take_the_parameter_type():
    r = _report(sql="select a, n from t where d >= :p", parameters=[{"name": "p", "type": "timestamp"}],
                rules=[{"param": "p", "rule": "min", "value": "2026-01-01"}])
    assert q.coerce_parameters(r, {"p": "2026-02-01"})
    with pytest.raises(q.InvalidParameters, match="at least 2026-01-01"):
        q.coerce_parameters(r, {"p": "2025-12-31"})


@pytest.mark.parametrize("extra", [
    "fields: [{name: a, type: date, grains: 5}]",
    "fields: [{name: a, measures: 5}]",
    "fields: [{name: a}]\nview: {grains: [a]}",
    "fields: [{name: a}]\noptions: [x]",
    "fields: [{name: a}]\nslice: 3",
    "fields: [{name: a}]\ncustomParametersFormType: [x]",
])
def test_a_malformed_report_fails_alone(tmp_path, extra):
    (tmp_path / "ok.yml").write_text((EXAMPLES / "sales.yml").read_text())
    (tmp_path / "bad.yml").write_text(f"id: bad\ntitle: B\nsql: select 1 as a\n{extra}\n")
    reports, errors = load_reports(tmp_path)
    assert list(reports) == ["sales"] and list(errors) == [str(tmp_path / "bad.yml")]


def test_a_decimal_bound_on_an_integer_is_kept():
    r = _report(parameters=[{"name": "p", "type": "integer"}], rules=[{"param": "p", "rule": "min", "value": 2.5}])
    with pytest.raises(q.InvalidParameters, match="at least 2.5"):
        q.coerce_parameters(r, {"p": 2})


@pytest.mark.parametrize("ftype, expected", [
    ("timestamp", tuple(Grain)),
    ("datetime", tuple(Grain)),                                                  # a type alias
    ("date", (Grain.DAY, Grain.WEEK, Grain.MONTH, Grain.QUARTER, Grain.YEAR)),   # no minute or hour in a day
])
def test_all_grains(ftype, expected):
    r = Report.model_validate({"id": "x", "title": "X", "sql": "select 1 as d",
                               "fields": [{"name": "d", "type": ftype, "grains": "all"}]})
    assert r.field_map["d"].grains == expected


def test_all_grains_need_a_date():
    with pytest.raises(ValueError, match="grains need a date"):
        Report.model_validate({"id": "x", "title": "X", "sql": "select 1 as d", "fields": [{"name": "d", "grains": "all"}]})


def test_sub_folders_are_read_and_named(tmp_path):
    (tmp_path / "audit" / "deep").mkdir(parents=True)
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "sales.yml").write_text((EXAMPLES / "sales.yml").read_text())
    (tmp_path / "audit" / "ash.yml").write_text((EXAMPLES / "ash_activity.yml").read_text())
    (tmp_path / "audit" / "deep" / "x.yml").write_text(
        (EXAMPLES / "sales.yml").read_text().replace("id: sales", "id: deep-sales"))
    (tmp_path / ".hidden" / "y.yml").write_text("id: y\n")
    (tmp_path / "datasources.yml").write_text('default: "duckdb:///:memory:"\n')
    reports, errors = load_reports(tmp_path)
    assert not errors
    assert {r.id: r.name for r in reports.values()} == {
        "sales": "Sales", "ash-activity": "audit - Database activity", "deep-sales": "audit/deep - Sales"}


def test_result_columns_is_the_new_name_of_fields():
    base = _report().model_dump(mode="json", by_alias=False)
    renamed = {k: v for k, v in base.items() if k != "fields"} | {"result_columns": base["fields"]}
    assert Report.model_validate(renamed).fields == _report().fields


def test_colours_are_checked_and_keyed_by_the_value_as_text():
    r = _report(fields=[{"name": "a", "groupable": True, "colors": {"x": "#ABC", True: "#00cc00", None: "#123456"}},
                        {"name": "n", "type": "integer", "measures": ["sum"]}], view={"color": "#174D98"})
    assert r.field_map["a"].colors == {"x": "#aabbcc", "True": "#00cc00", "(empty)": "#123456"}
    assert r.view.color == "#174d98"
    with pytest.raises(ValueError, match="not a #rrggbb colour"):
        _report(view={"color": "red"})
