"""Turn a report and a user's choices into SQL that runs on the source database.

The report's SQL becomes a subquery. Every filter, grouping and measure is checked
against the permissions its field declares before any SQL is written. Values are
always bound, never inlined; identifiers only ever come from the report.

Field names match the SQL's columns ignoring case: ``probe`` asks the database for the
real column names, ``resolve_columns`` maps each field onto one, and the builders quote
that exact name. Output columns always carry the field name as written in the YAML.

Filters combine the same way everywhere: filters on different fields must all match; on one
field, the inclusions (``in``, ``between``, ``is_null``) are alternatives and the exclusions
(``not_in``, ``not_between``, ``not_null``) all apply. An exclusion keeps NULL rows unless NULL
is one of its values or ``not_null`` is set.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from yamlboard import times
from yamlboard.dialects import Dialect
from yamlboard.schema import Aggregation, FieldDef, FieldType, Grain, Report, Rule

MAX_GROUPS = 10_000
MAX_PAGE = 10_000
MAX_IN_VALUES = 1_000  # far below the bind limits of every supported database


class NotAllowed(ValueError):
    """The request uses a field in a way the report does not allow."""


class ColumnMismatch(ValueError):
    """The report's fields and its SQL's columns do not match."""


class InvalidParameters(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


OPS = ("in", "not_in", "between", "not_between", "is_null", "not_null")
NEGATED = {"not_in": "in", "not_between": "between", "not_null": "is_null"}


@dataclass(frozen=True)
class Filter:
    field: str
    op: str
    values: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Group:
    field: str
    grain: Grain | None = None


@dataclass(frozen=True)
class Measure:
    field: str = "*"
    aggregation: Aggregation = Aggregation.COUNT

    @property
    def alias(self) -> str:
        return "count" if self.field == "*" else f"{self.aggregation.value}_{self.field}"


@dataclass(frozen=True)
class Statement:
    sql: str
    binds: dict[str, Any]


# --- parameters -------------------------------------------------------------


def coerce(ftype: FieldType, value: Any) -> Any:
    """A user-entered value as the Python type its field or parameter declares.

    Dates and timestamps follow ``times``: strict ISO-8601 strings, timestamps as naive UTC.
    """
    if value is None or value == "":
        return None
    if ftype is FieldType.DATE:
        return times.parse_date(value)
    if ftype is FieldType.TIMESTAMP:
        return times.parse_timestamp(value)
    if ftype is FieldType.INTEGER:
        return int(value)
    if ftype is FieldType.NUMBER:
        return float(value)
    if ftype is FieldType.BOOLEAN:
        return value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes", "y")
    return str(value)


def coerce_value(ftype: FieldType, value: Any, field: str) -> Any:
    """``coerce`` for a filter value: one that does not fit its field is refused (a forged request, a stale
    settings file), not a crash."""
    try:
        return coerce(ftype, value)
    except (TypeError, ValueError):
        raise NotAllowed(f"{value!r} is not a valid {ftype.value} for {field!r}") from None


def coerce_parameters(report: Report, raw: Mapping[str, Any]) -> dict[str, Any]:
    """The report's parameters, typed and checked against ``required`` and its rules."""
    values: dict[str, Any] = {}
    problems: list[str] = []
    for p in report.parameters:
        try:
            values[p.name] = coerce(p.type, raw.get(p.name, p.default))
        except (TypeError, ValueError):
            problems.append(f"{p.title}: {raw.get(p.name, p.default)!r} is not a valid {p.type.value}"
                            + (" (ISO-8601, UTC)" if p.type.temporal else ""))
            values[p.name] = None
            continue
        if p.required and values[p.name] is None:
            problems.append(f"{p.title} is required")
    unknown = set(raw) - set(values)
    if unknown:
        problems.append(f"unknown parameters: {', '.join(sorted(unknown))}")
    if not problems:
        problems += check_rules(report, values)
    if problems:
        raise InvalidParameters(problems)
    return values


def _bound(ftype: FieldType, value: Any) -> Any:
    """A min or max as its parameter compares: a date or timestamp parsed, a number as written (2.5 stays 2.5)."""
    return coerce(ftype, value) if ftype.temporal else value


