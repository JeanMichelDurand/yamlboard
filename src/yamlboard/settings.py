"""A user's board settings as a JSON file, to come back to a view: each report's views, chart, grouping, measures,
grains, partial periods hidden or not, colours, the chart's format (empty cells and their trim, each measure's chart and axis, the
axes), filters, period and detail options (columns in their order, sort, page size).

Settings live in Streamlit's session state under ``{report_id}:…`` keys; ``export`` reads them and ``restore``
turns a file back into those keys. A file is checked against the reports as they are now: a value a report no
longer allows (a removed field, a measure it dropped) is skipped with a warning, never applied. A range is kept
as its period (``last_7_days``), which ends when the view is opened again; custom dates and the other
parameters are not kept: they are about the day the file was written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

from yamlboard import times
from yamlboard.query import OPS as QUERY_OPS
from yamlboard.query import Filter, Measure, _check_filter, check_rules, coerce_value, view_measures
from yamlboard.schema import (
    MAX_DECIMALS,
    SERIES_CHARTS,
    AxisDef,
    AxisSide,
    ChartFormat,
    ChartType,
    Empty,
    FieldDef,
    FieldType,
    Parameter,
    ParamRange,
    Report,
    Rule,
    Series,
    color_hex,
)

VERSION = 1
MODES = ("basic", "advanced")
VIEWS = ("Chart", "Map", "Pivot", "Detail")  # Map: a report with a map section
PAGE_SIZES = (1, 5, 10, 50, 200)
# How a number reads, by its look: the thousands separator, the decimal point. The space is a narrow no-break one,
# so a number never wraps.
NUMBER_STYLES = {"1 234.56": ("\u202f", "."), "1,234.56": (",", "."), "1 234,56": ("\u202f", ","),
                 "1234.56": ("", ".")}
OPS = set(QUERY_OPS)
CUSTOM = "custom"  # a range entered as two dates
SERIES = "*"  # charts.SERIES: the colour key of a chart with one series
MEASURE = "__measure"  # charts.MEASURE: the colour key of a chart of several measures
AXIS_PROPS = tuple(AxisDef.model_fields)  # min, max, log, units, decimals, prefix, suffix


def measures(report: Report) -> dict[str, Measure]:
    """The measures the board offers on ``report``, by alias: a row count, then each field's aggregations."""
    return {m.alias: m for m in [Measure()] + [Measure(f.name, a) for f in report.field_map.values()
                                              for a in f.measures]}


def day_range(report: Report, rg: ParamRange) -> bool:
    """Whether a range is entered as days: not when both its parameters are timestamps."""
    params = report.param_map
    return not (params[rg.start].type is params[rg.end].type is FieldType.TIMESTAMP)


def periods(report: Report, rg: ParamRange) -> list[str]:
    """The periods ending now offered for a range: those its rules accept."""
    days = day_range(report, rg)
    names = times.DAY_PERIODS if days else tuple(times.PERIODS)
    return [n for n in names
            if not check_rules(report, dict(zip((rg.start, rg.end), times.period_bounds(n, days), strict=True)))]


def default_period(report: Report, rg: ParamRange, offered: list[str]) -> str:
    """The longest period a span rule allows, else 7 days (as the custom range); custom when none fits."""
    capped = any({r.param, r.other} == {rg.start, rg.end} and r.rule in (Rule.MAX_DAYS, Rule.MAX_HOURS)
                 for r in report.rules)
    if not offered:
        return CUSTOM
    return offered[-1] if capped or "last_7_days" not in offered else "last_7_days"


def default_range(report: Report, start: Parameter) -> tuple[date, date]:
    """The two dates a custom range opens on: as many days back as its ``max_days`` rule allows, else 7."""
    span = next((r.value for r in report.rules if r.param == start.name and r.rule is Rule.MAX_DAYS), 7)
    today = times.today()
    return today - timedelta(days=int(span)), today


def _value(v: Any) -> Any:
    v = times.iso(v)
    return v.item() if hasattr(v, "item") else v  # numpy scalars from a result


