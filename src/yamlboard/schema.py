"""Report definitions: the YAML schema.

Canonical keys are English. The legacy French keys (``mapping``, ``filtrage``,
``agregationLigne``, ``customParameters``, ``slice``, ...) are accepted as aliases,
so existing report files load unchanged.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from pydantic import Field as F

from yamlboard import times


class ReportError(ValueError):
    """A report file that cannot be loaded."""


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"

    @property
    def temporal(self) -> bool:
        return self in (FieldType.DATE, FieldType.TIMESTAMP)

    @property
    def numeric(self) -> bool:
        return self in (FieldType.INTEGER, FieldType.NUMBER)


class Grain(str, Enum):
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class Aggregation(str, Enum):
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    MEDIAN = "median"


# count and count_distinct work on any field; every other aggregation needs a number.
NUMERIC_ONLY = frozenset({Aggregation.SUM, Aggregation.AVG, Aggregation.MIN, Aggregation.MAX, Aggregation.MEDIAN})


class ChartType(str, Enum):
    COLUMN = "column"
    COLUMN_STACKED = "column-stacked"
    BAR = "bar"
    BAR_STACKED = "bar-stacked"
    LINE = "line"
    AREA = "area"
    PIE = "pie"
    DONUT = "donut"

    @property
    def one_dimension(self) -> bool:
        """Draws one dimension only: its slices are the row groups, never split by a column field."""
        return self in (ChartType.PIE, ChartType.DONUT)


class Rule(str, Enum):
    MIN = "min"
    MAX = "max"
    MIN_LENGTH = "min_length"
    MAX_LENGTH = "max_length"
    PATTERN = "pattern"
    MAX_DAYS = "max_days"
    MAX_HOURS = "max_hours"


_TYPE_ALIASES = {
    "mnemo": "string", "uuid": "string", "varchar": "string", "text": "string",
    "int": "integer", "float": "number", "double": "number", "decimal": "number",
    "datetime": "timestamp", "bool": "boolean",
}
_GRAIN_ALIASES = {
    "minutes": "minute", "hours": "hour", "heures": "hour", "days": "day", "jour": "day",
    "weeks": "week", "semaine": "week", "months": "month", "mois": "month",
    "quarters": "quarter", "trimestre": "quarter", "years": "year", "annee": "year", "année": "year",
}
_AGG_ALIASES = {"distinctcount": "count_distinct", "average": "avg", "mean": "avg"}
_CHART_ALIASES = {
    "stacked_column": "column-stacked", "stacked-column": "column-stacked",
    "bar_h": "bar", "stacked_bar": "bar-stacked", "stacked-bar": "bar-stacked",
}
_RULE_ALIASES = {"day_between": "max_days", "hour_between": "max_hours"}


def _normalise(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, str):
        key = value.strip().lower()
        return aliases.get(key, key)
    return value


_HEX = re.compile(r"#[0-9a-f]{6}")
EMPTY = "(empty)"  # how a NULL value shows in a legend, a pivot header, a filter: and the key of its colour


def color_hex(value: Any) -> str:
    """``#rrggbb`` in lower case from ``#rrggbb`` or ``#rgb``; a ValueError otherwise."""
    text = str(value).strip().lower()
    if re.fullmatch(r"#[0-9a-f]{3}", text):
        text = "#" + "".join(c * 2 for c in text[1:])
    if not _HEX.fullmatch(text):
        raise ValueError(f"{value!r} is not a #rrggbb colour")
    return text


def value_key(value: Any) -> str:
    """A value as text: the key of its colour (a YAML key ``true`` and a result's True are both "True")."""
    return EMPTY if value is None else str(value)


def _colors(v: Any) -> dict[str, str]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError(f"colors: expected 'value: \"#rrggbb\"' pairs, got {type(v).__name__}")  # noqa: TRY004
    return {value_key(k): color_hex(c) for k, c in v.items()}


