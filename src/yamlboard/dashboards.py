"""Dashboards: a grid of charts, one per cell, each a report seen through a saved view, in a JSON file.

    {
      "yamlboard_dashboard": 1,
      "name": "Sales overview",
      "description": "Optional, under the name.",
      "grid": {"columns": 2, "height": 320},
      "cells": [
        {"view": "views/sales-view.json"},
        {"report": "sales", "title": "Orders by region", "width": 2,
         "view": {"chart": "bar", "row": "region", "measure": "count"}}
      ]
    }

The cells fill the grid row by row, ``width`` grid columns each (default 1). A cell's ``view`` is a saved view
file (the board's :material/bookmark:), by its path from the dashboard's directory, or one report's part of
such a file, written in the cell next to its ``report``; no view is the report's own. Only the chart is shown:
the view's chart, grouping, measure, grains, colours, filters and period. The view is checked as an opened view
is (``settings.restore``): what a report no longer allows is skipped, and said so on the cell.

Dashboard files live in the reports directory's ``dashboards/`` (and its sub-directories); the board can also
open one from the reader's disk, and write one from saved view files.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from yamlboard import charts, settings, times
from yamlboard import query as q
from yamlboard.schema import ChartType, Grain, Report

VERSION = 1
MAX_COLUMNS = 6
DEFAULT_COLUMNS = 2
DEFAULT_HEIGHT = 320
HEIGHTS = (150, 1200)  # the chart's height in pixels, bounds included


class DashboardError(ValueError):
    """Not a dashboard file."""


@dataclass(frozen=True)
class Cell:
    """One chart: ``report``'s id, seen through ``view`` (one report's settings, as a saved view holds them).

    ``error``: why the cell cannot be drawn (its view file is missing…); the others are drawn.
    """

    report: str | None
    title: str | None = None
    width: int = 1
    view: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class Dashboard:
    name: str
    cells: list[Cell]
    columns: int = DEFAULT_COLUMNS
    height: int = DEFAULT_HEIGHT
    description: str = ""
    folder: str = ""

    @property
    def label(self) -> str:
        """``folder - name``, as the board lists reports; the name alone at the top level."""
        return f"{self.folder} - {self.name}" if self.folder else self.name


def _int(grid: Mapping[str, Any], name: str, default: int, lo: int, hi: int) -> int:
    value = grid.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise DashboardError(f"grid {name} must be a whole number from {lo} to {hi}, not {value!r}")
    return value


def _view_file(path: str, base: Path | None) -> tuple[str | None, dict[str, Any]]:
    """A saved view file's report and its settings; DashboardError when it cannot be read."""
    if base is None:
        raise DashboardError(f"view file {path!r}: a dashboard opened from a disk has its views written in")
    target = (base / path).resolve()
    if not target.is_relative_to(base.resolve()):  # the reports' directory only, not the server's disk
        raise DashboardError(f"view file {path!r} is outside the dashboards' directory")
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DashboardError(f"view file {path!r}: {e}") from None
    if not isinstance(doc, dict) or doc.get("yamlboard_settings") != settings.VERSION:
        raise DashboardError(f"view file {path!r} is not a saved view")
    rid = doc.get("report")
    views = doc.get("reports") if isinstance(doc.get("reports"), dict) else {}
    if rid is None and len(views) == 1:
        rid = next(iter(views))
    view = views.get(rid, {})
    return rid, view if isinstance(view, dict) else {}


def _cell(raw: Any, columns: int, base: Path | None) -> Cell:
    if not isinstance(raw, dict):
        return Cell(None, error=f"not a cell: {raw!r}")
    title = raw.get("title") if isinstance(raw.get("title"), str) else None
    width = raw.get("width", 1)
    width = min(max(width, 1), columns) if isinstance(width, int) and not isinstance(width, bool) else 1
    rid, view = raw.get("report"), raw.get("view") or {}
    try:
        if isinstance(view, str):
            from_file, view = _view_file(view, base)
            rid = rid or from_file
        elif not isinstance(view, dict):
            raise DashboardError(f"view {view!r} is neither a file nor a view")
    except DashboardError as e:
        return Cell(rid if isinstance(rid, str) else None, title, width, error=str(e))
    if not isinstance(rid, str):
        return Cell(None, title, width, error="no report")
    return Cell(rid, title, width, view)


