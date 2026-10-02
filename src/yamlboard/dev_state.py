"""Session state shared by the dev app's pages: the source (a database or a file), the SQL under test, the
report YAML.

Widget state is dropped when its page is not shown, so the SQL and the YAML live under plain keys; their
editors take a versioned key, bumped when another page replaces the text.
"""

from __future__ import annotations

from collections.abc import Mapping

import streamlit as st
from sqlalchemy import make_url, text

from yamlboard import files
from yamlboard.discover import to_url
from yamlboard.engine import engine_for, error_text, sql_text
from yamlboard.schema import FieldType


def init(conn: str, yaml_text: str = "") -> None:
    defaults = {
        "conn": conn, "url": None, "conn_error": None, "file": None, "file_path": "", "file_format": "auto",
        "dev_sql": "", "sql_version": 0, "bind_types": {}, "tested_params": {}, "preview": None,
        "yaml_text": yaml_text, "yaml_version": 0,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def connect(conn: str) -> None:
    """Open ``conn`` and run ``SELECT 1``; on failure keep the previous connection and record the error."""
    try:
        url = to_url(conn)
        with engine_for(url).connect() as con:
            con.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001 — any driver, any cause: shown to the developer
        st.session_state.conn_error = error_text(e)
        return
    st.session_state.url, st.session_state.conn_error, st.session_state.file = url, None, None
    st.session_state.preview = None


def open_file(path: str, fmt: str = "auto") -> None:
    """Make a file the source, read by an in-memory DuckDB; on failure keep the previous source and record why.

    ``fmt`` is a ``files.Format`` value, or ``auto`` to guess it from the extension.
    """
    try:
        full = files.resolve(path)
        kind = files.guess_format(full) if fmt == "auto" else files.Format(fmt)
        if kind is not files.Format.LOG:  # read the header now: a bad file fails here, not on every page
            with engine_for(files.MEMORY_URL).connect() as con:
                con.execute(sql_text(f"DESCRIBE {files.select_sql(full, kind)}"))
    except Exception as e:  # noqa: BLE001 — a missing file, a bad format: shown to the developer
        st.session_state.conn_error = error_text(e)
        return
    st.session_state.url, st.session_state.conn_error = files.MEMORY_URL, None
    st.session_state.file = {"path": full, "format": kind}
    st.session_state.preview = None


def masked(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 — never show a password, whatever the URL
        return url.split("@")[-1]


def set_sql(sql: str, bind_types: Mapping[str, FieldType] | None = None) -> None:
    st.session_state.dev_sql = sql
    st.session_state.sql_version += 1
    st.session_state.bind_types.update(bind_types or {})
    st.session_state.preview = None


def set_yaml(yaml_text: str) -> None:
    st.session_state.yaml_text = yaml_text
    st.session_state.yaml_version += 1


def page(name: str) -> str:
    """A page path for ``st.switch_page``, relative to ``dev.py``."""
    return f"dev_pages/{name}.py"
