"""What the board's pages share: the workspace, the cached queries and how a failed one is shown."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from yamlboard import query as q
from yamlboard.engine import DB_ERRORS, Workspace, columns_for, engine_for, error_text, run
from yamlboard.schema import Report

REPORT_PAGE = "board_pages/report.py"  # for st.switch_page, relative to app.py


def reports_dir() -> Path:
    """Read on every run, not once: ``yamlboard run`` sets it, and the tests change it."""
    return Path(os.environ.get("YAMLBOARD_REPORTS", "reports"))


def dashboards_dir() -> Path:
    return reports_dir() / "dashboards"


@st.cache_resource(ttl=60)
def _workspace(reports: str, datasources: str | None) -> Workspace:
    return Workspace.open(reports, datasources)


def workspace() -> Workspace:
    return _workspace(str(reports_dir()), os.environ.get("YAMLBOARD_DATASOURCES"))


@st.cache_data(ttl=600, max_entries=256, show_spinner=False)
def fetch(url: str, sql: str, binds: dict[str, Any]) -> pd.DataFrame:
    return run(engine_for(url), q.Statement(sql, binds))


@st.cache_data(ttl=600, max_entries=256, show_spinner=False)
def resolved_columns(url: str, definition: str, params: dict[str, Any]) -> dict[str, str]:
    """``definition``, the report as JSON, keys the cache on the report as it is now: the YAML reloads every
    minute, and the dev app's changes with each edit."""
    return columns_for(engine_for(url), Report.model_validate_json(definition), params)


def db_error(e: Exception) -> None:
    """The driver's message and the run in the log, not a traceback quoting the SQL to the report's readers."""
    run_id = getattr(e, "run_id", None)
    st.error(f"The database could not run this report: {error_text(e)}"
             + (f"\n\nRun `{run_id}` in the log." if run_id else ""), icon=":material/error:")


@contextmanager
def shown_errors() -> Iterator[None]:
    """A refused request or a failed query, shown in place of the result."""
    try:
        yield
    except q.NotAllowed as e:
        st.error(str(e), icon=":material/block:")
    except DB_ERRORS as e:
        db_error(e)
