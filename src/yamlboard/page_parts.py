"""What the dev and lite apps' Report pages share: one runs on a server, the other in the browser (stlite), so
their pages differ only in how they run SQL."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd
import streamlit as st

from yamlboard import charts, times
from yamlboard import query as q
from yamlboard.schema import FieldType, Parameter, Report


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


def fields_table(report: Report) -> None:
    """What each result column allows."""
    st.dataframe(pd.DataFrame([{
        "field": f.name, "type": f.type.value, "filter": f.filterable, "rows": f.groupable_rows,
        "columns": f.groupable_columns, "grains": ", ".join(g.value for g in f.grains),
        "measures": ", ".join(a.value for a in f.measures),
        "colors": ", ".join(f"{v} {c}" for v, c in f.colors.items()),
    } for f in report.field_map.values()]), hide_index=True, column_config={
        "filter": st.column_config.CheckboxColumn("Filter"),
        "rows": st.column_config.CheckboxColumn("Rows"),
        "columns": st.column_config.CheckboxColumn("Columns"),
    })


def view_chart(report: Report, df: pd.DataFrame, params: dict[str, Any]) -> None:
    """The chart the board opens on, from the result of ``q.default_view``."""
    view = report.view
    measure = q.Measure(view.measures[0].field, view.measures[0].aggregation)
    if view.rows and len(df):  # the chart the board opens on
        split = [c for c in view.columns[:1] if not view.chart.one_dimension and c != view.rows[0]]
        color = split[0] if split else None
        st.altair_chart(charts.build(df.assign(**{measure.alias: pd.to_numeric(df[measure.alias])}),
                                     view.chart, report, view.rows[0], measure, color,
                                     q.default_grain(report, view.rows[0], params),
                                     color and q.default_grain(report, color, params)))
    elif not view.rows:
        st.caption("No `view.rows`: the board opens on the pivot, not a chart.")
