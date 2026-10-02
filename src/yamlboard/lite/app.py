"""yamlboard lite: open a file or a DuckDB database in the browser, cut a log into columns, test a query, draft
and check a report YAML. The page runs on stlite; nothing leaves the browser.

Built by ``yamlboard lite DIR`` (``build.py``); not run by ``streamlit run``.
"""

from __future__ import annotations

import re
from pathlib import Path

import streamlit as st

from yamlboard import files, ui
from yamlboard.lite import duck
from yamlboard.lite import state as lite_state

DUCKDB = "duckdb"
MAX_UPLOAD_MB = 1024  # the browser holds the file, Python's copy and DuckDB's: beyond, a tab runs out of memory

st.set_page_config(page_title="yamlboard lite", page_icon="assets/yamlboard.png", layout="wide")
ui.logo("assets/yamlboard.svg")
lite_state.init()
state = st.session_state

with st.spinner("Starting DuckDB…"):
    engine_version = await duck.connect()  # noqa: F704, PLE1142 — stlite runs the script in an event loop


def is_duckdb(name: str, data: bytes) -> bool:
    return Path(name).suffix.lower() in (".duckdb", ".db", ".ddb") or data[8:12] == b"DUCK"


STAGING = "_yb_opening"  # a database being checked; an alias from a file name never starts with _


async def close_previous() -> None:
    """Detach the open database, if any, and free the open file. From here on, nothing is open until the new
    source is recorded."""
    prev, state.source = state.source, None
    await duck.execute("USE memory")
    if prev and prev["kind"] == DUCKDB:
        alias = prev["alias"]
        await duck.execute(f'DETACH DATABASE IF EXISTS "{alias}"')
    if prev:
        await duck.unregister(prev["path"])


async def open_source(name: str, data: bytes, fmt: str) -> None:
    """Make an uploaded file the source; on failure keep the previous one and record why.

    The file is checked from a staging copy first, so a file that does not open (a corrupt database, a CSV that
    is not one) leaves the previous source as it was, even one of the same name. Then it takes the previous
    source's place, at ``/data/<its name>``: SQL already written for that path still runs.
    """
    staged = None
    try:
        staged = await duck.register(name, data, duck.STAGING_DIR)
        database = fmt == DUCKDB or (fmt == "auto" and is_duckdb(name, data))
        if database:
            await duck.execute(f'DETACH DATABASE IF EXISTS "{STAGING}"')
            await duck.execute(f'ATTACH {files.literal(staged)} AS "{STAGING}" (READ_ONLY)')
            try:  # ATTACH alone accepts a corrupt file with a SQLite header: reading its tables opens it
                await duck.query(f"SELECT count(*) FROM information_schema.tables WHERE table_catalog = '{STAGING}'")
            finally:
                await duck.execute(f'DETACH DATABASE IF EXISTS "{STAGING}"')
        else:
            kind = files.guess_format(name) if fmt == "auto" else files.Format(fmt)
            if kind is not files.Format.LOG:  # read the header now: a bad file fails here, not on every page
                await duck.query(f"DESCRIBE {files.select_sql(staged, kind)}")
    except Exception as e:  # noqa: BLE001 — a malformed file, a wrong format: shown to the user
        state.open_error = duck.error_text(e)
        return
    finally:
        if staged:
            await duck.unregister(staged)
    await close_previous()
    state.results, state.preview, state.view_check = {}, None, None
    try:
        path = await duck.register(name, data)
        if database:
            alias = re.sub(r"\W+", "_", Path(name).stem).strip("_").lower() or "db"
            await duck.execute(f'DETACH DATABASE IF EXISTS "{alias}"')
            await duck.execute(f'ATTACH {files.literal(path)} AS "{alias}" (READ_ONLY)')
            await duck.execute(f'USE "{alias}"')
            source = {"kind": DUCKDB, "name": name, "path": path, "alias": alias}
        else:
            source = {"kind": "file", "name": name, "path": path, "format": kind}
    except Exception as e:  # noqa: BLE001 — checked above: only a browser out of memory gets here
        state.open_error = duck.error_text(e)
        return
    state.source, state.open_error = source, None

with st.sidebar:
    st.header(":material/construction: yamlboard lite")
    uploaded = st.file_uploader(
        "File", key="source_upload", max_upload_size=MAX_UPLOAD_MB,
        help="CSV, JSON, Parquet, a log (any other text file, `.gz` too) or a DuckDB database. It stays in this "
             "browser tab: nothing is uploaded anywhere.")
    fmt = st.selectbox("Format", ["auto", *(f.value for f in files.Format), DUCKDB], key="source_format",
                       format_func=lambda f: "From the extension" if f == "auto" else
                       "DuckDB database" if f == DUCKDB else f.upper())
    if uploaded is not None and state.get("opened") != (uploaded.file_id, fmt):
        state.opened = (uploaded.file_id, fmt)
        with st.spinner(f"Opening {uploaded.name}…"):
            await open_source(uploaded.name, uploaded.getvalue(), fmt)  # noqa: F704, PLE1142
    if state.open_error:
        st.error(state.open_error, icon=":material/error:")
    src = state.source
    if src:
        st.caption(f"Reading `{src['name']}` ("
                   f"{'DuckDB database' if src['kind'] == DUCKDB else src['format'].value})")
    yaml_file = st.file_uploader("Report YAML", type=["yml", "yaml"], key="yaml_upload",
                                 help="Opens the file on the Report page.")
    if yaml_file is not None and state.get("yaml_id") != yaml_file.file_id:
        state.yaml_id = yaml_file.file_id
        lite_state.set_yaml(yaml_file.getvalue().decode("utf-8"))
        st.switch_page(lite_state.page("report"))
    st.caption(f"Runs in this browser: Streamlit {st.__version__} on Pyodide, DuckDB {engine_version} (WASM).")

src = state.source
first = (st.Page("lite_pages/schema.py", title="Schema", icon=":material/table:", default=True)
         if src and src["kind"] == DUCKDB else
         st.Page("lite_pages/file.py", title="File", icon=":material/description:", default=True))
st.navigation([
    first,
    st.Page("lite_pages/query.py", title="Query", icon=":material/terminal:"),
    st.Page("lite_pages/report.py", title="Report", icon=":material/edit_document:"),
], position="top").run()
