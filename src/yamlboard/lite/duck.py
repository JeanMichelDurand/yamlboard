"""DuckDB-WASM from Python, in the browser: open files, run SQL, get a DataFrame back.

stlite's Python has neither Python's DuckDB nor a real pyarrow, so a result travels as plain JS arrays. DuckDB
first turns the columns Arrow's JS arrays would garble into ones they carry well (``portable``): a timestamp
into its epoch microseconds, a decimal into a double, a list or a struct into text; ``frame`` turns them back.
A statement's ``:name`` binds are written into the SQL as typed literals (``inline``): it runs on the user's
own copy of their own file, in their own browser.

Only ``connect``, ``register`` and ``query`` touch the browser; the rest is plain Python, tested on DuckDB.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from yamlboard import times
from yamlboard.files import literal
from yamlboard.schema import _BIND, _LITERALS

DUCKDB_WASM = "1.32.0"  # its engine is DuckDB 1.4.3
DATA_DIR = "/data"  # where opened files live, for DuckDB and for Python (files.head)
STAGING_DIR = "/staging"  # an uploaded file until it is known to open: the open source keeps its path meanwhile

# Loads DuckDB-WASM in stlite's worker; DuckDB starts its own worker. ``run`` returns column arrays.
BOOT_JS = """return (async (version) => {
  const duckdb = await import(`https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@${version}/+esm`);
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const url = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], {type: "text/javascript"}));
  const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING), new Worker(url));
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  const conn = await db.connect();
  await conn.query("SET TimeZone = 'UTC'");
  self.yamlboard_duckdb = {
    db, conn,
    async run(sql) {
      const t = await conn.query(sql);
      return {names: t.schema.fields.map(f => f.name), cols: t.schema.fields.map((f, i) => Array.from(t.getChildAt(i)))};
    },
  };
  return await db.getVersion();
})(arguments[0])"""

TIMESTAMP, DATE, NUMBER, PLAIN, TEXT = "timestamp", "date", "number", "plain", "text"
_PLAIN = {"BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
          "FLOAT", "DOUBLE", "VARCHAR"}
_NUMBER = re.compile(r"^(DECIMAL|NUMERIC|HUGEINT|UHUGEINT|VARINT|BIGNUM)\b")


def kind(duckdb_type: str) -> str:
    """How a column of this DuckDB type travels: as is, or converted (see the module docstring)."""
    t = duckdb_type.upper()
    if t.startswith("TIMESTAMP"):
        return TIMESTAMP
    if t == "DATE":
        return DATE
    if _NUMBER.match(t):
        return NUMBER
    return PLAIN if t in _PLAIN else TEXT


def portable(sql: str, types: Sequence[str]) -> str:
    """``sql`` with each column turned into what a JS array carries well; columns are renamed ``c0``, ``c1``…
    so that duplicate names survive."""
    cols = [f"c{i}" for i in range(len(types))]
    select = []
    for c, t in zip(cols, types, strict=True):
        k = kind(t)
        select.append(f"epoch_us({c}) AS {c}" if k == TIMESTAMP else f"CAST({c} AS DOUBLE) AS {c}" if k == NUMBER
                      else f"CAST({c} AS VARCHAR) AS {c}" if k in (DATE, TEXT) else c)
    return f"SELECT {', '.join(select)}\nFROM (\n{sql}\n) AS yb_lite({', '.join(cols)})"


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:  # infinity, a year beyond 9999
        return None


def frame(names: Sequence[str], types: Sequence[str], cols: Sequence[Sequence[Any]]) -> pd.DataFrame:
    """The DataFrame of a ``portable`` result, typed as the dev app's engine returns it: timestamps in UTC."""
    series = []
    for t, values in zip(types, cols, strict=True):
        k = kind(t)
        if k == TIMESTAMP:
            series.append(pd.to_datetime(pd.Series(values, dtype="Int64"), unit="us", utc=True))
        elif k == DATE:
            series.append(pd.Series([_date(v) for v in values], dtype=object))
        else:
            series.append(pd.Series(values, dtype=object if not values else None))
    df = pd.concat(series, axis=1) if series else pd.DataFrame()
    df.columns = list(names)
    return times.to_utc(df)


