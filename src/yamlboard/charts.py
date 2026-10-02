"""The chart of an aggregate result: one row group on the axis, optionally split by a column field.

Shared by the board and the dev app, so a report's chart looks the same in both. Bars and slices of a
date grouped by a grain sit on a band axis, one per period (``2026-03``), not on a continuous timeline
where a month is one hair-thin day. Lines and areas keep a time axis, with one tick per period when the
periods are few.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import altair as alt
import pandas as pd

from yamlboard import query as q
from yamlboard import times
from yamlboard.schema import EMPTY, Aggregation, ChartType, Grain, Report, value_key

AGG_LABELS = {
    Aggregation.COUNT: "Count", Aggregation.COUNT_DISTINCT: "Distinct count", Aggregation.SUM: "Sum",
    Aggregation.AVG: "Average", Aggregation.MIN: "Min", Aggregation.MAX: "Max", Aggregation.MEDIAN: "Median",
}
# Series colours on a light background, contrast-checked: #1baf7a, #eda100 and #e87ba4 are under 3:1, so the
# tooltip and the pivot carry the numbers. No red: red means an error or a warning in the app, unless a report
# or a user picks it for a value (an ASH chart shows row lock contention in red).
PALETTE = ["#2A78D6", "#EB6834", "#1BAF7A", "#EDA100", "#E87BA4", "#008300", "#4A3AA7", "#52514E"]
BANDED = (ChartType.COLUMN, ChartType.COLUMN_STACKED, ChartType.BAR, ChartType.BAR_STACKED)
# d3 time formats of a time axis, per grain (%G-W%V: the ISO week).
AXIS_FORMATS = {Grain.MINUTE: "%Y-%m-%d %H:%M", Grain.HOUR: "%Y-%m-%d %H:00", Grain.DAY: "%Y-%m-%d",
                Grain.WEEK: "%G-W%V", Grain.MONTH: "%Y-%m", Grain.QUARTER: "%Y-Q%q", Grain.YEAR: "%Y"}
MAX_TICKS = 24
# The colours offered in one click on the board, beside the picker: the palette, then the ones a report on
# incidents wants (red, dark red, bright green, grey).
QUICK_COLORS = [c.lower() for c in PALETTE] + ["#d00000", "#8b1a00", "#00cc00", "#9e9e9e"]
SERIES = "*"  # the colour key of a chart with one series: no split, no slices


def measure_label(report: Report, m: q.Measure) -> str:
    if m.field == "*":
        return "Row count"
    return f"{AGG_LABELS[m.aggregation]} of {report.field_map[m.field].title}"


def title(report: Report, measure: q.Measure, x: str | None, x_grain: Grain | None = None,
          color: str | None = None, color_grain: Grain | None = None) -> str:
    """``Sum of Amount by Order date (month), split by Region``: the field labels as the report writes them."""
    fields = report.field_map

    def name(f: str, g: Grain | None) -> str:
        return fields[f].title + (f" ({g.value})" if g else "")

    text = measure_label(report, measure)
    if x:
        text += f" by {name(x, x_grain)}"
    if color:
        text += f", split by {name(color, color_grain)}"
    return text


def _ms(values: pd.Series) -> list[int]:
    """Epoch milliseconds, what a Vega-Lite time axis takes as tick values."""
    ts = pd.to_datetime(values, utc=True)
    return sorted(((ts - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)).astype(int).unique().tolist())


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


def legend(df: pd.DataFrame, kind: ChartType, report: Report, x: str, color: str | None = None,
           x_grain: Grain | None = None, color_grain: Grain | None = None) -> tuple[str | None, list[str]]:
    """The field the chart colours by (the slices of a pie, else the split, else none) and its values as text,
    in legend order (the values' own order, empty last). One series: (None, [SERIES])."""
    field, grain = (x, x_grain) if kind.one_dimension else (color, color_grain)
    if not field:
        return None, [SERIES]
    ordered = df[field].drop_duplicates().sort_values(na_position="last")
    return field, _text(ordered, report.field_map[field].type.temporal, grain).drop_duplicates().tolist()


def palette(report: Report, field: str | None, values: list[str],
            colors: Mapping[str, str] | None = None) -> dict[str, str]:
    """Each value's colour: the user's (``colors``), else the report's, else the palette's in legend order."""
    preset = report.field_map[field].colors if field else ({SERIES: report.view.color} if report.view.color else {})
    chosen = {**preset, **(colors or {})}
    return {v: chosen.get(v, PALETTE[i % len(PALETTE)]) for i, v in enumerate(values)}


def build(df: pd.DataFrame, kind: ChartType, report: Report, x: str, measure: q.Measure,
          color: str | None = None, x_grain: Grain | None = None, color_grain: Grain | None = None,
          colors: Mapping[str, str] | None = None) -> alt.Chart:
    """``kind`` of ``measure`` by field ``x``, split by field ``color`` (ignored by a one-dimension chart).

    ``x_grain`` and ``color_grain``: the grain each date field is grouped by, for its labels. ``colors``: the
    user's colour of each value of the coloured field (see ``legend``), over the report's own.
    """
    fields = report.field_map
    y = measure.alias
    y_title = measure_label(report, measure)
    if kind.one_dimension:
        color = None
    banded = kind in BANDED or kind.one_dimension
    x_temporal = fields[x].type.temporal
    dim, values = legend(df, kind, report, x, color, x_grain, color_grain)
    hues = palette(report, dim, values, colors)
    labels: dict[str, pd.Series] = {}
    if x_temporal and banded and x_grain:
        labels[x] = times.period_label(df[x], x_grain)
    if dim:  # coloured by text, so the colours of the values match the legend's
        labels[dim] = _text(df[dim], fields[dim].type.temporal, x_grain if dim == x else color_grain)
    data = df.assign(**labels) if labels else df

    tooltip = [alt.Tooltip(c, title=fields[c].title if c in fields else y_title) for c in df.columns]
    base = alt.Chart(data)
    top_legend = alt.Legend(orient="top", title=None)
    scale = alt.Scale(domain=values, range=[hues[v] for v in values])
    if kind.one_dimension:
        arc = base.mark_arc(innerRadius=70) if kind is ChartType.DONUT else base.mark_arc()
        return arc.encode(theta=alt.Theta(f"{y}:Q", title=y_title),
                          color=alt.Color(f"{x}:N", title=fields[x].title, scale=scale, legend=top_legend),
                          tooltip=tooltip)

    if x_temporal and not banded:  # a time axis, in UTC (not the browser's zone)
        axis = alt.Axis(format=AXIS_FORMATS[x_grain], labelAngle=0) if x_grain else alt.Axis()
        if x_grain and data[x].nunique() <= MAX_TICKS:
            axis = alt.Axis(format=AXIS_FORMATS[x_grain], values=_ms(data[x].dropna()), labelAngle=0)
        x_enc = alt.X(f"{x}:T", title=fields[x].title, scale=alt.Scale(type="utc"), axis=axis)
    else:
        x_enc = alt.X(f"{x}:{'O' if x_temporal else 'N'}", title=fields[x].title)
    y_enc = alt.Y(f"{y}:Q", title=y_title)
    enc: dict[str, Any] = {"tooltip": tooltip}
    mark: dict[str, Any] = {}
    if color:
        enc["color"] = alt.Color(f"{color}:N", title=fields[color].title, scale=scale, legend=top_legend)
    else:
        mark["color"] = hues[SERIES]

    if kind in (ChartType.BAR, ChartType.BAR_STACKED):
        x_enc = alt.Y(x_enc.shorthand, title=fields[x].title)
        y_enc = alt.X(f"{y}:Q", title=y_title, stack=kind is ChartType.BAR_STACKED or None)
        if color and kind is ChartType.BAR:
            enc["yOffset"] = f"{color}:N"
        return base.mark_bar(**mark).encode(y=x_enc, x=y_enc, **enc)
    if kind in (ChartType.COLUMN, ChartType.COLUMN_STACKED):
        if color and kind is ChartType.COLUMN:
            enc["xOffset"] = f"{color}:N"
        return base.mark_bar(**mark).encode(x=x_enc, y=y_enc, **enc)
    if kind is ChartType.AREA:
        return base.mark_area(**mark).encode(x=x_enc, y=y_enc.stack(True), **enc)
    return base.mark_line(point=len(df) < 60, **mark).encode(x=x_enc, y=y_enc, **enc)
