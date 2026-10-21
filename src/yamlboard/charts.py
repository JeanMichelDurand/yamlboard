"""The chart of an aggregate result: one row group on the axis, optionally split by a column field, or several
measures over it (a combo: each one a column, a line or an area, on the primary or the secondary axis).

Shared by the board and the dev app, so a report's chart looks the same in both. Bars and slices of a
date grouped by a grain sit on a band axis, one per period (``2026-03``), not on a continuous timeline
where a month is one hair-thin day. Lines and areas keep a time axis, with one tick per period when the
periods are few.

An empty cell, a period with no rows or a split value missing at a row group, is a row ``fill`` adds with no
measure: the chart leaves a gap there and a line breaks, unless the chart's format says zero or connect (Excel's
*Hidden and empty cells*).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import altair as alt
import pandas as pd

from yamlboard import query as q
from yamlboard import times
from yamlboard.schema import (
    EMPTY,
    Aggregation,
    AxisDef,
    AxisSide,
    ChartFormat,
    ChartType,
    Empty,
    Grain,
    Report,
    Units,
    value_key,
)

AGG_LABELS = {
    Aggregation.COUNT: "Count", Aggregation.COUNT_DISTINCT: "Distinct count", Aggregation.SUM: "Sum",
    Aggregation.AVG: "Average", Aggregation.MIN: "Min", Aggregation.MAX: "Max", Aggregation.MEDIAN: "Median",
}
# Series colours on a light background, contrast-checked: #1baf7a, #eda100 and #e87ba4 are under 3:1, so the
# tooltip and the pivot carry the numbers. No red: red means an error or a warning in the app, unless a report
# or a user picks it for a value (an ASH chart shows row lock contention in red).
PALETTE = ["#2A78D6", "#EB6834", "#1BAF7A", "#EDA100", "#E87BA4", "#008300", "#4A3AA7", "#52514E"]
BANDED = (ChartType.COLUMN, ChartType.COLUMN_STACKED, ChartType.BAR, ChartType.BAR_STACKED, ChartType.COLUMN_PERCENT,
          ChartType.BAR_PERCENT)
BARS = (ChartType.BAR, ChartType.BAR_STACKED, ChartType.BAR_PERCENT)  # across: the measure on the x axis
PERCENT = (ChartType.COLUMN_PERCENT, ChartType.BAR_PERCENT)
# The charts whose measures can each have a chart of their own (a combo): the upright ones. Bars run across, a pie
# shows one measure.
COMBO = (ChartType.COLUMN, ChartType.COLUMN_STACKED, ChartType.COLUMN_PERCENT, ChartType.LINE, ChartType.AREA)
# d3 time formats of a time axis, per grain (%G-W%V: the ISO week).
AXIS_FORMATS = {Grain.MINUTE: "%Y-%m-%d %H:%M", Grain.HOUR: "%Y-%m-%d %H:00", Grain.DAY: "%Y-%m-%d",
                Grain.WEEK: "%G-W%V", Grain.MONTH: "%Y-%m", Grain.QUARTER: "%Y-Q%q", Grain.YEAR: "%Y"}
MAX_TICKS = 24
# The colours offered in one click on the board, beside the picker: the palette, then the ones a report on
# incidents wants (red, dark red, bright green, grey).
QUICK_COLORS = [c.lower() for c in PALETTE] + ["#d00000", "#8b1a00", "#00cc00", "#9e9e9e"]
SERIES = "*"  # the colour key of a chart with one series: no split, no slices
# A chart of several measures draws them as series: the colour key of their legend, and the two columns of its
# data, one row per row group and measure.
MEASURE, VALUE = "__measure", "__value"
# The selections a chart reports to Python: a click on a mark, a click on the legend (see ``picked``).
CLICK, LEGEND = "click", "legend"
# The fields a pie, a donut or a stacked chart computes for its tooltip: the sum of the slices (or of one column
# or bar), a slice's measure and its share of that sum.
TOTAL, SHARE = "__total", "__share"
# The measures an empty cell reads 0 on with ``Empty.ZERO``: no rows, nothing counted or summed. An average, a min,
# a max or a median of no rows has no value: a gap.
ADDITIVE = (Aggregation.COUNT, Aggregation.COUNT_DISTINCT, Aggregation.SUM)
# Display units: what an axis's values are divided by, and the text after them.
UNITS = {Units.THOUSANDS: (1_000, " k"), Units.MILLIONS: (1_000_000, " M"), Units.BILLIONS: (1_000_000_000, " bn")}


def measure_label(report: Report, m: q.Measure) -> str:
    if m.field == "*":
        return "Row count"
    return f"{AGG_LABELS[m.aggregation]} of {report.field_map[m.field].title}"


def _measures(measure: q.Measure | Sequence[q.Measure]) -> list[q.Measure]:
    return [measure] if isinstance(measure, q.Measure) else list(measure)


def _and(texts: Sequence[str]) -> str:
    """``A``, ``A and B``, ``A, B and C``."""
    return texts[0] if len(texts) == 1 else ", ".join(texts[:-1]) + " and " + texts[-1]


def title(report: Report, measure: q.Measure | Sequence[q.Measure], x: str | None, x_grain: Grain | None = None,
          color: str | None = None, color_grain: Grain | None = None) -> str:
    """``Sum of Amount by Order date (month), split by Region``, ``Sum of Amount and Row count by Region``: the
    field labels as the report writes them."""
    fields = report.field_map

    def name(f: str, g: Grain | None) -> str:
        return fields[f].title + (f" ({g.value})" if g else "")

    text = _and([measure_label(report, m) for m in _measures(measure)])
    if x:
        text += f" by {name(x, x_grain)}"
    if color:
        text += f", split by {name(color, color_grain)}"
    return text


def _ticks(values: pd.Series) -> list[str]:
    """A time axis's tick values: ISO-8601 UTC instants. Streamlit's Vega-Lite draws no label at epoch milliseconds."""
    ts = pd.to_datetime(values, utc=True).drop_duplicates().sort_values()
    return ts.dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist()


def _text(values: pd.Series, temporal: bool, grain: Grain | None) -> pd.Series:
    """Values as a legend shows them, the keys of their colours: a period of a grouped date, else the value."""
    if temporal and grain:
        return times.period_label(values, grain)

    def one(v: Any) -> str:
        if pd.isna(v):
            return EMPTY
        if isinstance(v, float) and v.is_integer():  # an integer column with a NULL comes back as floats
            v = int(v)
        return value_key(v.item() if hasattr(v, "item") else v)

    return values.map(one).astype(object)


def _with_share(chart: alt.Chart, y: str, y_title: str, tooltip: list[alt.Tooltip],
                groupby: list[str]) -> tuple[alt.Chart, list[alt.Tooltip]]:
    """``chart`` computing each mark's measure with its share of its group's sum, ``13502 (95.9%)``, and
    ``tooltip`` showing that instead of the bare measure. ``groupby``: none for a pie (the whole)."""
    total = {TOTAL: f"sum({y})"}
    value, share = f"datum[{y!r}]", f"datum[{y!r}] / datum[{TOTAL!r}]"
    chart = (chart.transform_joinaggregate(**total, groupby=groupby) if groupby else
             chart.transform_joinaggregate(**total)).transform_calculate(
        **{SHARE: f"isValid({value}) ? {value} + ' (' + format({share}, '.1%') + ')' : {value}"})
    return chart, [alt.Tooltip(f"{SHARE}:N", title=y_title) if t.shorthand == y else t for t in tooltip]


def several(kind: ChartType, measures: Sequence[q.Measure]) -> bool:
    """Whether the chart draws several measures, each a series: not a pie, which shows its first."""
    return len(measures) > 1 and not kind.one_dimension


def legend(df: pd.DataFrame, kind: ChartType, report: Report, x: str, color: str | None = None,
           x_grain: Grain | None = None, color_grain: Grain | None = None,
           measures: Sequence[q.Measure] = ()) -> tuple[str | None, list[str]]:
    """The field the chart colours by (the slices of a pie, else the split, else none) and its values as text,
    in legend order (the values' own order, empty last). One series: (None, [SERIES]). Several ``measures``:
    (MEASURE, their labels)."""
    if several(kind, measures):
        return MEASURE, [measure_label(report, m) for m in measures]
    field, grain = (x, x_grain) if kind.one_dimension else (color, color_grain)
    if not field:
        return None, [SERIES]
    ordered = df[field].drop_duplicates().sort_values(na_position="last")
    return field, _text(ordered, report.field_map[field].type.temporal, grain).drop_duplicates().tolist()


def palette(report: Report, field: str | None, values: list[str],
            colors: Mapping[str, str] | None = None) -> dict[str, str]:
    """Each value's colour: the user's (``colors``), else the report's, else the palette's in legend order."""
    if field is None:
        preset = {SERIES: report.view.color} if report.view.color else {}
    else:
        preset = report.field_map[field].colors if field in report.field_map else {}
    chosen = {**preset, **(colors or {})}
    return {v: chosen.get(v, PALETTE[i % len(PALETTE)]) for i, v in enumerate(values)}


def family(kind: ChartType, chart: ChartType | None = None) -> ChartType:
    """The mark a measure is drawn with: its own ``chart`` in a combo (column, line or area), else its chart's:
    COLUMN for any column chart, BAR for any bar chart, LINE, AREA."""
    if chart is not None and kind in COMBO:
        return chart
    if kind in BARS:
        return ChartType.BAR
    return kind if kind in (ChartType.LINE, ChartType.AREA) else ChartType.COLUMN


def marks(kind: ChartType, measures: Sequence[q.Measure], fmt: ChartFormat | None = None) -> dict[str, ChartType]:
    """Each measure's mark (``family``) by alias, in a chart of several measures; none in a chart of one."""
    if not several(kind, measures):
        return {}
    fmt = fmt or ChartFormat()
    return {m.alias: family(kind, fmt.chart_of(m.alias)) for m in measures}


def selections(kind: ChartType, x: str, color: str | None,
               drawn: Mapping[str, ChartType] | None = None) -> dict[str, list[str]]:
    """The fields each selection of the chart picks: a bar or a slice picks its row group and its split value, a
    line or an area only its series (a click on one is not on a period); the legend picks what it colours.
    ``drawn``: the ``marks`` of a chart of several measures, where a column or a bar picks its row group and
    nothing else picks (a measure is no filter)."""
    if drawn:
        return {CLICK: [x]} if any(m in (ChartType.COLUMN, ChartType.BAR) for m in drawn.values()) else {}
    color = None if kind.one_dimension else color
    click = [color] if kind in (ChartType.LINE, ChartType.AREA) else [x, color]
    legend = x if kind.one_dimension else color
    out = {CLICK: [f for f in click if f]}
    if legend:
        out[LEGEND] = [legend]
    return {name: fields for name, fields in out.items() if fields}


def picked(df: pd.DataFrame, report: Report, state: Mapping[str, Any], x: str, color: str | None = None,
           x_grain: Grain | None = None, color_grain: Grain | None = None) -> list[tuple[str, Any, Grain | None]]:
    """What a click picked on the chart of ``df``, from its selection ``state`` (``{selection: [{field: value}]}``):
    each (field, value, grain), the value as the result holds it, not as the chart labels it (``2026-03`` back to
    the first of March, ``(empty)`` back to None)."""
    grains = {x: x_grain, **({color: color_grain} if color else {})}
    fields = report.field_map
    out: list[tuple[str, Any, Grain | None]] = []
    for points in state.values():
        for point in points if isinstance(points, list) else [points]:
            for f, v in (point.items() if isinstance(point, Mapping) else ()):
                if f not in grains:
                    continue
                temporal = fields[f].type.temporal
                labels = dict(zip(_text(df[f], temporal, grains[f]), df[f], strict=True))
                if isinstance(v, str) and v in labels:
                    v = labels[v]
                elif temporal and isinstance(v, int | float):  # a time axis gives epoch milliseconds
                    v = pd.Timestamp(v, unit="ms", tz="UTC")
                v = None if v is None or pd.isna(v) else v
                item = (f, v.item() if hasattr(v, "item") else v, grains[f] if temporal else None)
                if item not in out:
                    out.append(item)
    return out


def fill(df: pd.DataFrame, kind: ChartType, report: Report, x: str, measures: Sequence[q.Measure],
         color: str | None = None, x_grain: Grain | None = None, empty: Empty = Empty.GAPS,
         span: tuple[datetime, datetime] | None = None, at: datetime | None = None,
         trim: bool = False) -> pd.DataFrame | None:
    """``df`` with a row for each empty cell of its chart, its measures empty: every ``x_grain`` period of date field
    ``x`` from ``span``'s start (``query.timeframe``; else the first in ``df``) to its end or ``at`` (default:
    now), whichever comes first (else the last in ``df``); with a split, each ``color`` value at each row group.
    ``trim``: from the first period in ``df`` to the last, whatever ``span`` (a year's range on 90 days of data).
    ``Empty.ZERO``: a count or a sum reads 0 there. A pie has no empty cell. None when the cells would pass
    ``query.MAX_GROUPS``: the chart shows ``df`` as it is."""
    fields = report.field_map
    temporal = x_grain is not None and fields[x].type.temporal
    if kind.one_dimension or not (temporal or color):
        return df
    colors = df[color].drop_duplicates() if color else None
    per_x = len(colors) if colors is not None else 1
    work = df
    if temporal:
        stamps = pd.to_datetime(df[x], utc=True)
        work = df.assign(**{x: stamps})
        known = stamps.dropna()
        first, last = (span[0], min(span[1], at or times.now())) if span and not trim else (None, None)
        if len(known):
            lo, hi = (t.tz_convert(None).to_pydatetime() for t in (known.min(), known.max()))
            first, last = min(first or lo, lo), max(last or hi, hi)
        starts: list[datetime] = []
        if first is not None and first <= last:
            if times.period_count(first, last, x_grain) * per_x > q.MAX_GROUPS:
                return None
            starts = times.period_starts(first, last, x_grain)
        generated = pd.Series(pd.to_datetime(starts, utc=True), dtype=stamps.dtype)
        xs = pd.concat([generated, known]).drop_duplicates().sort_values()
        if stamps.isna().any():
            xs = pd.concat([xs, stamps[stamps.isna()].head(1)])
    else:
        xs = df[x].drop_duplicates()
    if len(xs) * per_x > q.MAX_GROUPS:
        return None
    keys = [x, color] if color else [x]
    grid = (pd.MultiIndex.from_product([xs, colors], names=keys).to_frame(index=False) if color else
            xs.rename(x).reset_index(drop=True).to_frame())
    out = grid.merge(work, on=keys, how="left", indicator=True)
    added = out.pop("_merge") == "left_only"
    if empty is Empty.ZERO and added.any():
        for m in measures:
            if m.aggregation in ADDITIVE and m.alias in out:
                out[m.alias] = out[m.alias].astype(float).where(~added, 0)
    return out


def label_expr(a: AxisDef) -> str | None:
    """The Vega expression of a value axis's labels: in its units, with its decimals, its text before and after;
    None: the axis's own labels."""
    d = a.decimals
    if a.units is Units.PERCENT:
        core = f"format(datum.value, '.{d}%')" if d is not None else "format(datum.value, '.2~%')"
    elif a.units in UNITS:
        div, unit = UNITS[a.units]
        core = f"format(datum.value / {div}, '{f',.{d}f' if d is not None else ',.2~f'}') + {_quote(unit)}"
    elif d is not None:
        core = f"format(datum.value, ',.{d}f')"
    elif a.prefix or a.suffix:
        core = "datum.label"
    else:
        return None
    return " + ".join([*([_quote(a.prefix)] if a.prefix else []), core, *([_quote(a.suffix)] if a.suffix else [])])


def _quote(text: str) -> str:
    """A string literal of a Vega expression."""
    return json.dumps(text, ensure_ascii=False)


def _value(a: AxisDef, percent: bool) -> dict[str, Any]:
    """A value channel's ``scale`` and ``axis`` from its axis's format. A 100% chart's axis is set: 0 to 100%."""
    if percent:
        return {"axis": alt.Axis(format="%")}
    out: dict[str, Any] = {}
    scale = {k: v for k, v in (("domainMin", a.min), ("domainMax", a.max)) if v is not None}
    if a.log:
        scale["type"] = "log"
    if scale:
        out["scale"] = alt.Scale(**scale)
    if expr := label_expr(a):
        out["axis"] = alt.Axis(labelExpr=expr)
    return out


def _mark(mark: ChartType, empty: Empty, a: AxisDef) -> dict[str, Any]:
    """A mark's options: how a line shows an empty cell (it breaks there, Vega-Lite's default, or runs from one
    point to the next), clipped to the axis's bounds. A bar leaves an empty cell out: no bar."""
    out: dict[str, Any] = {}
    if mark in (ChartType.LINE, ChartType.AREA) and empty is Empty.CONNECT:
        out["invalid"] = "filter"
    if a.min is not None or a.max is not None:
        out["clip"] = True
    return out


def _positive(values: pd.Series) -> pd.Series:
    """A log scale has no zero, no negative: those values are left out (Excel will not draw them either)."""
    return values.where(values > 0)


def _x(data: pd.DataFrame, report: Report, x: str, x_grain: Grain | None, banded: bool,
       channel: type[alt.X | alt.Y] = alt.X) -> alt.X | alt.Y:
    """The row group's channel: a band per value or per period, or a time axis in UTC (not the browser's zone).
    Every period of the data has its band, an empty one too (Vega-Lite drops a band whose bars have no value)."""
    field = report.field_map[x]
    if field.type.temporal and not banded:
        axis = alt.Axis(format=AXIS_FORMATS[x_grain], labelAngle=0) if x_grain else alt.Axis()
        if x_grain and data[x].nunique() <= MAX_TICKS:
            axis = alt.Axis(format=AXIS_FORMATS[x_grain], values=_ticks(data[x].dropna()), labelAngle=0)
        return channel(f"{x}:T", title=field.title, scale=alt.Scale(type="utc"), axis=axis)
    if field.type.temporal and x_grain:  # the periods' labels sort in time order, the empty one last
        periods = sorted(set(data[x]) - {EMPTY}) + ([EMPTY] if EMPTY in set(data[x]) else [])
        return channel(f"{x}:O", title=field.title, scale=alt.Scale(domain=periods))
    return channel(f"{x}:{'O' if field.type.temporal else 'N'}", title=field.title)


def build(df: pd.DataFrame, kind: ChartType, report: Report, x: str, measure: q.Measure | Sequence[q.Measure],
          color: str | None = None, x_grain: Grain | None = None, color_grain: Grain | None = None,
          colors: Mapping[str, str] | None = None, selectable: bool = False,
          fmt: ChartFormat | None = None) -> alt.Chart | alt.LayerChart:
    """``kind`` of ``measure`` by field ``x``, split by field ``color`` (ignored by a one-dimension chart).

    ``x_grain`` and ``color_grain``: the grain each date field is grouped by, for its labels. ``colors``: the
    user's colour of each value of the coloured field (see ``legend``), over the report's own. ``selectable``:
    a click on a mark or on the legend is reported (see ``selections`` and ``picked``). Several measures are
    drawn unsplit, each a series (a pie draws its first). ``fmt``: empty cells, each measure's chart and axis,
    each axis's format.
    """
    measures = _measures(measure)
    fmt = fmt or ChartFormat()
    if several(kind, measures):
        return _combo(df, kind, report, x, measures, x_grain, colors, selectable, fmt)
    chart = _build(df, kind, report, x, measures[0], color, x_grain, color_grain, colors, fmt)
    if not selectable:
        return chart
    params = [alt.selection_point(name=name, fields=fields, bind="legend") if name == LEGEND
              else alt.selection_point(name=name, fields=fields, on="click", clear=False)
              for name, fields in selections(kind, x, color).items()]
    return chart.add_params(*params) if params else chart


def _build(df: pd.DataFrame, kind: ChartType, report: Report, x: str, measure: q.Measure, color: str | None,
           x_grain: Grain | None, color_grain: Grain | None, colors: Mapping[str, str] | None,
           fmt: ChartFormat) -> alt.Chart:
    fields = report.field_map
    y = measure.alias
    y_title = measure_label(report, measure)
    if kind.one_dimension:
        color = None
    banded = kind in BANDED or kind.one_dimension
    dim, values = legend(df, kind, report, x, color, x_grain, color_grain)
    hues = palette(report, dim, values, colors)
    percent = kind in PERCENT
    axis = fmt.axis(AxisSide.PRIMARY)
    labels: dict[str, pd.Series] = {}
    if fields[x].type.temporal and banded and x_grain:
        labels[x] = times.period_label(df[x], x_grain)
    if dim:  # coloured by text, so the colours of the values match the legend's
        labels[dim] = _text(df[dim], fields[dim].type.temporal, x_grain if dim == x else color_grain)
    if axis.log and not percent and not kind.one_dimension:
        labels[y] = _positive(df[y])
    data = df.assign(**labels) if labels else df

    tooltip = [alt.Tooltip(c, title=fields[c].title if c in fields else y_title) for c in df.columns]
    base = alt.Chart(data)
    top_legend = alt.Legend(orient="top", title=None)
    scale = alt.Scale(domain=values, range=[hues[v] for v in values])
    if kind.one_dimension:  # a slice's measure with its share of the whole
        arc = base.mark_arc(innerRadius=70) if kind is ChartType.DONUT else base.mark_arc()
        arc, tooltip = _with_share(arc, y, y_title, tooltip, [])
        return arc.encode(theta=alt.Theta(f"{y}:Q", title=y_title),
                          color=alt.Color(f"{x}:N", title=fields[x].title, scale=scale, legend=top_legend),
                          tooltip=tooltip)
    if (kind.stacked or kind is ChartType.AREA) and color:  # a value's share of its column, bar or period
        base, tooltip = _with_share(base, y, y_title, tooltip, [x])

    x_enc = _x(data, report, x, x_grain, banded, alt.Y if kind in BARS else alt.X)
    stack = "normalize" if percent else (True if kind.stacked else None)
    y_label = f"Share of {y_title[0].lower()}{y_title[1:]}" if percent else y_title
    value = _value(axis, percent)
    y_enc = alt.Y(f"{y}:Q", title=y_label, stack=stack, **value)
    enc: dict[str, Any] = {"tooltip": tooltip}
    mark = _mark(family(kind), fmt.empty, axis)
    if color:
        enc["color"] = alt.Color(f"{color}:N", title=fields[color].title, scale=scale, legend=top_legend)
    else:
        mark["color"] = hues[SERIES]

    if kind in BARS:
        y_enc = alt.X(f"{y}:Q", title=y_label, stack=stack, **value)
        if color and kind is ChartType.BAR:
            enc["yOffset"] = f"{color}:N"
        return base.mark_bar(**mark).encode(y=x_enc, x=y_enc, **enc)
    if kind in (ChartType.COLUMN, ChartType.COLUMN_STACKED, ChartType.COLUMN_PERCENT):
        if color and kind is ChartType.COLUMN:
            enc["xOffset"] = f"{color}:N"
        return base.mark_bar(**mark).encode(x=x_enc, y=y_enc, **enc)
    if kind is ChartType.AREA:
        return base.mark_area(**mark).encode(x=x_enc, y=y_enc.stack(True), **enc)
    return base.mark_line(point=len(df) < 60, **mark).encode(x=x_enc, y=y_enc, **enc)


def _combo(df: pd.DataFrame, kind: ChartType, report: Report, x: str, measures: list[q.Measure],
           x_grain: Grain | None, colors: Mapping[str, str] | None, selectable: bool,
           fmt: ChartFormat) -> alt.LayerChart:
    """Several measures over ``x``, each a series in one legend: one layer per mark and axis. The measures on the
    secondary axis get their own scale, on the other side; columns of several measures stand side by side, or
    stacked in a stacked chart."""
    fields = report.field_map
    banded = kind in BANDED
    across = kind in BARS
    percent = kind in PERCENT
    names = {m.alias: measure_label(report, m) for m in measures}
    hues = palette(report, MEASURE, list(names.values()), colors)
    drawn = marks(kind, measures, fmt)
    sides = {a: fmt.axis_of(a) for a in names}
    if len(set(sides.values())) < 2:  # a secondary axis needs a primary one
        sides = dict.fromkeys(names, AxisSide.PRIMARY)

    wide = df
    if fields[x].type.temporal and banded and x_grain:
        wide = df.assign(**{x: times.period_label(df[x], x_grain)})
    data = wide.melt(id_vars=[x], value_vars=list(names), var_name=MEASURE, value_name=VALUE)
    data[VALUE] = pd.to_numeric(data[VALUE])
    for side in set(sides.values()):
        if fmt.axis(side).log and not percent:
            on = data[MEASURE].isin([a for a, s in sides.items() if s is side])
            data[VALUE] = data[VALUE].where(~on, _positive(data[VALUE]))
    data[MEASURE] = data[MEASURE].map(names)

    category = _x(data, report, x, x_grain, banded, alt.Y if across else alt.X)
    x_type = category.shorthand.rsplit(":", 1)[1]
    columns = [names[a] for a, mk in drawn.items() if mk in (ChartType.COLUMN, ChartType.BAR)]
    side_by_side = len(columns) > 1 and not kind.stacked
    color = alt.Color(f"{MEASURE}:N", title=None, legend=alt.Legend(orient="top", title=None),
                      scale=alt.Scale(domain=list(names.values()), range=list(hues.values())))
    tooltip = [alt.Tooltip(f"{x}:{x_type}", title=fields[x].title),
               alt.Tooltip(f"{MEASURE}:N", title="Measure"), alt.Tooltip(f"{VALUE}:Q", title="Value")]
    pickable: list[str] = []
    layers: list[alt.Chart | alt.LayerChart] = []
    for side in AxisSide:
        on_side = [a for a in names if sides[a] is side]
        if not on_side:
            continue
        axis = fmt.axis(side)
        side_title = _and([names[a] for a in on_side])
        side_layers = []
        for mk in dict.fromkeys(drawn[a] for a in on_side):
            group = [names[a] for a in on_side if drawn[a] is mk]
            bars = mk in (ChartType.COLUMN, ChartType.BAR)
            stack = ("normalize" if percent else True) if bars and kind.stacked else None
            shares = stack == "normalize"
            value_title = f"Share of {side_title[0].lower()}{side_title[1:]}" if shares else side_title
            value = (alt.X if across else alt.Y)(f"{VALUE}:Q", title=value_title, stack=stack,
                                                 **_value(axis, shares))
            enc: dict[str, Any] = {"x": value, "y": category} if across else {"x": category, "y": value}
            if bars and side_by_side:
                offset = alt.YOffset if across else alt.XOffset
                enc["yOffset" if across else "xOffset"] = offset(f"{MEASURE}:N", scale=alt.Scale(domain=columns))
            base = alt.Chart().transform_filter(alt.FieldOneOfPredicate(field=MEASURE, oneOf=group))
            opts = _mark(mk, fmt.empty, axis)
            if bars:
                name = f"yb_pick_{len(pickable)}"
                pickable.append(name)
                layer = base.mark_bar(**opts).properties(name=name)
            elif mk is ChartType.AREA:  # measures of different things: overlaid, not stacked
                layer = base.mark_area(opacity=0.5, **opts)
            else:
                layer = base.mark_line(point=len(df) < 60, **opts)
            side_layers.append(layer.encode(color=color, tooltip=tooltip, **enc))
        layers.append(alt.layer(*side_layers) if len(side_layers) > 1 else side_layers[0])
    chart = alt.layer(*layers, data=data)
    if len(layers) > 1:
        chart = chart.resolve_scale(**{"x" if across else "y": "independent"})
    if selectable and pickable:
        chart.params = [alt.TopLevelSelectionParameter(
            name=CLICK, select=alt.PointSelectionConfig(type="point", fields=[x], on="click", clear=False),
            views=pickable)]
    return chart