def load(text: str | bytes, base: Path | None = None, name: str = "", folder: str = "") -> Dashboard:
    """A dashboard from its JSON; ``base`` is the directory its view files are read from (none: inline views
    only). ``name`` when the file gives none. Raises DashboardError when ``text`` is not a dashboard."""
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise DashboardError(f"not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("yamlboard_dashboard") != VERSION:
        raise DashboardError(f"not a yamlboard dashboard file (version {VERSION})")
    grid = doc.get("grid") or {}
    if not isinstance(grid, dict):
        raise DashboardError(f"grid {grid!r} is not a mapping")
    columns = _int(grid, "columns", DEFAULT_COLUMNS, 1, MAX_COLUMNS)
    height = _int(grid, "height", DEFAULT_HEIGHT, *HEIGHTS)
    cells = doc.get("cells")
    if not isinstance(cells, list) or not cells:
        raise DashboardError("no cells: a dashboard lists its charts in `cells`")
    return Dashboard(
        name=doc["name"] if isinstance(doc.get("name"), str) and doc["name"].strip() else name or "Dashboard",
        cells=[_cell(c, columns, base) for c in cells], columns=columns, height=height,
        description=doc.get("description") if isinstance(doc.get("description"), str) else "", folder=folder,
    )


def load_dir(directory: str | Path) -> tuple[list[Dashboard], dict[str, str]]:
    """Every ``*.json`` dashboard in ``directory`` and its sub-directories (not hidden ones), by label, and the
    errors by file. A saved view file there is a cell's view, not a dashboard: skipped without an error."""
    root = Path(directory)
    found: list[Dashboard] = []
    errors: dict[str, str] = {}
    for path in sorted(root.rglob("*.json")) if root.is_dir() else []:
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            if _is_view(text):
                continue
            found.append(load(text, path.parent, path.stem, "" if rel.parent == Path() else rel.parent.as_posix()))
        except (OSError, DashboardError) as e:
            errors[str(path)] = f"{rel}: {e}"
    return sorted(found, key=lambda d: (d.folder.lower(), d.name.lower())), errors


def _is_view(text: str) -> bool:
    try:
        doc = json.loads(text)
    except ValueError:
        return False
    return isinstance(doc, dict) and "yamlboard_settings" in doc


def grid_rows(cells: Sequence[Cell], columns: int) -> list[list[Cell]]:
    """The cells row by row: a cell wider than what is left of a row starts the next one."""
    rows: list[list[Cell]] = []
    used = columns
    for c in cells:
        if used + c.width > columns:
            rows.append([])
            used = 0
        rows[-1].append(c)
        used += c.width
    return rows


@dataclass(frozen=True)
class Plan:
    """What a cell's chart needs: its parameters before coercion, its query's choices, its colours."""

    raw_params: dict[str, Any]
    filters: list[q.Filter]
    kind: ChartType
    row: str | None
    column: str | None
    measure: q.Measure
    grains: dict[str, Grain | None]
    colors: dict[str, dict[str, str]]
    keys: dict[str, Any]  # the session-state keys of the view, to open it on the Reports page
    skipped: list[str]
    period: str = ""  # the first range's period, as a caption says it

    def groups(self) -> tuple[list[q.Group], list[q.Group]]:
        rows = [q.Group(self.row, self.grains.get(self.row))] if self.row else []
        columns = [q.Group(self.column, self.grains.get(self.column))] if self.column else []
        return rows, columns


def plan(cell: Cell, report: Report) -> Plan:
    """The chart a cell shows: its view's settings over the report's own, as the Reports page opens them."""
    rid = report.id
    keys, skipped = settings.restore(json.dumps({"yamlboard_settings": settings.VERSION, "reports": {rid: cell.view}},
                                                default=str), {rid: report})
    keys.pop("report", None)
    view = report.view

    def setting(name: str, default: Any) -> Any:
        return keys.get(f"{rid}:{name}", default)

    raw: dict[str, Any] = {}
    period = ""
    params = report.param_map
    in_range = set()
    for rg in report.ranges:
        days = settings.day_range(report, rg)
        offered = settings.periods(report, rg)
        name = setting(f"period:{rg.start}", settings.default_period(report, rg, offered))
        if name == settings.CUSTOM:  # its dates are not saved: the period the report opens on
            name = settings.default_period(report, rg, offered)
        if name == settings.CUSTOM:
            bounds = settings.default_range(report, params[rg.start])
        else:
            bounds = times.period_bounds(name, days)
            period = period or times.PERIODS[name][0]
        raw[rg.start], raw[rg.end] = bounds
        in_range |= {rg.start, rg.end}
    for p in report.parameters:
        if p.name not in in_range:
            default = q.coerce(p.type, p.default)
            raw[p.name] = times.today() if default is None and p.type.temporal else default

    kind = setting("chart", view.chart)
    row = setting("row", view.rows[0] if view.rows else None)
    column = None if kind.one_dimension else setting("col", view.columns[0] if view.columns else None)
    if column == row:
        column = None
    measures = settings.measures(report)
    default_m = q.Measure(view.measures[0].field, view.measures[0].aggregation).alias
    measure = measures[setting("measure", default_m if default_m in measures else next(iter(measures)))]
    filters = setting("filters", [])
    grains = {f: setting(f"grain:{f}", None) for f in (row, column) if f and report.field_map[f].grains}
    return Plan(raw, filters, kind, row, column, measure, grains, setting("colors", {}), keys, skipped, period)


def with_grains(p: Plan, report: Report, params: Mapping[str, Any]) -> Plan:
    """``p`` with each date field's grain: the view's, else the one the period shown calls for."""
    grains = {f: g or q.default_grain(report, f, params, p.filters) for f, g in p.grains.items()}
    return replace(p, grains=grains)


def chart_title(p: Plan, report: Report) -> str:
    return charts.title(report, p.measure, p.row, p.grains.get(p.row), p.column, p.grains.get(p.column))


def skeleton(name: str, views: Sequence[tuple[str, bytes]], columns: int = DEFAULT_COLUMNS,
             height: int = DEFAULT_HEIGHT) -> tuple[dict[str, Any], list[str]]:
    """A dashboard of saved view files' contents, one cell each in the files' order, their views written in;
    and the files skipped, with why."""
    cells: list[dict[str, Any]] = []
    skipped: list[str] = []
    for filename, text in views:
        try:
            doc = json.loads(text)
        except ValueError as e:
            skipped.append(f"{filename}: not JSON ({e})")
            continue
        reports = doc.get("reports") if isinstance(doc, dict) else None
        if not isinstance(reports, dict) or doc.get("yamlboard_settings") != settings.VERSION:
            skipped.append(f"{filename}: not a saved view")
            continue
        rid = doc.get("report") if doc.get("report") in reports else next(iter(reports), None)
        if rid is None:
            skipped.append(f"{filename}: no report in it")
            continue
        cells.append({"report": rid, "view": reports[rid]})
    doc = {"yamlboard_dashboard": VERSION, "name": name, "grid": {"columns": columns, "height": height},
           "cells": cells}
    return doc, skipped
