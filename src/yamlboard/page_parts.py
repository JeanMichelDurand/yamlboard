"""What the dev and lite apps' Report pages share: one runs on a server, the other in the browser (stlite), so
their pages differ only in how they run SQL."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd
import streamlit as st

from yamlboard import charts, settings, times
from yamlboard import query as q
from yamlboard.schema import Aggregation, FieldType, Grain, Parameter, Report


def param_widget(p: Parameter, report: Report) -> Any:
    key = f"report_param:{report.id}:{p.name}"
    label = p.title + (" *" if p.required else "")
    try:  # the value last run on the Query page, where the bind may have had another type
        tested = q.coerce(p.type, st.session_state.tested_params.get(p.name))
    except (TypeError, ValueError):
        tested = None
    default = tested if tested is not None else q.coerce(p.type, p.default)
    if p.type.temporal:
        starts = {r.start for r in report.ranges}
        today = times.today()
        return st.date_input(label + " (UTC)", value=default or (today - timedelta(days=30) if p.name in starts
                                                                  else today), key=key)
    if p.type.numeric:
        return st.number_input(label, value=default, step=1 if p.type is FieldType.INTEGER else None, key=key)
    if p.type is FieldType.BOOLEAN:
        return st.checkbox(label, value=bool(default), key=key)
    return st.text_input(label, value=default or "", key=key)


def field_frame(report: Report) -> pd.DataFrame:
    """What each result column allows, one row per field."""
    return pd.DataFrame([{
        "field": f.name, "type": f.type.value, "visible": f.visible, "filter": f.filterable, "rows": f.groupable_rows,
        "columns": f.groupable_columns, "grains": [g.value for g in f.grains],
        "measures": [a.value for a in f.measures],
        "colors": ", ".join(f"{v} {c}" for v, c in f.colors.items()),
    } for f in report.field_map.values()])


def fields_table(report: Report, key: str | None = None) -> pd.DataFrame:
    """``field_frame`` shown; with ``key``, an editor of it (the dev app), and the table as edited."""
    df = field_frame(report)
    config = {
        "type": st.column_config.SelectboxColumn("Type", options=[t.value for t in FieldType], required=True),
        "visible": st.column_config.CheckboxColumn("Shown", help="In the detail, before any column is picked"),
        "filter": st.column_config.CheckboxColumn("Filter"),
        "rows": st.column_config.CheckboxColumn("Rows"),
        "columns": st.column_config.CheckboxColumn("Columns"),
        "grains": st.column_config.MultiselectColumn("Grains", options=[g.value for g in Grain],
                                                     help="Dates: day to year; timestamps: minute to year"),
        "measures": st.column_config.MultiselectColumn("Measures", options=[a.value for a in Aggregation],
                                                       help="Sum, avg, min, max, median: numbers only"),
    }
    if key is None:  # lists as text, no type picker: the lite app runs on stlite, whose Arrow is a stub
        st.dataframe(df.assign(grains=df["grains"].str.join(", "), measures=df["measures"].str.join(", ")),
                     hide_index=True, column_config={k: config[k] for k in ("visible", "filter", "rows", "columns")})
        return df
    return st.data_editor(df, hide_index=True, column_config=config, key=key, disabled=["field", "colors"],
                          num_rows="fixed")


def field_edits(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """``fields_table``'s rows as ``discover.with_fields`` takes them, by field name."""
    return {r["field"]: {"type": r["type"], "visible": bool(r["visible"]), "filterable": bool(r["filter"]),
                         "groupable_rows": bool(r["rows"]), "groupable_columns": bool(r["columns"]),
                         "grains": list(r["grains"] or []), "measures": list(r["measures"] or [])}
            for r in df.to_dict("records")}


def view_chart(report: Report, df: pd.DataFrame, params: dict[str, Any]) -> None:
    """The chart the board opens on, from the result of ``q.default_view``."""
    view = report.view
    measures = q.view_measures(report)
    shown = measures[:1] if view.chart.one_dimension else measures
    if view.rows and len(df):  # the chart the board opens on
        x = view.rows[0]
        split = [c for c in view.columns[:1] if not view.chart.one_dimension and c != x and len(measures) == 1]
        color = split[0] if split else None
        filters = settings.view_filters(report)
        x_grain = q.default_grain(report, x, params, filters)
        df = df.assign(**{m.alias: pd.to_numeric(df[m.alias]) for m in shown})
        fmt = view.chart_format()
        span = q.timeframe(report, x, params, filters) if x_grain else None
        filled = charts.fill(df, view.chart, report, x, shown, color, x_grain, fmt.empty, span, trim=fmt.trim_empty)
        st.altair_chart(charts.build(df if filled is None else filled, view.chart, report, x, shown, color, x_grain,
                                     color and q.default_grain(report, color, params, filters), fmt=fmt))
    elif not view.rows:
        st.caption("No `view.rows`: the board opens on the pivot, not a chart.")