def _names(items: Any) -> Any:
    """``[x, {field: y}, {uniqueName: z}]`` -> ``[x, y, z]``."""
    if not isinstance(items, list):
        return items
    return [i.get("field", i.get("uniqueName")) if isinstance(i, dict) else i for i in items]


def _mapping(value: Any, key: str) -> dict:
    """A legacy section that must be a mapping; empty when absent."""
    if value is None:
        return {}
    if not isinstance(value, dict):  # a ValueError: pydantic reports it, a TypeError would escape
        raise ValueError(f"{key}: expected a mapping, got {type(value).__name__}")  # noqa: TRY004
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class FieldDef(_Model):
    """A column of the report's SQL result, and what users may do with it."""

    name: str = F(validation_alias=AliasChoices("name", "mapping"))
    type: FieldType = FieldType.STRING
    label: str | None = None
    visible: bool = F(True, validation_alias=AliasChoices("visible", "affichageDefaut"))
    filterable: bool = F(False, validation_alias=AliasChoices("filterable", "filtrage"))
    groupable_rows: bool = F(False, validation_alias=AliasChoices("groupable_rows", "agregationLigne"))
    groupable_columns: bool = F(
        False, validation_alias=AliasChoices("groupable_columns", "agregationColone", "agregationColonne")
    )
    grains: tuple[Grain, ...] = F((), validation_alias=AliasChoices("grains", "availableGroupingDateType"))
    measures: tuple[Aggregation, ...] = ()
    exclude: bool = False
    colors: dict[str, str] = {}  # a value's colour in a chart, by the value as text: {"CPU": "#00cc00"}

    @model_validator(mode="before")
    @classmethod
    def _groupable_shortcut(cls, data: Any) -> Any:
        if isinstance(data, dict) and "groupable" in data:
            data = dict(data)
            flag = data.pop("groupable")
            data.setdefault("groupable_rows", flag)
            data.setdefault("groupable_columns", flag)
        return data

    @model_validator(mode="before")
    @classmethod
    def _all_grains(cls, data: Any) -> Any:
        """``grains: all``: minute to year on a timestamp, day to year on a date."""
        if not isinstance(data, dict):
            return data
        key = next((k for k in ("grains", "availableGroupingDateType") if k in data), None)
        if key is None or _normalise(data[key], {}) != "all":
            return data
        ftype = _normalise(data.get("type", "string"), _TYPE_ALIASES)
        return {**data, key: [g.value for g in Grain if ftype != "date" or g not in (Grain.MINUTE, Grain.HOUR)]}

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v: Any) -> Any:
        return _normalise(v, _TYPE_ALIASES)

    @field_validator("grains", mode="before")
    @classmethod
    def _grains(cls, v: Any) -> Any:
        return [_normalise(g, _GRAIN_ALIASES) for g in v] if isinstance(v, list | tuple) else v or ()

    @field_validator("measures", mode="before")
    @classmethod
    def _measures(cls, v: Any) -> Any:
        return [_normalise(a, _AGG_ALIASES) for a in v] if isinstance(v, list | tuple) else v or ()

    @field_validator("colors", mode="before")
    @classmethod
    def _colors(cls, v: Any) -> Any:
        return _colors(v)

    @model_validator(mode="after")
    def _consistent(self) -> FieldDef:
        if self.grains and not self.type.temporal:
            raise ValueError(f"field {self.name!r}: grains need a date or timestamp field, not {self.type.value}")
        numeric_only = NUMERIC_ONLY.intersection(self.measures)
        if numeric_only and not self.type.numeric:
            names = ", ".join(sorted(a.value for a in numeric_only))
            raise ValueError(f"field {self.name!r}: {names} need a numeric field, not {self.type.value}")
        return self

    @property
    def title(self) -> str:
        return self.label or self.name


