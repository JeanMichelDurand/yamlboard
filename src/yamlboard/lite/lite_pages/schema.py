"""The opened DuckDB database's tables and views, and the columns of the one selected."""

from __future__ import annotations

import streamlit as st

from yamlboard.dialects import for_name
from yamlboard.files import literal
from yamlboard.lite import duck
from yamlboard.lite import state as lite_state

src = st.session_state.source
db = literal(src["alias"])
try:
    all_tables = await lite_state.cached(  # noqa: F704, PLE1142
        f"SELECT table_schema AS schema, table_name AS table, table_type AS type\nFROM information_schema.tables\n"
        f"WHERE table_catalog = {db}\nORDER BY 1, 2")
except Exception as e:  # noqa: BLE001 — a database this DuckDB cannot read (a newer storage version)
    st.error(duck.error_text(e), icon=":material/error:")
    st.stop()
if all_tables.empty:
    st.info("This database has no table or view.", icon=":material/table:")
    st.stop()

search = st.text_input("Search", placeholder="Table or schema name", icon=":material/search:", key="schema_search")
shown = all_tables
if search:
    hit = all_tables["table"].str.contains(search, case=False, regex=False) | all_tables["schema"].str.contains(
        search, case=False, regex=False)
    shown = all_tables[hit].reset_index(drop=True)

left, right = st.columns([2, 3])
with left:
    st.caption(f"{len(shown):,} of {len(all_tables):,} tables and views")
    event = st.dataframe(shown, hide_index=True, on_select="rerun", selection_mode="single-row",
                         key=f"schema_tables:{search}")
picked = [i for i in event.selection.rows if i < len(shown)]

with right:
    if not picked:
        st.caption("Select a table to see its columns.")
        st.stop()
    row = shown.iloc[picked[0]]
    st.subheader(f"{row['schema']}.{row['table']}", anchor=False)
    cols = await lite_state.cached(  # noqa: F704, PLE1142
        f"SELECT column_name AS column, data_type AS type, is_nullable = 'YES' AS nullable\n"
        f"FROM information_schema.columns\nWHERE table_catalog = {db} AND table_schema = {literal(row['schema'])} "
        f"AND table_name = {literal(row['table'])}\nORDER BY ordinal_position")
    st.dataframe(cols, hide_index=True, column_config={"nullable": st.column_config.CheckboxColumn("Nullable")})
    if st.button("Query this table", icon=":material/terminal:", type="primary"):
        quote = for_name("duckdb").quote
        lite_state.set_sql(f"SELECT *\nFROM {quote(row['schema'])}.{quote(row['table'])}")
        st.switch_page(lite_state.page("query"))
