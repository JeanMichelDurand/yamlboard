"""Discover a database and draft a report: its tables and columns, a test query, then the report YAML.

The dev app (``yamlboard dev``) is built on it; it is usable as a library too. Every statement goes
through ``engine.run``, which rolls back, so nothing here writes to the source. The drafted report is a
starting point: its permissions follow the column types (see ``draft_field``), not what users of the
report should be allowed to do, so review them before publishing the file.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

import pandas as pd
import yaml
from sqlalchemy import Engine

from yamlboard.engine import dialect_of, run
from yamlboard.query import Statement
from yamlboard.schema import FieldType, sql_binds

PREVIEW_ROWS = 100
MAX_PREVIEW_ROWS = 10_000
SYSTEM_SCHEMAS = ("information_schema", "pg_catalog", "pg_toast")


# --- connection -------------------------------------------------------------


def to_url(conn: str) -> str:
    """A SQLAlchemy URL from what a user types: a SQLAlchemy URL, a JDBC URL or a DuckDB file path.

    ``jdbc:postgresql://host:5432/db?user=u&password=p`` -> ``postgresql+psycopg://u:p@host:5432/db``;
    ``jdbc:duckdb:/data/w.duckdb`` -> ``duckdb:////data/w.duckdb?access_mode=read_only`` (``jdbc:duckdb:``
    alone is in memory); ``data/w.duckdb`` -> ``duckdb:///data/w.duckdb?access_mode=read_only``. A DuckDB
    file given this way opens read-only; a SQLAlchemy URL is kept as written. ``${VAR}`` is kept for
    ``engine.expand_env``.
    """
    conn = conn.strip()
    if not conn:
        raise ValueError("empty connection")
    lower = conn.lower()
    if lower.startswith("jdbc:duckdb:"):
        return _duckdb_file(conn[len("jdbc:duckdb:"):])
    if lower.startswith(("jdbc:postgresql:", "jdbc:postgres:")):
        return _jdbc_postgres(conn)
    if lower.startswith("jdbc:"):
        raise ValueError(f"unsupported JDBC URL {conn.split('//')[0]!r}: use jdbc:postgresql or jdbc:duckdb, "
                         "or a SQLAlchemy URL")
    if "://" in conn:
        return conn
    if conn.endswith((".duckdb", ".db", ".ddb")) or conn == ":memory:":
        return _duckdb_file(conn)
    raise ValueError(f"not a connection URL: {conn!r}")


def _duckdb_file(path: str) -> str:
    if path in ("", ":memory:"):
        return "duckdb:///:memory:"
    return f"duckdb:///{path}?access_mode=read_only"


def _jdbc_postgres(conn: str) -> str:
    rest = conn.split(":", 2)[2]                       # //host:port/db?query
    if not rest.startswith("//"):
        raise ValueError(f"expected jdbc:postgresql://host[:port]/database, got {conn!r}")
    location, _, query = rest[2:].partition("?")
    options = dict(parse_qsl(query, keep_blank_values=True))
    user, password = options.pop("user", ""), options.pop("password", "")
    safe = "${}"                                       # keep ${VAR} readable for expand_env
    auth = quote(user, safe=safe) + (":" + quote(password, safe=safe) if password else "")
    url = f"postgresql+psycopg://{auth + '@' if auth else ''}{location}"
    return url + ("?" + urlencode(options) if options else "")


# --- schema -----------------------------------------------------------------


def tables(engine: Engine) -> pd.DataFrame:
    """Every table and view the connection can see: ``schema``, ``table``, ``type`` (table or view)."""
    excluded = ", ".join(f"'{s}'" for s in SYSTEM_SCHEMAS)
    sql = f"""SELECT table_schema AS "schema", table_name AS "table",
       CASE WHEN table_type = 'VIEW' THEN 'view' ELSE 'table' END AS "type"
FROM information_schema.tables
WHERE table_schema NOT IN ({excluded})
ORDER BY 1, 2"""
    return run(engine, Statement(sql, {}))


def columns(engine: Engine, schema: str, table: str) -> pd.DataFrame:
    """A table's columns in order: ``column``, ``type`` (the database's own name), ``nullable``."""
    sql = """SELECT column_name AS "column", data_type AS "type", is_nullable = 'YES' AS "nullable"
