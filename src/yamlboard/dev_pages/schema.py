"""The connection's tables and views, and the columns of the one selected."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from yamlboard import dev_state, discover
from yamlboard.engine import engine_for

url = st.session_state.url
if url is None:
    st.info("Open a database or a file in the sidebar.", icon=":material/link_off:")
    st.stop()


@st.cache_data(ttl=60, max_entries=16, show_spinner="Reading the schema…")
def tables(url: str) -> pd.DataFrame:
    return discover.tables(engine_for(url))


@st.cache_data(ttl=60, max_entries=256, show_spinner=False)
def columns(url: str, schema: str, table: str) -> pd.DataFrame:
    return discover.columns(engine_for(url), schema, table)


all_tables = tables(url)
if all_tables.empty:
    st.info("No table or view is visible on this connection.", icon=":material/table:")
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
    st.subheader(f"{row['schema']}.{row['table']}")
    st.dataframe(columns(url, row["schema"], row["table"]), hide_index=True,
                 column_config={"nullable": st.column_config.CheckboxColumn("Nullable")})
    if st.button("Query this table", icon=":material/terminal:", type="primary"):
        dev_state.set_sql(discover.select_all(engine_for(url), row["schema"], row["table"]))
        st.switch_page(dev_state.page("query"))
