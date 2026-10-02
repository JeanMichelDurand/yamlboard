"""The opened file: its columns and first rows, or for a log, the pattern that cuts its lines (as the dev app's
File page, on DuckDB-WASM)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from yamlboard import discover, files
from yamlboard.lite import duck
from yamlboard.lite import state as lite_state
from yamlboard.schema import FieldType

state = st.session_state
src = state.source
if src is None:
    st.info("Open a file in the sidebar: CSV, JSON, Parquet, a log, or a DuckDB database. It stays in this browser.",
            icon=":material/upload_file:")
    st.stop()

path: str = src["path"]
fmt: files.Format = src["format"]


def query_this(sql: str) -> None:
    if st.button("Query this file", icon=":material/terminal:", type="primary"):
        lite_state.set_sql(sql)
        st.switch_page(lite_state.page("query"))


st.subheader(src["name"], anchor=False)

if fmt is not files.Format.LOG:
    sql = files.select_sql(path, fmt)
    try:
        df = await lite_state.cached(lite_state.preview_sql(sql, discover.PREVIEW_ROWS))  # noqa: F704, PLE1142
    except Exception as e:  # noqa: BLE001 — a malformed file: shown to the user
        st.error(duck.error_text(e), icon=":material/error:")
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
columns: dict[str, files.Column] = {}
with st.container(border=True):  # one row per group: stlite's st.data_editor needs pyarrow, which it lacks
    st.caption("Columns: a timestamp takes a strptime format such as `%d/%b/%Y:%H:%M:%S %z`; empty is ISO 8601, and "
               "a time without an offset is UTC.")
    for n in names:
        default = defaults.get(n, files.Column(n))
        row = st.container(horizontal=True, vertical_alignment="center")
        row.markdown(f"`{n}`", width=160)
        ftype = row.selectbox(n, types, index=types.index(default.type.value), key=f"log_type:{preset}:{pattern}:{n}",
                              width=180, label_visibility="collapsed")
        fmt_text = ""
        if ftype == FieldType.TIMESTAMP.value:
            fmt_text = row.text_input("Time format", value=default.format or "", key=f"log_fmt:{preset}:{pattern}:{n}",
                                      placeholder="ISO 8601", label_visibility="collapsed")
        columns[n] = files.Column(n, FieldType(ftype), fmt_text.strip() or None)
sql = files.log_sql(path, pattern, columns)

try:
    counts = (await lite_state.cached(files.match_sql(path, pattern))).iloc[0]  # noqa: F704, PLE1142
    df = await lite_state.cached(lite_state.preview_sql(sql, discover.PREVIEW_ROWS))  # noqa: F704, PLE1142
except Exception as e:  # noqa: BLE001 — RE2 rejects what Python accepts (lookaround…): shown to the user
    st.error(duck.error_text(e), icon=":material/error:")
    st.stop()

lines, matched = int(counts["lines"]), int(counts["matched"])
color = "green" if matched == lines else "orange" if matched else "red"
st.markdown(f":{color}-badge[{matched:,} of {lines:,} lines match]")
# Read only when asked: it scans the whole file. A toggle, not an expander that runs its code collapsed
# (its on_change, which could gate it, may be newer than stlite's Streamlit).
if matched < lines and st.toggle("Show the lines that do not match", key=f"unmatched:{pattern}"):
    unmatched = await lite_state.cached(files.unmatched_sql(path, pattern))  # noqa: F704, PLE1142
    st.code("\n".join(unmatched["line"]), language=None)
nulls = [c.name for c in columns.values() if c.type is not FieldType.STRING and len(df) and df[c.name].isna().all()]
if nulls:
    st.caption(f":orange[No value converts in {', '.join(nulls)}: check the type or the time format.]")
st.dataframe(df, hide_index=True)
with st.expander("SQL", icon=":material/code:"):
    st.code(sql, language="sql")
query_this(sql)