FROM information_schema.columns
WHERE table_schema = :schema AND table_name = :table
ORDER BY ordinal_position"""
    return run(engine, Statement(sql, {"schema": schema, "table": table}))


def select_all(engine: Engine, schema: str, table: str) -> str:
    """A starting query on a table."""
    d = dialect_of(engine)
    return f"SELECT *\nFROM {d.quote(schema)}.{d.quote(table)}"


# --- test a query -----------------------------------------------------------


def preview(engine: Engine, sql: str, params: Mapping[str, Any] | None = None,
            limit: int = PREVIEW_ROWS) -> pd.DataFrame:
    """The first ``limit`` rows of ``sql``, its ``:name`` binds taken from ``params``."""
    params = dict(params or {})
    missing = sql_binds(sql) - set(params)
    if missing:
        raise ValueError(f"no value for :{', :'.join(sorted(missing))}")
    if not 0 < limit <= MAX_PREVIEW_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_PREVIEW_ROWS}")
    stmt = Statement(preview_sql(sql, limit, engine), {k: v for k, v in params.items() if k in sql_binds(sql)})
    return run(engine, stmt)


def preview_sql(sql: str, limit: int, engine: Engine) -> str:
    """``sql`` wrapped to return its first ``limit`` rows."""
    base = sql.strip().rstrip(";").rstrip()
    return f"SELECT *\nFROM (\n{base}\n) yb_preview\n{dialect_of(engine).limit(limit)}"


def infer_type(values: pd.Series) -> FieldType:
    """The field type of a result column, from its dtype, else from its first non-empty value.

    A column with no value at all (an empty preview) is a string: run the query on rows first.
    """
    if pd.api.types.is_bool_dtype(values):
        return FieldType.BOOLEAN
    if pd.api.types.is_integer_dtype(values):
        return FieldType.INTEGER
    if pd.api.types.is_float_dtype(values):
        return FieldType.NUMBER
    if pd.api.types.is_datetime64_any_dtype(values):
        return FieldType.TIMESTAMP
    sample = values.dropna()
    if sample.empty:
        return FieldType.STRING
    v = sample.iloc[0]
    if isinstance(v, bool):
        return FieldType.BOOLEAN
    if isinstance(v, int):
        return FieldType.INTEGER
    if isinstance(v, float | Decimal):
        return FieldType.NUMBER
    if isinstance(v, dt.datetime):                     # before date: a datetime is a date too
        return FieldType.TIMESTAMP
    if isinstance(v, dt.date):
        return FieldType.DATE
    return FieldType.STRING


# --- draft a report ---------------------------------------------------------

DATE_GRAINS = ["day", "week", "month", "quarter", "year"]
TIMESTAMP_GRAINS = ["minute", "hour", "day", "week", "month", "quarter", "year"]
NUMBER_MEASURES = ["sum", "avg", "min", "max", "median"]
_ID = re.compile(r"(^|_)id$", re.IGNORECASE)


def _label(name: str) -> str:
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ").strip()
    return words[:1].upper() + words[1:].lower()


def draft_field(name: str, ftype: FieldType) -> dict[str, Any]:
    """What a field of this type usually allows.

    Text and booleans filter and group; dates filter and group by grain; numbers filter and aggregate.
    An integer named ``id`` or ``*_id`` is an identifier: it filters and counts distinct, never sums.
    """
    field: dict[str, Any] = {"name": name, "type": ftype.value, "label": _label(name), "filterable": True}
    if ftype is FieldType.DATE:
        field |= {"groupable_rows": True, "grains": DATE_GRAINS}
    elif ftype is FieldType.TIMESTAMP:
        field |= {"groupable_rows": True, "grains": TIMESTAMP_GRAINS}
    elif ftype.numeric and _ID.search(name):
        field["measures"] = ["count_distinct"]
    elif ftype.numeric:
        field["measures"] = NUMBER_MEASURES
    else:
        field["groupable"] = True
    return field


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "report"


def draft_report(
    sql: str,
    result: pd.DataFrame,
    title: str,
    params: Mapping[str, FieldType] | None = None,
    datasource: str = "default",
    report_id: str | None = None,
) -> dict[str, Any]:
    """A report definition for ``sql``, one field per column of its ``result`` (a preview).

    Every ``:name`` of the SQL becomes a required parameter, typed by ``params`` (string otherwise).
    The view opens on the first date over time, else the first text field, measured by the sum of the
    first number, else a row count.
    """
    params = dict(params or {})
    fields = [draft_field(str(c), infer_type(result[c])) for c in result.columns]
    report: dict[str, Any] = {
        "id": report_id or _slug(title),
        "title": title,
        "description": "",
        "datasource": datasource,
    }
    binds = sorted(sql_binds(sql))
    if binds:
        report["parameters"] = [
            {"name": b, "type": FieldType(params.get(b, FieldType.STRING)).value, "label": _label(b),
             "required": True}
            for b in binds
        ]
    report["sql"] = sql.strip().rstrip(";").rstrip() + "\n"
    report["result_columns"] = fields

    temporal = next((f for f in fields if "grains" in f), None)
    text = next((f for f in fields if f.get("groupable")), None)
    number = next((f for f in fields if "sum" in f.get("measures", ())), None)
    view: dict[str, Any] = {}
    if temporal:
        view |= {"rows": [temporal["name"]], "grains": {temporal["name"]: "month"}}
        if text:
            view["columns"] = [text["name"]]
    elif text:
        view["rows"] = [text["name"]]
    view["measures"] = [{"field": number["name"], "aggregation": "sum"} if number else {"field": "*"}]
    view["chart"] = "line" if temporal else "column"
    report["view"] = view
    return report


class _Dumper(yaml.SafeDumper):
    """Block style for multi-line text (the SQL), flow style for short lists (``[day, week]``)."""


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


def _list(dumper: yaml.SafeDumper, value: list) -> yaml.Node:
    flat = all(not isinstance(v, dict | list) for v in value)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", value, flow_style=flat)


_Dumper.add_representer(str, _str)
_Dumper.add_representer(list, _list)


def to_yaml(report: Mapping[str, Any]) -> str:
    return yaml.dump(dict(report), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)
