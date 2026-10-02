"""Session state shared by the lite app's pages, as ``dev_state`` holds it for the dev app."""

from __future__ import annotations

import fnmatch
import glob
import re
from collections.abc import Mapping
from pathlib import PurePosixPath

import streamlit as st

from yamlboard.dialects import for_name
from yamlboard.files import literal
from yamlboard.schema import FieldType


def init() -> None:
    defaults = {
        "source": None, "open_error": None, "results": {},
        "dev_sql": "", "sql_version": 0, "bind_types": {}, "tested_params": {}, "preview": None,
        "yaml_text": "", "yaml_version": 0, "view_check": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def set_sql(sql: str, bind_types: Mapping[str, FieldType] | None = None) -> None:
    st.session_state.dev_sql = sql
    st.session_state.sql_version += 1
    st.session_state.bind_types.update(bind_types or {})
    st.session_state.preview = None


def set_yaml(yaml_text: str) -> None:
    st.session_state.yaml_text = yaml_text
    st.session_state.yaml_version += 1
    st.session_state.view_check = None


def preview_sql(sql: str, limit: int) -> str:
    """The first ``limit`` rows of ``sql``, as ``discover.preview_sql`` writes it for DuckDB."""
    base = sql.strip().rstrip(";").rstrip()
    return f"SELECT *\nFROM (\n{base}\n) yb_preview\n{for_name('duckdb').limit(limit)}"


_STRING = re.compile(r"'(?:[^']|'')*'")


def localize(sql: str, name: str, path: str) -> str:
    """``sql`` with each string literal naming the opened file (``/var/log/app.log``, or a glob such as
    ``/var/log/*.log`` that ``name`` matches, with its directory) pointed at its copy in the browser, ``path``.

    A report holds the file's path on the user's machine; this page holds a copy, under another path.
    """

    def sub(m: re.Match[str]) -> str:
        text = m.group()[1:-1].replace("''", "'")
        posix = text.replace("\\", "/")
        base = PurePosixPath(posix).name
        if base == name or ("/" in posix and glob.has_magic(base) and fnmatch.fnmatchcase(name, base)):
            return literal(path)
        return m.group()

    return _STRING.sub(sub, sql)


def page(name: str) -> str:
    """A page path for ``st.switch_page``, relative to the entry script."""
    return f"lite_pages/{name}.py"


async def cached(sql: str) -> object:
    """``duck.query(sql)``, kept for the session: stlite cannot cache an async function with ``st.cache_data``.
    The files never change once opened, so a result stays right; the oldest go past 32."""
    from yamlboard.lite import duck

    results: dict = st.session_state.results
    if sql not in results:
        results[sql] = await duck.query(sql)
        while len(results) > 32:
            results.pop(next(iter(results)))
    return results[sql]