class Parameter(_Model):
    """A value bound into the report's SQL as ``:name``."""

    name: str = F(validation_alias=AliasChoices("name", "mapping"))
    type: FieldType = FieldType.STRING
    label: str | None = None
    required: bool = False
    default: Any = None

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v: Any) -> Any:
        return _normalise(v, _TYPE_ALIASES)

    @property
    def title(self) -> str:
        return self.label or self.name


class ParamRange(_Model):
    """Two parameters entered together as one range (start, end).

    With ``field``, yamlboard applies the range to that date or timestamp field itself
    (``field BETWEEN start AND end``, keeping the whole end day of a timestamp), so the
    SQL does not mention the two parameters.
    """

    start: str = F(validation_alias=AliasChoices("start", "paramsId"))
    end: str = F(validation_alias=AliasChoices("end", "optionalParamsId"))
    field: str | None = None


class ParamRule(_Model):
    """A check on a parameter value, or on the span between two parameters."""

    param: str = F(validation_alias=AliasChoices("param", "paramsId"))
    other: str | None = F(None, validation_alias=AliasChoices("other", "optionalParamsId"))
    rule: Rule = F(validation_alias=AliasChoices("rule", "type"))
    value: Any

    @field_validator("rule", mode="before")
    @classmethod
    def _rule(cls, v: Any) -> Any:
        return _normalise(v, _RULE_ALIASES)


class MeasureDef(_Model):
    field: str = F("*", validation_alias=AliasChoices("field", "uniqueName"))
    aggregation: Aggregation = Aggregation.COUNT

    @field_validator("aggregation", mode="before")
    @classmethod
    def _agg(cls, v: Any) -> Any:
        return _normalise(v, _AGG_ALIASES)


class View(_Model):
    """The view a report opens on."""

    rows: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    measures: tuple[MeasureDef, ...] = (MeasureDef(),)
    chart: ChartType = ChartType.COLUMN
    grains: dict[str, Grain] = {}
    color: str | None = None  # the colour of a chart with one series (no split)

    @field_validator("rows", "columns", mode="before")
    @classmethod
    def _names(cls, v: Any) -> Any:
        return _names(v)

    @field_validator("chart", mode="before")
    @classmethod
    def _chart(cls, v: Any) -> Any:
        return _normalise(v, _CHART_ALIASES)

    @field_validator("grains", mode="before")
    @classmethod
    def _grains(cls, v: Any) -> Any:
        return {k: _normalise(g, _GRAIN_ALIASES) for k, g in v.items()} if isinstance(v, dict) else v or {}

    @field_validator("color", mode="before")
    @classmethod
    def _color(cls, v: Any) -> Any:
        return None if v is None else color_hex(v)


# A ``:name`` bind, as SQLAlchemy reads it: not a ``::type`` cast, and not ``:name::type`` either
# (write ``cast(:name as type)``). String literals and comments are blanked first.
_BIND = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)(?![:\w])")
_LITERALS = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.DOTALL)


def sql_binds(sql: str) -> set[str]:
    return set(_BIND.findall(_LITERALS.sub(" ", sql)))


