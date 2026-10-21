"""What the board's pages share: the workspace, the cached queries and how a failed one is shown."""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable, Iterator
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


CACHE_TTL = 600  # seconds a query result is reused, unless --cache-ttl says otherwise
MAX_CACHE_TTL = 86400
REFRESH = "yb:refresh"  # a counter in the session: auto-refresh and the refresh button move it on


def cache_ttl(default: int = CACHE_TTL) -> int:
    """``$YAMLBOARD_CACHE_TTL`` (``--cache-ttl``), read on every run; 0: no cache."""
    raw = os.environ.get("YAMLBOARD_CACHE_TTL")
    return default if raw in (None, "") else min(int(raw), MAX_CACHE_TTL)


def cache_key(default: int = CACHE_TTL) -> tuple[int, int] | None:
    """What keys a cached result besides its query: the TTL's current slot (a result is at most ``ttl`` seconds
    old) and the session's refresh count (a refresh runs the query again). None: no cache."""
    ttl = cache_ttl(default)
    if ttl <= 0:
        return None
    return int(time.time() // ttl), int(st.session_state.get(REFRESH, 0))


def refresh() -> None:
    st.session_state[REFRESH] = int(st.session_state.get(REFRESH, 0)) + 1


def _query(url: str, sql: str, binds: dict[str, Any]) -> pd.DataFrame:
    return run(engine_for(url), q.Statement(sql, binds))


@functools.cache
def cached(fn: Callable[..., Any], ttl: int, max_entries: int = 256, show_spinner: bool | str = False) -> Any:
    """``fn`` under ``st.cache_data`` for ``ttl`` seconds. The TTL is read on every run (``cache_ttl``) and the
    decorator takes it once: so the decorator is applied here, per TTL, and old slots expire with it."""
    return st.cache_data(ttl=ttl, max_entries=max_entries, show_spinner=show_spinner)(fn)


def _fetch(url: str, sql: str, binds: dict[str, Any], slot: tuple[int, int]) -> pd.DataFrame:
    return _query(url, sql, binds)  # `slot` only keys the cache: not `_slot`, which st.cache_data would not hash


def fetch(url: str, sql: str, binds: dict[str, Any]) -> pd.DataFrame:
    slot = cache_key()
    return _query(url, sql, binds) if slot is None else cached(_fetch, cache_ttl())(url, sql, binds, slot)


def _columns(url: str, definition: str, params: dict[str, Any]) -> dict[str, str]:
    return columns_for(engine_for(url), Report.model_validate_json(definition), params)


def _resolved_columns(url: str, definition: str, params: dict[str, Any], slot: tuple[int, int]) -> dict[str, str]:
    return _columns(url, definition, params)


def resolved_columns(url: str, definition: str, params: dict[str, Any]) -> dict[str, str]:
    """``definition``, the report as JSON, keys the cache on the report as it is now: the YAML reloads every
    minute, and the dev app's changes with each edit."""
    slot = cache_key()
    if slot is None:
        return _columns(url, definition, params)
    return cached(_resolved_columns, cache_ttl())(url, definition, params, slot)


def auto_refresh(key: str, every: float | None) -> None:
    """Rerun the whole page every ``every`` seconds, its queries anew. A fragment's timer reruns only the fragment:
    on a timer run (not one the page started, which sets ``key`` first) it moves the refresh count on and reruns the
    page."""
    if not every:
        return
    st.session_state[key] = True

    @st.fragment(run_every=every)
    def tick() -> None:
        if st.session_state.pop(key, False):
            return
        refresh()
        st.rerun(scope="app")

    tick()


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
