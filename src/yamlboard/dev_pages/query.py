"""Test a query: its first rows and the field type of each column, then a report drafted from it."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from yamlboard import dev_state, discover
from yamlboard import query as q
from yamlboard.engine import engine_for
from yamlboard.schema import FieldType, sql_binds

url = st.session_state.url
if url is None:
    st.info("Open a database or a file in the sidebar.", icon=":material/link_off:")
    st.stop()

state = st.session_state
sql = st.text_area(
    "SQL", value=state.dev_sql, height=220, key=f"sql_editor:{state.sql_version}",
    placeholder="SELECT order_date, region, amount FROM sales.orders WHERE order_date >= :since",
    help="`:name` binds a parameter, as in a report. Write `cast(:name as date)`, not `:name::date`.",
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
        result = discover.preview(engine_for(url), sql, params, int(limit))
    except Exception as e:  # noqa: BLE001 — a bad value or any driver's SQL error: shown to the developer
        state.preview = None
        st.error(dev_state.error_text(e), icon=":material/error:")
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
    with st.form("draft", border=False):
        row = st.container(horizontal=True)
        title = row.text_input("Title", value="New report")
        datasource = row.text_input("Datasource", value="default", help="Its name in `datasources.yml`")
        if st.form_submit_button("Draft the YAML", icon=":material/edit_document:", type="primary", disabled=stale):
            report = discover.draft_report(res["sql"], df, title.strip() or "New report",
                                           {name: types[name] for name in sql_binds(res["sql"])},
                                           datasource.strip() or "default")
            dev_state.set_yaml(discover.to_yaml(report))
            st.switch_page(dev_state.page("report"))