def export(state: Mapping[str, Any], reports: Mapping[str, Report], current: str | None = None) -> dict[str, Any]:
    """Every report's settings found in ``state``; a report never opened has none."""
    out: dict[str, Any] = {}
    for rid, report in reports.items():
        s: dict[str, Any] = {}
        views = {m: list(state[f"{rid}:mode:{m}"]) for m in MODES if f"{rid}:mode:{m}" in state}
        if views:
            s["views"] = views
        for name, key in (("chart", "chart"), ("row", "row"), ("column", "col"), ("measures", "measures"),
                          ("page_size", "page_size"), ("columns", "detail_cols"),
                          ("decimals", "decimals"), ("number_style", "number_style"),
                          ("hide_partial", "hide_partial"), ("empty", "empty"), ("trim_empty", "trim_empty")):
            if f"{rid}:{key}" in state:
                v = state[f"{rid}:{key}"]
                s[name] = v.value if isinstance(v, Enum) else list(v) if isinstance(v, list | tuple) else v
        fmt, _ = format_of(state, report)
        series = {a: {"chart": c.chart.value if c.chart else None, "axis": c.axis.value}
                  for a in sorted(_series_aliases(state, rid)) if (c := fmt.series.get(a))}
        if series:
            s["series"] = series
        axes = {side.value: _axis_dict(a) for side, a in fmt.axes.items() if _axis_dict(a)}
        if any(str(k).startswith(f"{rid}:axis:") for k in state):
            s["axes"] = axes
        grains = {f: state[f"{rid}:grain:{f}"] for f in report.field_map if f"{rid}:grain:{f}" in state}
        if grains:
            s["grains"] = {f: getattr(g, "value", g) for f, g in grains.items() if g is not None}
        colors = {f: dict(c) for f, c in (state.get(f"{rid}:colors") or {}).items() if c}
        if colors:
            s["colors"] = colors
        chosen = {rg.start: state[f"{rid}:period:{rg.start}"] for rg in report.ranges
                  if f"{rid}:period:{rg.start}" in state}
        if chosen:
            s["periods"] = chosen
        if f"{rid}:order" in state:
            s["sort"] = [{"field": f, "descending": bool(d)} for f, d in state[f"{rid}:order"]]
        if state.get(f"{rid}:filters"):
            s["filters"] = [{"field": f.field, "op": f.op, "values": [_value(v) for v in f.values]}
                            for f in state[f"{rid}:filters"]]
        if s:
            out[rid] = s
    doc: dict[str, Any] = {"yamlboard_settings": VERSION}
    if current:
        doc["report"] = current
    if "advanced" in state:
        doc["advanced"] = bool(state["advanced"])
    doc["reports"] = out
    return doc


def _series_aliases(state: Mapping[str, Any], rid: str) -> set[str]:
    """The measures (aliases) whose chart or axis ``state`` holds."""
    return {str(k).rsplit(":", 1)[1] for k in state if str(k).startswith((f"{rid}:series_chart:", f"{rid}:series_axis:"))}


def _axis_dict(a: AxisDef) -> dict[str, Any]:
    """An axis's format as a file or the YAML writes it: its settings that are not the default."""
    return {k: v.value if isinstance(v, Enum) else v for k, v in a.model_dump(exclude_defaults=True).items()}


def format_of(state: Mapping[str, Any], report: Report) -> tuple[ChartFormat, list[str]]:
    """The chart format ``state`` holds for ``report`` (the board's keys, or ``restore``'s), the view's where it
    holds none; and the problems of the axes it could not take (a min above the max), drawn from the data."""
    rid, view = report.id, report.view.chart_format()
    empty = state.get(f"{rid}:empty") or view.empty
    series = dict(view.series)
    for a in _series_aliases(state, rid):
        own = series.get(a, Series())
        series[a] = Series(state.get(f"{rid}:series_chart:{a}", own.chart),
                           state.get(f"{rid}:series_axis:{a}") or own.axis)
    axes: dict[AxisSide, AxisDef] = {}
    problems: list[str] = []
    for side in AxisSide:
        values = (view.axes.get(side) or AxisDef()).model_dump()
        values |= {p: state[f"{rid}:axis:{side.value}:{p}"] for p in AXIS_PROPS if f"{rid}:axis:{side.value}:{p}" in state}
        try:
            axis = AxisDef.model_validate({k: v for k, v in values.items() if v is not None or k in ("min", "max", "decimals")})
        except ValueError as e:
            problems.append(f"{side.value.capitalize()} axis: {_reason(e)}")
            continue
        if axis != AxisDef():
            axes[side] = axis
    trim = state.get(f"{rid}:trim_empty", view.trim_empty)
    return ChartFormat(Empty(empty), series, axes, bool(trim)), problems


def _reason(e: ValueError) -> str:
    """A pydantic error's message, without its location and its links."""
    errors = getattr(e, "errors", None)
    return "; ".join(str(x["msg"]).removeprefix("Value error, ") for x in errors()) if errors else str(e)


