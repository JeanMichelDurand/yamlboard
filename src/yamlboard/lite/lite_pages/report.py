"""Edit a report YAML and check it: does it load, do its fields match its SQL, what does its view show (as the dev
app's Report page, on DuckDB-WASM and the opened file)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from yamlboard import page_parts
from yamlboard import query as q
from yamlboard.dialects import for_name
from yamlboard.lite import duck
from yamlboard.lite import state as lite_state
from yamlboard.schema import Report, ReportError, parse_report

state = st.session_state
editor, checks = st.columns(2)

with editor:
    yaml_text = st.text_area("Report YAML", value=state.yaml_text, height=560, key=f"yaml_editor:{state.yaml_version}",
                             placeholder="Upload a file in the sidebar, or draft one from the Query page.")
    state.yaml_text = yaml_text

if not yaml_text.strip():
    checks.info("Upload a report YAML in the sidebar, or draft one from a query on the Query page.",
                icon=":material/description:")
    st.stop()

try:
    report: Report = parse_report(yaml_text, "report")
except ReportError as e:
    checks.error(str(e).removeprefix("report: ").replace("; ", "\n\n"), icon=":material/error:")
    st.stop()


async def run(stmt: q.Statement) -> pd.DataFrame:
    src = state.source
    sql = lite_state.localize(stmt.sql, src["name"], src["path"]) if src["kind"] == "file" else stmt.sql
    return await duck.query(sql, stmt.binds)


with checks:
    fields = report.field_map
    st.success(f"**{report.title}** (`{report.id}`) loads: {len(fields)} fields, "
               f"{len(report.parameters)} parameters.", icon=":material/check_circle:")
    row = st.container(horizontal=True)
    row.download_button("Download", yaml_text, f"{report.id}.yml", "application/x-yaml", icon=":material/download:")
    if row.button("Test its SQL", icon=":material/terminal:", disabled=state.source is None):
        src = state.source
        sql = report.sql.strip()
        lite_state.set_sql(lite_state.localize(sql, src["name"], src["path"]) if src["kind"] == "file" else sql,
                           {p.name: p.type for p in report.parameters})
        st.switch_page(lite_state.page("query"))

    page_parts.fields_table(report)

    if state.source is None:
        st.info("Open the report's file in the sidebar to run it.", icon=":material/upload_file:")
        st.stop()
    with st.form("view_form"):
        st.caption(f"Runs on `{state.source['name']}`, whatever the report's datasource (`{report.datasource}`): "
                   "a path in its SQL that names this file reads the copy opened here.")
        raw = {p.name: page_parts.param_widget(p, report) for p in report.parameters}
        submitted = st.form_submit_button("Run the default view", icon=":material/play_arrow:", type="primary")

    if submitted:
        dialect = for_name("duckdb")
        try:
            params = q.coerce_parameters(report, raw)
            cmap = q.resolve_columns(report, list((await run(q.probe(report, dialect, params))).columns))  # noqa: F704, PLE1142
            stmt = q.default_view(report, dialect, params, columns_map=cmap)
            state.view_check = {"yaml": yaml_text, "stmt": stmt, "result": await run(stmt), "params": params}  # noqa: F704, PLE1142
        except Exception as e:  # noqa: BLE001 — bad parameters, missing columns, SQL errors
            state.view_check = None
            st.error(duck.error_text(e), icon=":material/error:")

    done = state.view_check
    if done and done["yaml"] == yaml_text:
        df: pd.DataFrame = done["result"]
        st.caption(f"Every field matches a column. The view returned {len(df):,} rows.")
        page_parts.view_chart(report, df, done["params"])
        st.dataframe(df, hide_index=True)
        with st.expander("SQL", icon=":material/code:"):
            st.code(done["stmt"].sql, language="sql")
            st.caption(f"Binds: {done['stmt'].binds}")