def sql_value(v: Any) -> str:
    """A bind value as a typed DuckDB literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int | Decimal | float):
        if isinstance(v, float) and not math.isfinite(v):
            return f"CAST('{v}' AS DOUBLE)"
        text = repr(v) if isinstance(v, float) else str(v)
        return f"({text})" if text.startswith("-") else text  # `10 -:n` with -5 is `10 --5`, a comment
    if isinstance(v, datetime):
        ts = pd.Timestamp(v)
        return (f"TIMESTAMPTZ '{ts.tz_convert('UTC').isoformat()}'" if ts.tzinfo else f"TIMESTAMP '{ts.isoformat()}'")
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'"
    if isinstance(v, time):
        return f"TIME '{v.isoformat()}'"
    return literal(str(v))


def inline(sql: str, binds: Mapping[str, Any]) -> str:
    """``sql`` with each ``:name`` outside strings and comments replaced by its value; a missing one is an error."""

    def sub(m: re.Match[str]) -> str:
        if m.group(1) not in binds:
            raise ValueError(f"no value for :{m.group(1)}")
        return sql_value(binds[m.group(1)])

    out, last = [], 0
    for lit in _LITERALS.finditer(sql):
        out += [_BIND.sub(sub, sql[last:lit.start()]), lit.group()]
        last = lit.end()
    return "".join([*out, _BIND.sub(sub, sql[last:])])


def error_text(e: BaseException) -> str:
    """DuckDB's message, without the JS wrapping (``Error: …``)."""
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return text.removeprefix("Error: ")


# --- the browser side ---------------------------------------------------------


def _bridge() -> Any:
    import js  # Pyodide only

    return getattr(js.self, "yamlboard_duckdb", None)


async def connect() -> str:
    """Start DuckDB-WASM once per page; its version."""
    import js

    if _bridge() is None:
        return await js.Function.new(BOOT_JS)(DUCKDB_WASM)
    return await _bridge().db.getVersion()


async def register(name: str, data: bytes, directory: str = DATA_DIR) -> str:
    """Make an opened file readable by DuckDB and by Python, at the same path; the path."""
    from pyodide.ffi import to_js

    path = f"{directory}/{Path(name).name}"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
    await _bridge().db.registerFileBuffer(path, to_js(data))
    return path


async def unregister(path: str) -> None:
    """Free a file ``register`` made: the browser holds each one in memory. Best effort: it only frees memory."""
    try:
        await _bridge().db.dropFile(path)
    except Exception:  # noqa: BLE001, S110 — already dropped, or still in use: it stays in memory
        pass
    Path(path).unlink(missing_ok=True)


async def _run(sql: str) -> tuple[list[str], list[list[Any]]]:
    res = (await _bridge().run(sql)).to_py()
    return res["names"], res["cols"]


async def execute(sql: str) -> None:
    """A statement whose result is not wanted (ATTACH, USE)."""
    await _bridge().conn.query(sql)


async def query(sql: str, binds: Mapping[str, Any] | None = None) -> pd.DataFrame:
    """Run ``sql`` with its ``:name`` binds. A statement DESCRIBE cannot type (SHOW, PRAGMA) comes back as is."""
    sql = inline(sql.strip().rstrip(";").rstrip(), binds or {})
    try:
        names, cols = await _run(f"DESCRIBE {sql}")
    except Exception:  # noqa: BLE001 — not a query: run it as written
        names, cols = await _run(sql)
        return pd.DataFrame(dict(zip(names, cols, strict=True)))
    described = dict(zip(names, cols, strict=True))
    types = described["column_type"]
    _, values = await _run(portable(sql, types))
    return frame(described["column_name"], types, values)