def format_keys(report: Report) -> dict[str, Any]:
    """The session-state keys of the chart format ``report``'s view opens with: empty cells, each of its measures'
    chart and axis, both axes."""
    rid, view = report.id, report.view.chart_format()
    keys: dict[str, Any] = {f"{rid}:empty": view.empty, f"{rid}:trim_empty": view.trim_empty}
    for a, c in view.series.items():
        keys[f"{rid}:series_chart:{a}"], keys[f"{rid}:series_axis:{a}"] = c.chart, c.axis
    for side in AxisSide:
        axis = view.axes.get(side) or AxisDef()
        keys |= {f"{rid}:axis:{side.value}:{p}": getattr(axis, p) for p in AXIS_PROPS}
    return keys


def view_filters(report: Report) -> list[Filter]:
    """The filters ``report``'s view opens with, their values of the field's type."""
    fields = report.field_map
    return [Filter(f.field, f.op, tuple(_typed(fields[f.field], v) for v in f.values)) for f in report.view.filters]


def view_keys(report: Report) -> dict[str, Any]:
    """The session-state keys of the view ``report`` opens on: its chart, groups, measures, filters, partial
    periods, the chart's format.

    Grains are not among them: the board picks a date's grain from the period shown, the view's one only without.
    """
    rid, v = report.id, report.view
    return {f"{rid}:chart": v.chart, f"{rid}:row": v.rows[0] if v.rows else None,
            f"{rid}:col": v.columns[0] if v.columns else None,
            f"{rid}:measures": default_measures(report),
            f"{rid}:filters": view_filters(report), f"{rid}:hide_partial": v.hide_partial} | format_keys(report)


def default_measures(report: Report) -> list[str]:
    """The aliases of the measures the view opens with: those the board offers, else its first one."""
    offered = measures(report)
    return [m.alias for m in view_measures(report) if m.alias in offered] or [next(iter(offered))]


def _yaml_value(field: FieldDef, v: Any) -> Any:
    """A filter value as YAML writes it: a date on a date field (a chart pick gives a timestamp), else as typed."""
    v = _typed(field, _value(v))
    return v.date() if field.type is FieldType.DATE and isinstance(v, datetime) else v


def view_of(state: Mapping[str, Any], report: Report) -> dict[str, Any]:
    """``report``'s ``view`` section as the board shows it in ``state``: its YAML mapping.

    What the state does not hold (a control never drawn) keeps the view's value. Each row, column and measure
    list holds the one shown, the board's only one; a value's colour on a split stays on the board (it belongs to
    the field's ``colors``), the colour of a chart with one series is the view's ``color``.
    """
    rid, v, fields = report.id, report.view, report.field_map
    s = export(state, {rid: report})["reports"].get(rid, {})
    row = s["row"] if "row" in s else (v.rows[0] if v.rows else None)
    col = s["column"] if "column" in s else (v.columns[0] if v.columns else None)
    offered = measures(report)
    ms = [offered[a] for a in s.get("measures", []) if a in offered] or view_measures(report)
    fmt, _ = format_of(state, report)
    out: dict[str, Any] = {}
    if row:
        out["rows"] = [row]
    if col and col != row:
        out["columns"] = [col]
    grains = {f: g.value for f, g in v.grains.items()} | {f: g for f, g in s.get("grains", {}).items()
                                                         if f in (row, col)}
    if grains:
        out["grains"] = grains
    out["measures"] = []
    for m in ms:
        item: dict[str, Any] = {"field": "*"} if m.field == "*" else {"field": m.field, "aggregation": m.aggregation.value}
        c = fmt.series.get(m.alias, Series())
        if len(ms) > 1 and c.chart:
            item["chart"] = c.chart.value
        if len(ms) > 1 and c.axis is not AxisSide.PRIMARY:
            item["axis"] = c.axis.value
        out["measures"].append(item)
    out["chart"] = s.get("chart", v.chart.value)
    if color := (s.get("colors") or {}).get(SERIES, {}).get(SERIES) or v.color:
        out["color"] = color
    filters = state[f"{rid}:filters"] if f"{rid}:filters" in state else view_filters(report)
    if filters:
        out["filters"] = [{"field": f.field, "op": f.op}
                          | ({"values": [_yaml_value(fields[f.field], x) for x in f.values]} if f.values else {})
                          for f in filters]
    if s.get("hide_partial", v.hide_partial):
        out["hide_partial"] = True
    if fmt.empty is not Empty.GAPS:
        out["empty"] = fmt.empty.value
    if fmt.trim_empty:
        out["trim_empty"] = True
    axes = {side.value: _axis_dict(a) for side, a in fmt.axes.items() if _axis_dict(a)}
    if axes:
        out["axes"] = axes
    return out


