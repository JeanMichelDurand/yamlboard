"""Datasources, and running statements on them."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import Engine, TextClause, create_engine, event, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from yamlboard import times
from yamlboard.dialects import Dialect, for_name
from yamlboard.query import Statement, probe, resolve_columns
from yamlboard.schema import _LITERALS, DATASOURCES_FILE, Report, load_reports

log = logging.getLogger("yamlboard")
# What a failed run raises: SQLAlchemy's errors on connect, pandas' DatabaseError around a failed statement.
DB_ERRORS = (SQLAlchemyError, pd.errors.DatabaseError)
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# What SQLAlchemy's text() reads as a bind (``TextClause._bind_params_regex``), and its escape ``\:name``.
_TEXT_BIND = re.compile(r"(?<![:\w\\]):(\w+)(?!:)")


def sql_text(sql: str) -> TextClause:
    """``sql`` as SQLAlchemy runs it, its ``:name`` binds read as ``schema.sql_binds`` reads them.

    ``text()`` takes ``':a'`` in a string literal or a comment for a bind and fails for want of a value, where
    the report's checks (and the lite's ``inline``) leave it alone: escaped here, it reaches the database as written.
    """
    out, last = [], 0
    for lit in _LITERALS.finditer(sql):
        out += [sql[last:lit.start()], _TEXT_BIND.sub(r"\\:\1", lit.group())]
        last = lit.end()
    return text("".join([*out, sql[last:]]))


def expand_env(url: str) -> str:
    """Replace ``${VAR}`` with the environment variable, so credentials stay out of the file."""

    def sub(m: re.Match[str]) -> str:
        try:
            return os.environ[m.group(1)]
        except KeyError:
            raise ValueError(f"environment variable {m.group(1)} is not set") from None

    return _ENV.sub(sub, url)


def load_datasources(path: str | Path) -> dict[str, str]:
    """``name: sqlalchemy-url`` pairs; ``${VAR}`` is expanded when the engine is created."""
    path = Path(path)
    if not path.exists():
        return {"default": "duckdb:///:memory:"}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: invalid YAML (quote URLs that contain ':'): {e}") from e
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ValueError(f"{path}: expected 'name: url' pairs")
    return data


@lru_cache(maxsize=16)
def engine_for(url: str) -> Engine:
    """An engine whose every connection runs in UTC.

    A DuckDB file is opened per statement and closed after it (no pool): an open connection locks the file
    against every other process, so a pooled one would block a writer for as long as the app runs.
    """
    full = expand_env(url)
    parsed = make_url(full)
    on_file = parsed.get_backend_name() == "duckdb" and parsed.database not in (None, "", ":memory:")
    engine = create_engine(full, **({"poolclass": NullPool} if on_file else {}))
    utc = for_name(engine.dialect.name).utc_session()

    @event.listens_for(engine, "connect")
    def _utc(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(utc)
        finally:
            cursor.close()
        dbapi_connection.commit()  # else run()'s rollback would undo the SET on PostgreSQL

    return engine


def error_text(e: BaseException) -> str:
    """The driver's own message: pandas and SQLAlchemy wrap it in theirs, which quote the whole SQL."""
    while (inner := getattr(e, "orig", None) or e.__cause__) is not None:
        e = inner
    text_ = str(e).strip()
    return text_.splitlines()[0] if text_ else type(e).__name__


def dialect_of(engine: Engine) -> Dialect:
    return for_name(engine.dialect.name)


def run(engine: Engine, statement: Statement) -> pd.DataFrame:
    """Run a statement and roll back: yamlboard never writes to the source. Timestamps come back in UTC.

    Each run gets a UUID, logged with its SQL, binds, row count and time, and kept in the result's
    ``attrs["run_id"]`` (or a failure's ``run_id`` attribute), so a run on screen can be found in the log.
    """
    run_id = str(uuid.uuid4())
    log.info("run %s on %s\n%s\n-- binds: %s", run_id, engine.url.render_as_string(hide_password=True),
             statement.sql, statement.binds)
    start = time.perf_counter()
    try:  # opening the connection can fail too: a missing file, a lock, a refused login
        with engine.connect() as con:
            try:
                df = times.to_utc(pd.read_sql(sql_text(statement.sql), con, params=statement.binds))
            finally:
                con.rollback()
    except Exception as e:
        log.warning("run %s failed after %.3f s: %s", run_id, time.perf_counter() - start, e)
        e.run_id = run_id  # type: ignore[attr-defined] — the log entry to show next to the error
        raise
    log.info("run %s: %d rows in %.3f s", run_id, len(df), time.perf_counter() - start)
    df.attrs["run_id"] = run_id
    return df


def columns_for(engine: Engine, report: Report, params: dict) -> dict[str, str]:
    """Each field's real column name in the report's SQL result (a query that returns no rows)."""
    return resolve_columns(report, list(run(engine, probe(report, dialect_of(engine), params)).columns))


@dataclass
class Workspace:
    """A directory of report files and the datasources they read."""

    reports: dict[str, Report]
    errors: dict[str, str]
    datasources: dict[str, str] = field(default_factory=dict)

    @classmethod
    def open(cls, reports_dir: str | Path, datasources_file: str | Path | None = None) -> Workspace:
        reports, errors = load_reports(reports_dir)
        datasources = load_datasources(datasources_file or Path(reports_dir) / DATASOURCES_FILE)
        for report in reports.values():
            if report.datasource not in datasources:
                errors[report.id] = f"report {report.id!r}: unknown datasource {report.datasource!r}"
        return cls({k: r for k, r in reports.items() if k not in errors}, errors, datasources)

    def url(self, report: Report) -> str:
        return self.datasources[report.datasource]