def check_rules(report: Report, values: dict[str, Any]) -> list[str]:
    """What breaks the report's rules in ``values``; a rule on a parameter not in ``values`` is skipped."""
    params = report.param_map
    problems = []
    for r in report.rules:
        v, name = values.get(r.param), params[r.param].title
        if v is None:
            continue
        if r.rule in (Rule.MAX_DAYS, Rule.MAX_HOURS):
            other = values.get(r.other) if r.other else None
            if other is None:
                continue
            if other < v:
                problems.append(f"{name} must not be after {params[r.other].title}")
            limit = timedelta(days=r.value) if r.rule is Rule.MAX_DAYS else timedelta(hours=r.value)
            if other - v > limit:
                unit = "day" if r.rule is Rule.MAX_DAYS else "hour"
                problems.append(f"{name} to {params[r.other].title} spans more than {r.value} {unit}(s)")
        elif r.rule is Rule.MIN and v < _bound(params[r.param].type, r.value):
            problems.append(f"{name} must be at least {r.value}")
        elif r.rule is Rule.MAX and v > _bound(params[r.param].type, r.value):
            problems.append(f"{name} must be at most {r.value}")
        elif r.rule is Rule.MIN_LENGTH and len(str(v)) < r.value:
            problems.append(f"{name} must be at least {r.value} characters")
        elif r.rule is Rule.MAX_LENGTH and len(str(v)) > r.value:
            problems.append(f"{name} must be at most {r.value} characters")
        elif r.rule is Rule.PATTERN and not re.fullmatch(str(r.value), str(v)):
            problems.append(f"{name} does not match {r.value}")
    return problems


# --- column names -----------------------------------------------------------


def resolve_columns(report: Report, actual: Sequence[str]) -> dict[str, str]:
    """Each field's column in the SQL result, matched ignoring case."""
    by_lower: dict[str, list[str]] = {}
    for col in actual:
        by_lower.setdefault(col.lower(), []).append(col)
    resolved, missing, ambiguous = {}, [], []
    for name in report.field_map:
        found = by_lower.get(name.lower(), [])
        if len(found) == 1:
            resolved[name] = found[0]
        elif found:
            ambiguous.append(f"{name} ({', '.join(found)})")
        else:
            missing.append(name)
    problems = ([f"fields not in the SQL result: {', '.join(missing)}"] if missing else []) + (
        [f"fields matching several columns: {'; '.join(ambiguous)}"] if ambiguous else []
    )
    if problems:
        raise ColumnMismatch(f"report {report.id!r}: " + "; ".join(problems))
    return resolved


# --- permission checks ------------------------------------------------------


def _field(report: Report, name: str) -> FieldDef:
    try:
        return report.field_map[name]
    except KeyError:
        raise NotAllowed(f"{name!r} is not a field of report {report.id!r}") from None


def _check_filter(report: Report, f: Filter) -> FieldDef:
    field = _field(report, f.field)
    if not field.filterable:
        raise NotAllowed(f"{field.name!r} is not filterable")
    if f.op not in OPS:
        raise NotAllowed(f"unknown filter operator {f.op!r} (allowed: {', '.join(OPS)})")
    if NEGATED.get(f.op, f.op) == "is_null":
        if f.values:
            raise NotAllowed(f"{f.op} on {field.name!r} takes no value, got {len(f.values)}")
    elif NEGATED.get(f.op, f.op) == "between":
        if len(f.values) != 2:
            raise NotAllowed(f"{f.op} on {field.name!r} takes 2 values, got {len(f.values)}")
        if all(v is None or v == "" for v in f.values):
            raise NotAllowed(f"{f.op} on {field.name!r} needs at least one bound")
    elif not f.values:
        raise NotAllowed(f"{f.op} on {field.name!r} needs at least one value")
    return field


def _check_group(report: Report, g: Group, axis: str) -> FieldDef:
    field = _field(report, g.field)
    if not getattr(field, f"groupable_{axis}"):
        raise NotAllowed(f"{field.name!r} cannot be grouped on {axis}")
    if g.grain is not None and g.grain not in field.grains:
        allowed = ", ".join(x.value for x in field.grains) or "none"
        raise NotAllowed(f"{field.name!r} cannot be grouped by {g.grain.value} (allowed: {allowed})")
    return field


def _check_measure(report: Report, m: Measure) -> None:
    if m.field == "*":
        if m.aggregation is not Aggregation.COUNT:
            raise NotAllowed(f"'*' only supports count, not {m.aggregation.value}")
        return
    field = _field(report, m.field)
    if m.aggregation not in field.measures:
        raise NotAllowed(f"{m.aggregation.value} of {field.name!r} is not allowed")