def dumps(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


class SettingsError(ValueError):
    """Not a settings file."""


def _typed(field: FieldDef, v: Any) -> Any:
    if v is None:
        return None
    if field.type.temporal:
        return times.parse_date(v) if times.is_day(v) else times.parse_timestamp(v)
    if field.type is FieldType.INTEGER and isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _items(s: dict[str, Any], name: str, skipped: list[str], rid: str) -> list[tuple[Any, Any]]:
    """The pairs of a mapping setting (``views``, ``grains``); none, and a note, when it is not a mapping."""
    value = s.get(name) or {}
    if not isinstance(value, dict):
        skipped.append(f"{rid}: {name} {value!r} is not a mapping")
        return []
    return list(value.items())


def restore(text: str | bytes, reports: Mapping[str, Report]) -> tuple[dict[str, Any], list[str]]:
    """The session-state keys to set from a settings file, and what was skipped and why.

    Raises SettingsError when ``text`` is not a settings file at all.
    """
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise SettingsError(f"not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("yamlboard_settings") != VERSION or not isinstance(doc.get("reports"), dict):
        raise SettingsError(f"not a yamlboard settings file (version {VERSION})")
    keys: dict[str, Any] = {}
    skipped: list[str] = []
    if isinstance(doc.get("advanced"), bool):
        keys["advanced"] = doc["advanced"]
    if doc.get("report") in reports:
        keys["report"] = doc["report"]
    for rid, s in doc["reports"].items():
        if rid not in reports:
            skipped.append(f"{rid}: no such report")
            continue
        if not isinstance(s, dict):
            skipped.append(f"{rid}: not a settings object")
            continue
        report_keys: dict[str, Any] = {}
        report_skipped: list[str] = []
        try:
            _restore_report(rid, s, reports[rid], report_keys, report_skipped)
        except (TypeError, AttributeError) as e:  # a shape no check expected (a list where a name goes…)
            skipped.append(f"{rid}: malformed settings, none applied ({e})")
            continue
        keys.update(report_keys)
        skipped += report_skipped
    return keys, skipped


def _restore_report(rid: str, s: dict[str, Any], r: Report, keys: dict[str, Any], skipped: list[str]) -> None:
    """One report's settings into ``keys``, what is not allowed into ``skipped``."""
    fields = r.field_map

    def keep(name: str, key: str, ok: bool, value: Any) -> None:
        if ok:
            keys[f"{rid}:{key}"] = value
        else:
            skipped.append(f"{rid}: {name} {s.get(name)!r} is not allowed")

    for mode, views in _items(s, "views", skipped, rid):
        allowed = VIEWS if mode == "advanced" else ("Chart", "Map", "Detail")
        keep("views", f"mode:{mode}", mode in MODES and isinstance(views, list)
             and set(views) <= set(allowed), [v for v in VIEWS if v in (views or [])])
    if "chart" in s:
        keep("chart", "chart", s["chart"] in {c.value for c in ChartType},
             ChartType(s["chart"]) if s["chart"] in {c.value for c in ChartType} else None)
    if "row" in s:
        keep("row", "row", s["row"] is None or (s["row"] in fields and fields[s["row"]].groupable_rows), s["row"])
    if "column" in s:
        keep("column", "col", s["column"] is None or (s["column"] in fields
                                                      and fields[s["column"]].groupable_columns), s["column"])
    offered = measures(r)
    if "measures" in s:
        ms = s["measures"]
        ok = isinstance(ms, list) and bool(ms) and all(isinstance(a, str) and a in offered for a in ms)
        keep("measures", "measures", ok, list(dict.fromkeys(ms)) if ok else None)
    elif "measure" in s:  # a file from before several measures
        keep("measure", "measures", isinstance(s["measure"], str) and s["measure"] in offered, [s["measure"]])
    if "empty" in s:
        keep("empty", "empty", s["empty"] in {e.value for e in Empty},
             Empty(s["empty"]) if s["empty"] in {e.value for e in Empty} else None)
    for alias, c in _items(s, "series", skipped, rid):
        chart = c.get("chart") if isinstance(c, dict) else None
        side = c.get("axis", AxisSide.PRIMARY.value) if isinstance(c, dict) else None
        if (alias not in offered or chart not in (None, *(x.value for x in SERIES_CHARTS))
                or side not in {x.value for x in AxisSide}):
            skipped.append(f"{rid}: series {alias!r} {c!r} is not allowed")
            continue
        keys[f"{rid}:series_chart:{alias}"] = ChartType(chart) if chart else None
        keys[f"{rid}:series_axis:{alias}"] = AxisSide(side)
    if "axes" in s:
        for side in AxisSide:  # a side the file leaves out is the default
            keys |= {f"{rid}:axis:{side.value}:{p}": getattr(AxisDef(), p) for p in AXIS_PROPS}
    for side, a in _items(s, "axes", skipped, rid):
        try:
            axis = AxisDef.model_validate(a)
            keys |= {f"{rid}:axis:{AxisSide(side).value}:{p}": getattr(axis, p) for p in AXIS_PROPS}
        except ValueError as e:  # pydantic's ValidationError is one
            skipped.append(f"{rid}: {side} axis: {_reason(e)}")
    for f, g in _items(s, "grains", skipped, rid):
        grains = {x.value: x for x in fields[f].grains} if f in fields else {}
        if g in grains:
            keys[f"{rid}:grain:{f}"] = grains[g]
        else:
            skipped.append(f"{rid}: grain {g!r} on {f!r} is not allowed")
    if "hide_partial" in s:
        keep("hide_partial", "hide_partial", isinstance(s["hide_partial"], bool), s["hide_partial"])
    if "trim_empty" in s:
        keep("trim_empty", "trim_empty", isinstance(s["trim_empty"], bool), s["trim_empty"])
    if "page_size" in s:
        keep("page_size", "page_size", s["page_size"] in PAGE_SIZES, s["page_size"])
    if "columns" in s:
        cols = s["columns"]
        keep("columns", "detail_cols", isinstance(cols, list) and set(cols) <= set(fields), cols)
    if isinstance(s.get("sort"), list):  # the sort keys, first one first
        order = [(k.get("field"), k.get("descending", False)) for k in s["sort"] if isinstance(k, dict)]
        keep("sort", "order", len(order) == len(s["sort"]) and all(f in fields and isinstance(d, bool) for f, d in order)
             and len({f for f, _ in order}) == len(order), order)
    elif "sort" in s or "descending" in s:  # a file from before several keys: one field and its direction
        field, desc = s.get("sort"), s.get("descending", False)
        keep("sort", "order", (field is None or field in fields) and isinstance(desc, bool),
             [(field, desc)] if field else [])
    if "decimals" in s:
        d = s["decimals"]
        keep("decimals", "decimals", isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= MAX_DECIMALS, d)
    if "number_style" in s:
        keep("number_style", "number_style", s["number_style"] in NUMBER_STYLES, s["number_style"])
    colors: dict[str, dict[str, str]] = {}
    for f, values in _items(s, "colors", skipped, rid):
        if (f not in fields and f not in (SERIES, MEASURE)) or not isinstance(values, dict):
            skipped.append(f"{rid}: colours of {f!r} are not allowed")
            continue
        for v, c in values.items():
            try:
                colors.setdefault(f, {})[str(v)] = color_hex(c)
            except ValueError as e:
                skipped.append(f"{rid}: colour of {v!r} on {f!r}: {e}")
    if "colors" in s:
        keys[f"{rid}:colors"] = colors
    ranges = {rg.start: rg for rg in r.ranges}
    for start, name in _items(s, "periods", skipped, rid):
        if start in ranges and (name == CUSTOM or name in periods(r, ranges[start])):
            keys[f"{rid}:period:{start}"] = name
        else:
            skipped.append(f"{rid}: period {name!r} on {start!r} is not allowed")
    if "filters" in s:
        filters = []
        if not isinstance(s["filters"] or [], list):
            skipped.append(f"{rid}: filters {s['filters']!r} is not a list")
        for f in s["filters"] if isinstance(s["filters"], list) else []:
            entry = f if isinstance(f, dict) else {}
            name, op, values = entry.get("field"), entry.get("op"), entry.get("values", [])
            if name not in fields or not fields[name].filterable or op not in OPS or not isinstance(values, list):
                skipped.append(f"{rid}: filter {f!r} is not allowed")
                continue
            try:
                new = Filter(name, op, tuple(_typed(fields[name], v) for v in values))
                _check_filter(r, new)  # its number of values: a between has two
                for v in new.values:  # a value of the field's type: "abc" on a number would fail the query
                    if not (v == "" and fields[name].type is FieldType.STRING):
                        coerce_value(fields[name].type, v, name)
            except ValueError as e:  # NotAllowed is one
                skipped.append(f"{rid}: filter on {name!r}: {e}")
                continue
            filters.append(new)
        keys[f"{rid}:filters"] = filters
