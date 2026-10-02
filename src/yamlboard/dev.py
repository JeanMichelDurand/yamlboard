"""The dev app: open a database or a file, browse it, test a query, draft and check a report YAML.

Run with ``yamlboard dev [--url URL | --file PATH] [--yaml FILE]``. A file (CSV, JSON, Parquet, or a log cut
by a pattern) is read by an in-memory DuckDB: see ``files.py``. It is for whoever writes the reports, not for their
readers: it runs whatever SQL is typed in, on the connection given. Every statement is rolled back, but
connect with a read-only account (or ``?access_mode=read_only`` on a DuckDB file) anyway.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from yamlboard import dev_state, files, logs, ui

ASSETS = Path(__file__).with_name("assets")
st.set_page_config(page_title="yamlboard dev", page_icon=str(ASSETS / "yamlboard.png"), layout="wide")
ui.logo(str(ASSETS / "yamlboard.svg"))
try:
    logs.setup("yamlboard-dev.log")
except logs.LogDirError as e:  # logging is compulsory: no log, no app
    st.error(f"{e}. Fix it, or set another directory with `--log-dir`.", icon=":material/error:")
    st.stop()

PAGES = Path(__file__).with_name("dev_pages")
_yaml_file = os.environ.get("YAMLBOARD_DEV_YAML")
dev_state.init(os.environ.get("YAMLBOARD_DEV_URL", "duckdb:///:memory:"),
               Path(_yaml_file).read_text(encoding="utf-8") if _yaml_file else "")
if st.session_state.url is None and st.session_state.conn_error is None:  # the source on the command line
    if _file := os.environ.get("YAMLBOARD_DEV_FILE"):
        st.session_state.file_path = _file
        dev_state.open_file(_file)
    else:
        dev_state.connect(st.session_state.conn)

source = (st.Page(PAGES / "file.py", title="File", icon=":material/description:", default=True)
          if st.session_state.file else
          st.Page(PAGES / "schema.py", title="Schema", icon=":material/table:", default=True))
page = st.navigation([
    source,
    st.Page(PAGES / "query.py", title="Query", icon=":material/terminal:"),
    st.Page(PAGES / "report.py", title="Report", icon=":material/edit_document:"),
], position="top")



@st.dialog("Choose a file", width="large")
def browse() -> None:
    """The server's files, from the opened file's directory: a click on a directory enters it, on a file opens it.

    The dev app runs on the developer's machine, so this is their disk; a browser's own file picker would
    upload a copy, and a report needs the file's path.
    """
    state = st.session_state
    current = Path(state.file["path"]).parent if state.file else Path.cwd()
    here = Path(state.setdefault("browse_dir", str(current)))
    top = st.container(horizontal=True, vertical_alignment="center")
    if top.button(":material/arrow_upward:", help="Parent directory", disabled=here.parent == here, type="tertiary"):
        state.browse_dir = str(here.parent)
        st.rerun(scope="fragment")
    top.markdown(f"`{here}`")
    hidden = top.toggle("Hidden files", key="browse_hidden")
    try:
        entries = files.listing(here, hidden)
    except OSError as e:
        st.error(dev_state.error_text(e), icon=":material/error:")
        return
    event = st.dataframe(entries, hide_index=True, on_select="rerun", selection_mode="single-row",
                         key=f"browse:{here}:{hidden}", height=420, column_config={
                             "size": st.column_config.NumberColumn("Size", format="compact"),
                             "modified": st.column_config.DatetimeColumn("Modified (UTC)", format="YYYY-MM-DD HH:mm")})
    if not event.selection.rows:
        st.caption("Click a directory to enter it, a file to open it.")
        return
    entry = entries.iloc[event.selection.rows[0]]
    target = here / entry["name"].rstrip("/")
    if entry["kind"] == "dir":
        state.browse_dir = str(target)
        st.rerun(scope="fragment")
    state.pop("browse_dir", None)
    state.picked_file = str(target)
    st.rerun()


with st.sidebar:
    st.header(":material/construction: yamlboard dev")
    kind = st.segmented_control("Source", ["Database", "File"], required=True, key="source_kind",
                                default="File" if st.session_state.file else "Database")
    if kind == "Database":
        with st.form("connection", border=False):
            st.text_input(
                "Connection", key="conn", persist_state="session",
                help="A SQLAlchemy URL (`duckdb:///data/w.duckdb`, `postgresql+psycopg://user:${PGPASSWORD}@host/db`), "
                     "a JDBC URL (`jdbc:postgresql://host:5432/db?user=…`, `jdbc:duckdb:/data/w.duckdb`) or a DuckDB "
                     "file path. `${VAR}` is read from the environment.",
            )
            if st.form_submit_button("Connect", icon=":material/link:", type="primary"):
                dev_state.connect(st.session_state.conn)
    else:
        if picked := st.session_state.pop("picked_file", None):  # chosen in the browser, before its widget is drawn
            st.session_state.file_path = picked
            dev_state.open_file(picked, st.session_state.get("file_format", "auto"))
        row = st.container(horizontal=True, vertical_alignment="bottom", gap="small")
        row.text_input("File", key="file_path", placeholder="logs/app.log", persist_state="session",
                       on_change=lambda: dev_state.open_file(st.session_state.file_path,
                                                             st.session_state.file_format),
                       help="A path on this machine, or a glob (`logs/*.log`) to read several files as one. "
                            "`.gz` files are read too. Enter opens it.")
        if row.button(":material/folder_open:", help="Browse this machine's files", type="tertiary"):
            st.session_state.pop("browse_dir", None)  # closed without a pick last time: start again
            browse()
        st.selectbox("Format", ["auto", *(f.value for f in files.Format)], key="file_format", persist_state="session",
                     format_func=lambda f: "From the extension" if f == "auto" else f.upper(),
                     help="A file that is not CSV, JSON or Parquet is a log: one row per line, cut by a pattern.")
        if st.button("Open", icon=":material/description:", type="primary"):
            dev_state.open_file(st.session_state.file_path, st.session_state.file_format)
    if st.session_state.conn_error:
        st.error(st.session_state.conn_error, icon=":material/link_off:")
    if st.session_state.file:
        st.caption(f"Reading `{st.session_state.file['path']}` ({st.session_state.file['format'].value})")
    elif st.session_state.url:
        st.caption(f"Connected to `{dev_state.masked(st.session_state.url)}`")

    uploaded = st.file_uploader("Report YAML", type=["yml", "yaml"], key="upload",
                                help="Opens the file on the Report page; nothing is written back to it.")
    if uploaded is not None and st.session_state.get("uploaded_id") != uploaded.file_id:
        st.session_state.uploaded_id = uploaded.file_id
        dev_state.set_yaml(uploaded.getvalue().decode("utf-8"))
        st.switch_page(dev_state.page("report"))

page.run()
