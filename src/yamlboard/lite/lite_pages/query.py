"""Test a query: its first rows and the field type of each column, then a report drafted from it (as the dev
app's Query page, on DuckDB-WASM)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from yamlboard import discover, files
from yamlboard import query as q
from yamlboard.lite import duck
from yamlboard.lite import state as lite_state
from yamlboard.schema import FieldType, sql_binds

state = st.session_state
src = state.source
if src is None:
    st.info("Open a file in the sidebar.", icon=":material/upload_file:")
    st.stop()

sql = st.text_area(
    "SQL", value=state.dev_sql, height=220, key=f"sql_editor:{state.sql_version}",
    placeholder="SELECT * FROM main.my_table" if src["kind"] == "duckdb" else
    f"SELECT * FROM {files.reader(src['path'], src['format'])}",
    help="DuckDB SQL. `:name` binds a parameter, as in a report. An opened file is read from "
         f"`{duck.DATA_DIR}/<its name>`.",
)
state.dev_sql = sql

binds = sorted(sql_binds(sql))
types = state.bind_types
raw: dict[str, str] = {}
if binds:
    with st.container(border=True):
        st.caption("Parameters")
        for name in binds:
            row = st.container(horizontal=True)
            kinds = [t.value for t in FieldType]
            types[name] = FieldType(row.selectbox(
                f":{name} type", kinds, index=kinds.index(types.get(name, FieldType.STRING).value),
                key=f"bind_type:{name}"))
            raw[name] = row.text_input(f":{name} value", key=f"bind_value:{name}",
                                       placeholder="YYYY-MM-DD" if types[name].temporal else None)

row = st.container(horizontal=True, vertical_alignment="bottom")
limit = row.number_input("Rows", min_value=1, max_value=discover.MAX_PREVIEW_ROWS, value=discover.PREVIEW_ROWS,
                         step=100, key="preview_limit")
if row.button("Run", icon=":material/play_arrow:", type="primary", disabled=not sql.strip()):
    try:
        params = {name: q.coerce(types[name], raw[name]) for name in binds}
        result = await duck.query(lite_state.preview_sql(sql, int(limit)), params)  # noqa: F704, PLE1142
    except Exception as e:  # noqa: BLE001 — a bad value or a SQL error: shown to the user
        state.preview = None
        st.error(duck.error_text(e), icon=":material/error:")
    else:
        state.preview = {"sql": sql, "result": result, "limit": int(limit)}
        state.tested_params.update(params)  # the Report page starts from these values

res = state.preview
if not res:
    st.stop()

df: pd.DataFrame = res["result"]
stale = res["sql"] != sql
if stale:
    st.warning("The SQL changed since this run: run it again before drafting a report.", icon=":material/history:")
st.caption(f"{len(df):,} rows" + (" (the limit)" if len(df) == res["limit"] else ""))
st.dataframe(df, hide_index=True)

with st.container(border=True):
    st.subheader("Draft a report", anchor=False)
    inferred = pd.DataFrame({"column": [str(c) for c in df.columns],
                             "field type": [discover.infer_type(df[c]).value for c in df.columns]})
    st.dataframe(inferred, hide_index=True)
    if df.empty:
        st.caption(":orange[No rows: every column is drafted as a string. Run on rows to infer the types.]")
    database = src["kind"] == "duckdb"
    with st.form("draft", border=False):
        row = st.container(horizontal=True)
        title = row.text_input("Title", value="New report")
        if database:
            datasource = row.text_input("Datasource", value=src["alias"], help="Its name in `datasources.yml`")
            real_path = src["path"]
        else:
            datasource = "default"
            real_path = row.text_input(
                "File path on your machine", value=src["name"],
                help="Where the board will read the file: this page only holds a copy, so the report needs the real "
                     "path. A glob (`/var/log/app/*.log`) reads several files.")
        if st.form_submit_button("Draft the YAML", icon=":material/edit_document:", type="primary", disabled=stale):
            report_sql = res["sql"].replace(files.literal(src["path"]), files.literal(real_path.strip() or src["name"]))
            report = discover.draft_report(report_sql, df, title.strip() or "New report",
                                           {name: types[name] for name in sql_binds(res["sql"])},
                                           datasource.strip() or "default")
            lite_state.set_yaml(discover.to_yaml(report))
            st.switch_page(lite_state.page("report"))
    if database:
        st.caption(f"On the board, `datasources.yml` maps the datasource to the database file: "
                   f"`{src['alias']}: duckdb:////path/to/{src['name']}?access_mode=read_only`.")
    else:
        st.caption("On the board, the `default` datasource is an in-memory DuckDB, which reads the file at that path.")