# --- SQL --------------------------------------------------------------------


def _union(values: Any) -> list[Any]:
    """The values once each, in first-seen order."""
    return list(dict.fromkeys(values))


class _Builder:
    def __init__(
        self, report: Report, dialect: Dialect, params: Mapping[str, Any], columns: Mapping[str, str] | None
    ):
        self.report = report
        self.d = dialect
        self.columns = columns
        self.binds: dict[str, Any] = coerce_parameters(report, params)
        self._n = 0

    def col(self, name: str) -> str:
        """The column of field ``name``: its resolved name, quoted, or else the name as the SQL folds it."""
        return self.d.quote(self.columns[name]) if self.columns else self.d.ref(name)

    def bind(self, value: Any) -> str:
        name = f"yb_{self._n}"
        self._n += 1
        self.binds[name] = value
        return f":{name}"

    def source(self) -> str:
        base = self.report.sql.strip().rstrip(";").rstrip()
        return f"FROM (\n{base}\n) yb_base"

    def where(self, filters: Sequence[Filter]) -> str:
        """The report's field-bound ranges, then the user's filters, grouped by field."""
        clauses = self._range_clauses()
        by_field: dict[str, tuple[FieldDef, list[Filter]]] = {}
        for f in filters:
            field = _check_filter(self.report, f)
            by_field.setdefault(field.name, (field, []))[1].append(f)
        for field, fs in by_field.values():
            clauses += self._field_clauses(field, fs)
        return "WHERE " + "\n  AND ".join(clauses) if clauses else ""

    def _range_clauses(self) -> list[str]:
        """``ranges`` with a ``field``: the parameters applied to that field, not written in the SQL."""
        clauses = []
        for r in self.report.ranges:
            if r.field is None:
                continue
            lo, hi = self.binds.get(r.start), self.binds.get(r.end)
            if lo is not None or hi is not None:
                clauses.append(self._between(self.report.field_map[r.field], lo, hi, negate=False))
        return clauses

    def _field_clauses(self, field: FieldDef, filters: list[Filter]) -> list[str]:
        """One OR group for the inclusions, one clause per kind of exclusion."""
        # is_null / not_null are NULL added to the inclusions / exclusions.
        include_values = _union([v for f in filters if f.op == "in" for v in f.values]
                                + [None for f in filters if f.op == "is_null"])
        exclude_values = _union([v for f in filters if f.op == "not_in" for v in f.values]
                                + [None for f in filters if f.op == "not_null"])
        for values in (include_values, exclude_values):
            if len(values) > MAX_IN_VALUES:
                raise NotAllowed(f"at most {MAX_IN_VALUES} values in one filter on {field.name!r}")
        alternatives = [self._in(field, include_values, negate=False)] if include_values else []
        alternatives += [self._between(field, *f.values, negate=False) for f in filters if f.op == "between"]
        clauses = []
        if alternatives:
            clauses.append(alternatives[0] if len(alternatives) == 1 else "(" + " OR ".join(alternatives) + ")")
        if exclude_values:
            clauses.append(self._in(field, exclude_values, negate=True))
        clauses += [self._between(field, *f.values, negate=True) for f in filters if f.op == "not_between"]
        return clauses

    def _in(self, field: FieldDef, raw: Sequence[Any], negate: bool) -> str:
        col = self.col(field.name)
        # '' is a value of a text column, picked from its values: only a typed field reads it as empty.
        values = _union(v if v == "" and field.type is FieldType.STRING else coerce_value(field.type, v, field.name)
                        for v in raw)
        present = [v for v in values if v is not None]
        has_null = len(present) < len(values)
        listed = f"({', '.join(self.bind(v) for v in present)})"
        if not negate:
            parts = ([f"{col} IN {listed}"] if present else []) + ([f"{col} IS NULL"] if has_null else [])
            return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"
        if not present:
            return f"{col} IS NOT NULL"
        # NULL NOT IN (...) is never true: say what NULL rows do.
        if has_null:
            return f"({col} NOT IN {listed} AND {col} IS NOT NULL)"
        return f"({col} IS NULL OR {col} NOT IN {listed})"

    def _between(self, field: FieldDef, lo: Any, hi: Any, negate: bool) -> str:
        """``col BETWEEN lo AND hi``, bounds included, either one optional.

        A timestamp field given a date as its upper bound keeps the whole of that day:
        ``col < day + 1``, since ``col <= day`` stops at midnight.
        """
        col = self.col(field.name)
        whole_day = field.type is FieldType.TIMESTAMP and times.is_day(hi)
        lo = coerce_value(field.type, lo, field.name)
        hi = coerce_value(FieldType.DATE if whole_day else field.type, hi, field.name)
        if lo is not None and hi is not None and lo > (datetime.combine(hi, time()) if whole_day else hi):
            raise NotAllowed(f"between on {field.name!r}: {lo} is after {hi}")
        if whole_day:
            hi = datetime.combine(hi + timedelta(days=1), time())
        if lo is not None and hi is not None and not whole_day:
            expr = f"{col} BETWEEN {self.bind(lo)} AND {self.bind(hi)}"
        else:
            parts = ([f"{col} >= {self.bind(lo)}"] if lo is not None else []) + (
                [f"{col} {'<' if whole_day else '<='} {self.bind(hi)}"] if hi is not None else [])
            expr = " AND ".join(parts)
        if negate:
            return f"({col} IS NULL OR NOT ({expr}))"
        return f"({expr})" if " AND " in expr and "BETWEEN" not in expr else expr

    def group_expr(self, g: Group) -> str:
        col = self.col(g.field)
        return self.d.trunc(g.grain, col) if g.grain else col

    def measure_expr(self, m: Measure) -> str:
        return self.d.aggregate(m.aggregation, "*" if m.field == "*" else self.col(m.field))