class Report(_Model):
    id: str
    title: str
    description: str = ""
    folder: str = ""  # the sub-directory of the reports directory it was read from, set by load_reports
    active: bool = F(True, validation_alias=AliasChoices("active", "actif"))
    datasource: str = "default"
    sql: str
    parameters: tuple[Parameter, ...] = F((), validation_alias=AliasChoices("parameters", "customParameters"))
    ranges: tuple[ParamRange, ...] = ()
    rules: tuple[ParamRule, ...] = ()
    # The columns of the SQL result and what users may do with each. ``fields`` is the older name.
    fields: tuple[FieldDef, ...] = F(validation_alias=AliasChoices("result_columns", "fields"))
    view: View = View()
    rights: tuple[str, ...] = F((), validation_alias=AliasChoices("rights", "right"))  # parsed, not enforced yet
    formats: tuple[str, ...] = ()
    readme: str = ""
    filename: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_legacy(cls, data: Any) -> Any:
        """Map the legacy structural keys (form types, validators, slice, options) onto the canonical ones."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "customParametersFormType" in data and "ranges" not in data:
            forms = data.pop("customParametersFormType") or []
            data["ranges"] = ([f for f in forms if not isinstance(f, dict) or str(f.get("type", "")).upper() == "DATE_RANGE"]
                              if isinstance(forms, list) else forms)
        if "customParametersValidators" in data and "rules" not in data:
            data["rules"] = data.pop("customParametersValidators") or []
        if ("slice" in data or "options" in data) and "view" not in data:
            slice_ = _mapping(data.pop("slice", None), "slice")
            chart = _mapping(_mapping(data.pop("options", None), "options").get("chart"), "options.chart").get("type")
            view = {k: slice_[k] for k in ("rows", "columns", "measures") if slice_.get(k)}
            if chart:
                view["chart"] = chart
            data["view"] = view
        for key in ("rights", "right"):
            if isinstance(data.get(key), list):
                data[key] = [str(r) for r in data[key]]
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Report:
        problems: list[str] = []
        for kind, names in (("field", [f.name for f in self.fields]), ("parameter", [p.name for p in self.parameters])):
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                problems.append(f"duplicate {kind} names: {', '.join(dupes)}")

        fields = self.field_map
        for name in self.view.rows:
            if name not in fields or not fields[name].groupable_rows:
                problems.append(f"view.rows: {name!r} is not a field with groupable_rows")
        for name in self.view.columns:
            if name not in fields or not fields[name].groupable_columns:
                problems.append(f"view.columns: {name!r} is not a field with groupable_columns")
        for name, grain in self.view.grains.items():
            if name not in fields or grain not in fields[name].grains:
                problems.append(f"view.grains: {grain.value!r} is not a grain of field {name!r}")
        for m in self.view.measures:
            if m.field == "*":
                if m.aggregation is not Aggregation.COUNT:
                    problems.append(f"view.measures: '*' only supports count, not {m.aggregation.value}")
            elif m.field not in fields or m.aggregation not in fields[m.field].measures:
                problems.append(f"view.measures: {m.aggregation.value} of {m.field!r} is not allowed by the field")

        params = {p.name for p in self.parameters}
        applied: set[str] = set()
        for r in self.ranges:
            problems += [f"ranges: unknown parameter {n!r}" for n in (r.start, r.end) if n not in params]
            if r.field is not None:
                if r.field not in fields or not fields[r.field].type.temporal:
                    problems.append(f"ranges: {r.field!r} is not a date or timestamp field")
                applied |= {r.start, r.end}
        reserved = sorted(p for p in params if p.lower().startswith("yb_"))
        if reserved:
            problems.append(f"parameter names starting with yb_ are reserved: {', '.join(reserved)}")
        by_name = self.param_map
        for r in self.rules:
            unknown = [n for n in (r.param, r.other) if n and n not in params]
            problems += [f"rules: unknown parameter {n!r}" for n in unknown]
            if not unknown:
                problems += _rule_problems(r, by_name)

        used = sql_binds(self.sql)
        if used - params:
            problems.append(f"sql uses undeclared parameters: {', '.join(sorted(used - params))}")
        if params - used - applied:
            problems.append(f"parameters never used in sql: {', '.join(sorted(params - used - applied))}")
        if used & applied:
            problems.append(f"parameters both in sql and applied by ranges.field: {', '.join(sorted(used & applied))}")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def name(self) -> str:
        """How the report list shows it: ``folder - title``, the title alone at the top of the directory."""
        return f"{self.folder} - {self.title}" if self.folder else self.title

    @property
    def field_map(self) -> dict[str, FieldDef]:
        return {f.name: f for f in self.fields if not f.exclude}

    @property
    def param_map(self) -> dict[str, Parameter]:
        return {p.name: p for p in self.parameters}

    def default_grain(self, name: str) -> Grain | None:
        """The view's grain for a field, else the field's first grain, else none (raw values).

        Used when the period shown is unknown: ``query.default_grain`` picks the grain from the period.
        """
        field = self.field_map.get(name)
        if name in self.view.grains:
            return self.view.grains[name]
        return field.grains[0] if field and field.grains else None



def _rule_problems(r: ParamRule, params: dict[str, Parameter]) -> list[str]:
    """What makes a rule unusable on its parameter's type: it would fail on the first run, not at load."""
    ptype = params[r.param].type
    where = f"rules: {r.rule.value} on {r.param!r} ({ptype.value})"
    number = isinstance(r.value, int | float) and not isinstance(r.value, bool)
    if r.rule in (Rule.MIN, Rule.MAX):
        if ptype.numeric:
            return [] if number else [f"{where}: {r.value!r} is not a number"]
        if ptype.temporal:
            try:
                times.parse_timestamp(r.value) if ptype is FieldType.TIMESTAMP else times.parse_date(r.value)
            except ValueError:
                return [f"{where}: {r.value!r} is not an ISO-8601 {ptype.value}"]
            return []
        return [f"{where}: needs a number, date or timestamp parameter"]
    if r.rule in (Rule.MIN_LENGTH, Rule.MAX_LENGTH):
        return [] if isinstance(r.value, int) and not isinstance(r.value, bool) and r.value >= 0 else [
            f"{where}: {r.value!r} is not a length"]
    if r.rule is Rule.PATTERN:
        try:
            re.compile(str(r.value))
        except re.error as e:
            return [f"{where}: invalid pattern: {e}"]
        return []
    # max_days, max_hours: a span between two dates or timestamps
    problems = [] if number and r.value >= 0 else [f"{where}: {r.value!r} is not a number of {r.rule.value[4:]}"]
    for n in filter(None, (r.param, r.other)):
        if not params[n].type.temporal:
            problems.append(f"rules: {r.rule.value} needs date or timestamp parameters, {n!r} is {params[n].type.value}")
    if r.other and ptype.temporal and params[r.other].type.temporal and params[r.other].type is not ptype:
        problems.append(f"{where}: {r.other!r} is {params[r.other].type.value}, both ends must be the same type")
    return problems


