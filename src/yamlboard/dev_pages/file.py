"""The file opened in the sidebar: its columns and first rows, or for a log, the pattern that cuts its lines."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from yamlboard import dev_state, discover, files
from yamlboard.engine import engine_for, run
from yamlboard.query import Statement
from yamlboard.schema import FieldType

state = st.session_state
path: str = state.file["path"]
fmt: files.Format = state.file["format"]
engine = engine_for(files.MEMORY_URL)


@st.cache_data(ttl=60, max_entries=32, show_spinner="Reading the file…")
def fetch(sql: str) -> pd.DataFrame:
    return run(engine_for(files.MEMORY_URL), Statement(sql, {}))


def query_this(sql: str) -> None:
    if st.button("Query this file", icon=":material/terminal:", type="primary"):
        dev_state.set_sql(sql)
        st.switch_page(dev_state.page("query"))


st.subheader(path, anchor=False)

if fmt is not files.Format.LOG:
    sql = files.select_sql(path, fmt)
    try:
        df = fetch(discover.preview_sql(sql, discover.PREVIEW_ROWS, engine))
    except Exception as e:  # noqa: BLE001 — a malformed file: shown to the developer
        st.error(dev_state.error_text(e), icon=":material/error:")
        st.stop()
    left, right = st.columns([1, 3])
    left.dataframe(pd.DataFrame({"column": [str(c) for c in df.columns],
                                 "field type": [discover.infer_type(df[c]).value for c in df.columns]}),
                   hide_index=True)
    right.caption(f"First {len(df):,} rows")
    right.dataframe(df, hide_index=True)
    query_this(sql)
    st.stop()

# --- a log: pick a format, or write a pattern, then type its groups ------------

with st.expander("First lines", icon=":material/notes:", expanded=True):
    st.code("\n".join(files.head(path)) or "(empty file)", language=None, wrap_lines=False)

CUSTOM = "custom"
row = st.container(horizontal=True, vertical_alignment="bottom")
preset = row.selectbox("Log format", [*files.LOG_FORMATS, CUSTOM], key="log_format", width=320,
                       format_func=lambda k: "Custom pattern" if k == CUSTOM else files.LOG_FORMATS[k].label)
known = files.LOG_FORMATS.get(preset)
pattern = st.text_input(
    "Pattern", value=known.pattern if known else r"^(?P<time>\S+ \S+) (?P<message>.*)$", key=f"log_pattern:{preset}",
    help="A regular expression; each named group `(?P<name>…)` becomes a column, an unnamed group is left out. "
         "RE2 syntax, as DuckDB runs it: no lookaround, no backreference.",
)
if known and known.note:
    st.caption(known.note)
st.caption("A line that does not match is left out: a multi-line entry keeps its first line only.")

try:
    names = [n for n in files.groups(pattern) if n]
except ValueError as e:
    st.error(str(e), icon=":material/error:")
    st.stop()

defaults = known.columns if known and pattern == known.pattern else {}
types = [t.value for t in files.COLUMN_TYPES]
edited = st.data_editor(
    pd.DataFrame([{"column": n, "type": defaults.get(n, files.Column(n)).type.value,
                   "format": defaults.get(n, files.Column(n)).format or ""} for n in names]),
    hide_index=True, disabled=["column"], key=f"log_columns:{preset}:{pattern}",
    column_config={
        "type": st.column_config.SelectboxColumn("Type", options=types, required=True),
        "format": st.column_config.TextColumn(
            "Time format", help="For a timestamp: a strptime format such as `%d/%b/%Y:%H:%M:%S %z`. Empty: ISO 8601, "
                                "and a time without an offset is UTC."),
    },
)
columns = {r.column: files.Column(r.column, FieldType(r.type), (r.format or "").strip() or None)
           for r in edited.itertuples() if r.type in types}
sql = files.log_sql(path, pattern, columns)

try:
    counts = fetch(files.match_sql(path, pattern)).iloc[0]
    df = fetch(discover.preview_sql(sql, discover.PREVIEW_ROWS, engine))
except Exception as e:  # noqa: BLE001 — RE2 rejects what Python accepts (lookaround…): shown to the developer
    st.error(dev_state.error_text(e), icon=":material/error:")
    st.stop()

lines, matched = int(counts["lines"]), int(counts["matched"])
color = "green" if matched == lines else "orange" if matched else "red"
st.markdown(f":{color}-badge[{matched:,} of {lines:,} lines match]")
if matched < lines:
    # Read only once opened: a collapsed expander still runs its code, and this scans the whole file.
    unmatched = st.expander("Lines that do not match", icon=":material/rule:", key=f"unmatched:{pattern}",
                            on_change="rerun")
    if unmatched.open:
        unmatched.code("\n".join(fetch(files.unmatched_sql(path, pattern))["line"]), language=None)
nulls = [c.name for c in columns.values() if c.type is not FieldType.STRING and len(df) and df[c.name].isna().all()]
if nulls:
    st.caption(f":orange[No value converts in {', '.join(nulls)}: check the type or the time format.]")
st.dataframe(df, hide_index=True)
with st.expander("SQL", icon=":material/code:"):
    st.code(sql, language="sql")
query_this(sql)