def aggregate(
    report: Report,
    dialect: Dialect,
    params: Mapping[str, Any],
    filters: Sequence[Filter] = (),
    rows: Sequence[Group] = (),
    columns: Sequence[Group] = (),
    measures: Sequence[Measure] = (Measure(),),
    limit: int = MAX_GROUPS,
    columns_map: Mapping[str, str] | None = None,
) -> Statement:
    """One row per group: ``rows`` then ``columns`` as keys, one column per measure."""
    groups = [(g, "rows") for g in rows] + [(g, "columns") for g in columns]
    for g, axis in groups:
        _check_group(report, g, axis)
    names = [g.field for g, _ in groups]
    if len(set(names)) < len(names):
        raise NotAllowed("a field can be grouped only once")
    if not measures:
        raise NotAllowed("at least one measure is needed")
    for m in measures:
        _check_measure(report, m)
    if not 0 < limit <= MAX_GROUPS + 1:
        raise NotAllowed(f"limit must be between 1 and {MAX_GROUPS + 1}")

    b = _Builder(report, dialect, params, columns_map)
    keys = [b.group_expr(g) for g, _ in groups]
    select = [f"{k} AS {dialect.quote(g.field)}" for k, (g, _) in zip(keys, groups, strict=True)]
    select += [f"{b.measure_expr(m)} AS {dialect.quote(m.alias)}" for m in measures]
    sql = [f"SELECT {', '.join(select)}", b.source(), b.where(filters)]
    if keys:
        sql += [f"GROUP BY {', '.join(keys)}", f"ORDER BY {', '.join(keys)}"]
    sql.append(dialect.limit(limit))
    return Statement("\n".join(s for s in sql if s), b.binds)


def detail(
    report: Report,
    dialect: Dialect,
    params: Mapping[str, Any],
    filters: Sequence[Filter] = (),
    order_by: str | None = None,
    descending: bool = False,
    limit: int = 100,
    offset: int = 0,
    columns_map: Mapping[str, str] | None = None,
) -> Statement:
    """The report's rows, filtered, one page at a time."""
    if not 0 < limit <= MAX_PAGE or offset < 0:
        raise NotAllowed(f"limit must be between 1 and {MAX_PAGE}, offset not negative")
    b = _Builder(report, dialect, params, columns_map)
    cols = ", ".join(f"{b.col(name)} AS {dialect.quote(name)}" for name in report.field_map)
    sql = [f"SELECT {cols}", b.source(), b.where(filters)]
    if order_by:
        sql.append(f"ORDER BY {b.col(_field(report, order_by).name)} {'DESC' if descending else 'ASC'}")
    sql.append(dialect.limit(limit, offset))
    return Statement("\n".join(s for s in sql if s), b.binds)