DATASOURCES_FILE = "datasources.yml"


def parse_report(text: str, source: str = "<text>") -> Report:
    """A report from YAML text; ``source`` names it in error messages."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ReportError(f"{source}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ReportError(f"{source}: expected a mapping at the top level")
    try:
        return Report.model_validate(data)
    except ValidationError as e:
        details = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'report'}: {err['msg']}" for err in e.errors())
        raise ReportError(f"{source}: {details}") from e
    except (TypeError, AttributeError) as e:  # a shape no validator expected: this file fails, not the directory
        raise ReportError(f"{source}: malformed report: {e}") from e


def load_report(path: str | Path) -> Report:
    path = Path(path)
    return parse_report(path.read_text(encoding="utf-8"), str(path))


def load_reports(directory: str | Path) -> tuple[dict[str, Report], dict[str, str]]:
    """Every ``*.yml``/``*.yaml`` in ``directory`` and its sub-directories (not hidden ones): (reports by id,
    errors by file). A report's ``folder`` is its sub-directory, ``a/b`` two levels down."""
    root = Path(directory)
    reports: dict[str, Report] = {}
    errors: dict[str, str] = {}
    for path in sorted(root.rglob("*.y*ml")):
        rel = path.relative_to(root)
        top = rel.parent == Path()
        if (path.suffix not in (".yml", ".yaml") or (top and path.stem == Path(DATASOURCES_FILE).stem)
                or any(part.startswith(".") for part in rel.parts)):
            continue
        try:
            report = load_report(path).model_copy(update={"folder": "" if top else rel.parent.as_posix()})
        except ReportError as e:
            errors[str(path)] = str(e)
            continue
        if report.id in reports:
            errors[str(path)] = f"{path}: duplicate report id {report.id!r}"
            continue
        reports[report.id] = report
    return reports, errors
