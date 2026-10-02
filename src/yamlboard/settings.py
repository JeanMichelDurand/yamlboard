"""A user's board settings as a JSON file, to come back to a view: each report's views, chart, grouping, measure,
grains, colours, filters, period and detail options (columns in their order, sort, page size).

Settings live in Streamlit's session state under ``{report_id}:…`` keys; ``export`` reads them and ``restore``
turns a file back into those keys. A file is checked against the reports as they are now: a value a report no
longer allows (a removed field, a measure it dropped) is skipped with a warning, never applied. A range is kept
as its period (``last_7_days``), which ends when the view is opened again; custom dates and the other
parameters are not kept: they are about the day the file was written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from yamlboard import times
from yamlboard.query import Filter, Measure, _check_filter, check_rules, coerce_value
from yamlboard.schema import ChartType, FieldDef, FieldType, Parameter, ParamRange, Report, Rule, color_hex

VERSION = 1
MODES = ("basic", "advanced")
VIEWS = ("Chart", "Pivot", "Detail")
PAGE_SIZES = (1, 5, 10, 50, 200)
OPS = {"in", "not_in", "between", "not_between", "is_null", "not_null"}
CUSTOM = "custom"  # a range entered as two dates
SERIES = "*"  # charts.SERIES: the colour key of a chart with one series


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
        for name, key in (("chart", "chart"), ("row", "row"), ("column", "col"), ("measure", "measure"),
                          ("page_size", "page_size"), ("columns", "detail_cols"), ("sort", "sort"),
                          ("descending", "desc")):
            if f"{rid}:{key}" in state:
                v = state[f"{rid}:{key}"]
                s[name] = v.value if isinstance(v, ChartType) else list(v) if isinstance(v, list | tuple) else v
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
        allowed = VIEWS if mode == "advanced" else ("Chart", "Detail")
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
    if "measure" in s:
        keep("measure", "measure", s["measure"] in measures(r), s["measure"])
    for f, g in _items(s, "grains", skipped, rid):
        grains = {x.value: x for x in fields[f].grains} if f in fields else {}
        if g in grains:
            keys[f"{rid}:grain:{f}"] = grains[g]
        else:
            skipped.append(f"{rid}: grain {g!r} on {f!r} is not allowed")
    if "page_size" in s:
        keep("page_size", "page_size", s["page_size"] in PAGE_SIZES, s["page_size"])
    if "columns" in s:
        cols = s["columns"]
        keep("columns", "detail_cols", isinstance(cols, list) and set(cols) <= set(fields), cols)
    if "sort" in s:
        keep("sort", "sort", s["sort"] is None or s["sort"] in fields, s["sort"])
    if "descending" in s:
        keep("descending", "desc", isinstance(s["descending"], bool), s["descending"])
    colors: dict[str, dict[str, str]] = {}
    for f, values in _items(s, "colors", skipped, rid):
        if (f not in fields and f != SERIES) or not isinstance(values, dict):
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