def count(
    report: Report,
    dialect: Dialect,
    params: Mapping[str, Any],
    filters: Sequence[Filter] = (),
    columns_map: Mapping[str, str] | None = None,
) -> Statement:
    b = _Builder(report, dialect, params, columns_map)
    sql = ["SELECT count(*) AS n", b.source(), b.where(filters)]
    return Statement("\n".join(s for s in sql if s), b.binds)


def distinct_values(
    report: Report,
    dialect: Dialect,
    params: Mapping[str, Any],
    field: str,
    filters: Sequence[Filter] = (),
    limit: int = 200,
    columns_map: Mapping[str, str] | None = None,
) -> Statement:
    """The values a filter on ``field`` can pick from (only for filterable fields)."""
    _check_filter(report, Filter(field, "in", (None,)))
    b = _Builder(report, dialect, params, columns_map)
    col = b.col(field)
    sql = [f"SELECT DISTINCT {col} AS {dialect.quote(field)}", b.source(), b.where(filters),
           f"ORDER BY {col}", dialect.limit(limit)]
    return Statement("\n".join(s for s in sql if s), b.binds)


def default_view(
    report: Report,
    dialect: Dialect,
    params: Mapping[str, Any],
    columns_map: Mapping[str, str] | None = None,
) -> Statement:
    """The aggregate of the report's ``view``: what the app sends before any user choice.

    As in the app: the first row group, split by the first column field unless the chart has one
    dimension (a pie) or the field is already the row group, measured by the first measure.
    """
    view = report.view
    rows = [Group(f, default_grain(report, f, params)) for f in view.rows[:1]]
    columns = [Group(f, default_grain(report, f, params)) for f in view.columns[:1]
               if not view.chart.one_dimension and f not in view.rows[:1]]
    m = view.measures[0]
    return aggregate(report, dialect, params, rows=rows, columns=columns,
                     measures=[Measure(m.field, m.aggregation)], columns_map=columns_map)


def _instant(value: Any, end: bool) -> datetime | None:
    """A bound as a naive UTC instant; a day as an end bound is its last instant, as ``_between`` keeps it."""
    if value is None:
        return None
    t = times.parse_timestamp(value)
    return t + timedelta(days=1, microseconds=-1) if end and times.is_day(value) else t


def timeframe(report: Report, name: str, params: Mapping[str, Any],
              filters: Sequence[Filter] = ()) -> tuple[datetime, datetime] | None:
    """The first and last instants a date field can hold in the result; None when either end is open.

    The report's range on that field, else its range written in the SQL (``ranges`` with no ``field``),
    narrowed by a ``between`` filter on the field when it is the field's only inclusion (several
    inclusions are alternatives, which can only widen the period).
    """
    ranges = [r for r in report.ranges if r.field == name] or [r for r in report.ranges if r.field is None]
    lo = hi = None
    if ranges:
        lo, hi = _instant(params.get(ranges[0].start), False), _instant(params.get(ranges[0].end), True)
    inclusions = [f for f in filters if f.field == name and f.op in ("in", "between", "is_null")]
    if len(inclusions) == 1 and inclusions[0].op == "between":
        f_lo, f_hi = _instant(inclusions[0].values[0], False), _instant(inclusions[0].values[1], True)
        lo = max((x for x in (lo, f_lo) if x is not None), default=None)
        hi = min((x for x in (hi, f_hi) if x is not None), default=None)
    return (lo, hi) if lo is not None and hi is not None and lo <= hi else None


def default_grain(report: Report, name: str, params: Mapping[str, Any],
                  filters: Sequence[Filter] = ()) -> Grain | None:
    """The grain a date field opens on: picked from the period shown (``times.auto_grain``) when it is known,
    else the report's (``Report.default_grain``)."""
    field = report.field_map.get(name)
    span = timeframe(report, name, params, filters) if field and field.grains else None
    return times.auto_grain(*span, field.grains) if span else report.default_grain(name)


def probe(report: Report, dialect: Dialect, params: Mapping[str, Any]) -> Statement:
    """No rows, only the SQL result's column names, for ``resolve_columns``."""
    b = _Builder(report, dialect, params, None)
    return Statement(f"SELECT *\n{b.source()}\nWHERE 1 = 0", b.binds)
