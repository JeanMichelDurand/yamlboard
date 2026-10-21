"""Files as a source: CSV, JSON and Parquet read by DuckDB, and log files cut into columns by a regular expression.

A file source is plain DuckDB SQL on an in-memory database: ``read_csv_auto``, ``read_json_auto`` or
``read_parquet`` on the file, or, for a log, each line matched against a pattern whose named groups become
columns. The SQL holds the file's absolute path (a glob such as ``/var/log/app/*.log`` reads every match), so
a report drafted from it runs on the board too, on a ``duckdb:///:memory:`` datasource, while the file stays
where it is.

Patterns are written in Python's ``(?P<name>…)`` syntax and run by DuckDB, whose engine is RE2: no
lookaround, no backreference. A line that does not match is left out, so a multi-line entry (a stack
trace, a SQL statement) keeps its first line only.
"""

from __future__ import annotations

import glob
import gzip
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import pandas as pd

from yamlboard import cases
from yamlboard.schema import FieldType

MEMORY_URL = "duckdb:///:memory:"
HEAD_LINES = 20
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Format(str, Enum):
    CSV = "csv"
    JSON = "json"
    PARQUET = "parquet"
    LOG = "log"


EXTENSIONS = {".csv": Format.CSV, ".tsv": Format.CSV, ".txt": Format.LOG, ".json": Format.JSON,
              ".jsonl": Format.JSON, ".ndjson": Format.JSON, ".parquet": Format.PARQUET}
COLUMN_TYPES = (FieldType.STRING, FieldType.INTEGER, FieldType.NUMBER, FieldType.TIMESTAMP)


def guess_format(path: str) -> Format:
    """The format from the extension (``.gz`` aside); anything else is a log."""
    name = path.lower().removesuffix(".gz")
    return EXTENSIONS.get(Path(name).suffix, Format.LOG)


def resolve(path: str) -> str:
    """The absolute path (``~`` expanded), once something matches it; a glob stays a glob."""
    path = path.strip()
    if not path:
        raise ValueError("no file given")
    full = str(Path(path).expanduser().absolute())
    cases.check_readable(full)  # an expired case of production logs is not read, even before it is deleted
    if not _matches(full):
        raise FileNotFoundError(f"no file matches {full}")
    if Path(full).is_dir():
        raise IsADirectoryError(f"{full} is a directory: give a file, or a glob such as {full}/*.log")
    return full


def _matches(path: str) -> list[str]:
    """The path itself when it exists (``export[2026].csv`` is a name, not a pattern), else the glob's matches."""
    return [path] if Path(path).exists() else sorted(glob.glob(path))


def listing(directory: str | Path, hidden: bool = False, limit: int = 1000) -> pd.DataFrame:
    """A directory's sub-directories, then its files, by name: ``name``, ``kind`` (dir, a format), ``size``,
    ``modified`` (UTC). Entries it cannot read (a broken link, no permission) are left out.
    """
    rows = []
    for entry in sorted(Path(directory).iterdir(), key=lambda e: e.name.lower()):
        if not hidden and entry.name.startswith("."):
            continue
        try:
            st = entry.stat()
            is_dir = entry.is_dir()
        except OSError:
            continue
        rows.append({"name": entry.name + ("/" if is_dir else ""), "kind": "dir" if is_dir else
                     guess_format(entry.name).value, "size": None if is_dir else st.st_size,
                     "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc)})
    rows.sort(key=lambda r: r["kind"] != "dir")  # stable: directories first, each group by name
    return pd.DataFrame(rows[:limit], columns=["name", "kind", "size", "modified"])


def literal(text: str) -> str:
    """A SQL string literal. DuckDB reads backslashes as written, so a pattern needs no other escaping."""
    return "'" + text.replace("'", "''") + "'"


def reader(path: str, fmt: Format) -> str:
    """The table function reading ``path``: one row per record, or per line (``line``) for a log."""
    p = literal(path)
    if fmt is Format.CSV:
        return f"read_csv_auto({p})"
    if fmt is Format.JSON:
        return f"read_json_auto({p})"
    if fmt is Format.PARQUET:
        return f"read_parquet({p})"
    # One VARCHAR column: no delimiter (a NUL byte), no quote, no escape, so every line comes back whole. Not
    # '\\0': DuckDB reads it as two characters, a backslash and a zero, and splits the lines that hold them.
    return (f"read_csv({p}, columns = {{'line': 'VARCHAR'}}, header = false, delim = chr(0), quote = '', "
            "escape = '', auto_detect = false)")


def select_sql(path: str, fmt: Format) -> str:
    """A starting query on a structured file."""
    return f"SELECT *\nFROM {reader(path, fmt)}"


def head(path: str, n: int = HEAD_LINES) -> list[str]:
    """The first ``n`` lines of the first file matching ``path``, to write a pattern against."""
    found = _matches(path)
    if not found:
        raise FileNotFoundError(f"no file matches {path}")
    first = found[0]
    opener = gzip.open if first.endswith(".gz") else open
    with opener(first, "rt", encoding="utf-8", errors="replace") as f:
        return [line.rstrip("\r\n") for _, line in zip(range(n), f, strict=False)]


# --- log formats ------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    """A named group of the pattern and what it becomes: its type, and for a timestamp its ``strptime`` format.

    A timestamp without a format is cast (ISO 8601, ``,`` or ``.`` before the fraction); a time without an
    offset is read as UTC, the session's time zone.
    """

    name: str
    type: FieldType = FieldType.STRING
    format: str | None = None


@dataclass(frozen=True)
class LogFormat:
    label: str
    pattern: str
    columns: dict[str, Column] = field(default_factory=dict)
    note: str = ""


def _cols(*columns: Column) -> dict[str, Column]:
    return {c.name: c for c in columns}


LOG_FORMATS: dict[str, LogFormat] = {
    "combined": LogFormat(
        "Apache / Nginx (combined)",
        r'^(?P<client>\S+) \S+ (?P<user>\S+) \[(?P<time>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+) '
        r'(?P<protocol>[^"]*)" (?P<status>\d{3}) (?P<bytes>\S+)(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?',
        _cols(Column("time", FieldType.TIMESTAMP, "%d/%b/%Y:%H:%M:%S %z"), Column("status", FieldType.INTEGER),
              Column("bytes", FieldType.INTEGER)),
        "The common log format too: its lines have no referer and no agent.",
    ),
    "syslog5424": LogFormat(
        "Syslog (RFC 5424)",
        r"^<(?P<priority>\d+)>1 (?P<time>\S+) (?P<host>\S+) (?P<app>\S+) (?P<procid>\S+) (?P<msgid>\S+) "
        r"(?P<data>-|\[.*?\]) ?(?P<message>.*)$",
        _cols(Column("priority", FieldType.INTEGER), Column("time", FieldType.TIMESTAMP)),
    ),
    "syslog3164": LogFormat(
        "Syslog (RFC 3164, /var/log/syslog)",
        r"^(?:<(?P<priority>\d+)>)?(?P<time>[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}) (?P<host>\S+) "
        r"(?P<program>[^\s:\[]+)(?:\[(?P<pid>\d+)\])?: (?P<message>.*)$",
        _cols(Column("priority", FieldType.INTEGER), Column("pid", FieldType.INTEGER)),
        "Its time has no year and no time zone, so it stays text.",
    ),
    "python": LogFormat(
        "Python logging (time level logger message)",
        r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (?P<level>[A-Z]+) (?P<logger>\S+) (?P<message>.*)$",
        _cols(Column("time", FieldType.TIMESTAMP)),
        "yamlboard's own log has this format.",
    ),
    "logback": LogFormat(
        "Java Logback / Log4j (time level [thread] logger - message)",
        r"^(?P<time>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+(?P<level>[A-Z]+)\s+"
        r"\[(?P<thread>[^\]]*)\]\s+(?P<logger>\S+)\s+-\s+(?P<message>.*)$",
        _cols(Column("time", FieldType.TIMESTAMP)),
        "Their stack traces follow the entry on lines of their own: keep them with \"Stack traces\".",
    ),
    "iso": LogFormat(
        "ISO time, level, message",
        r"^(?P<time>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+\[?"
        r"(?P<level>[A-Za-z]+)\]?:?\s+(?P<message>.*)$",
        _cols(Column("time", FieldType.TIMESTAMP)),
    ),
}


def groups(pattern: str) -> list[str | None]:
    """The pattern's capturing groups in order: a name, or None for an unnamed group (left out of the result).

    Raises ValueError on an invalid pattern, or one without a named group.
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"invalid pattern: {e}") from None
    by_index = {i: name for name, i in rx.groupindex.items()}
    names = [by_index.get(i) for i in range(1, rx.groups + 1)]
    if not any(names):
        raise ValueError("the pattern has no named group: write (?P<name>…) for each column")
    for name in filter(None, names):
        if not _NAME.match(name) or name.startswith("_g"):
            raise ValueError(f"group name {name!r}: use letters, digits and _, not starting with _g")
    return names


def _cast(c: Column) -> str:
    value = f"NULLIF(m[{literal(c.name)}], '')"  # an optional group that did not match is ''
    if c.type is FieldType.INTEGER:
        return f"TRY_CAST({value} AS BIGINT)"
    if c.type is FieldType.NUMBER:
        return f"TRY_CAST({value} AS DOUBLE)"
    if c.type is FieldType.TIMESTAMP and c.format:
        return f"TRY_STRPTIME({value}, {literal(c.format)})"
    if c.type is FieldType.TIMESTAMP:
        return f"TRY_CAST(REPLACE({value}, ',', '.') AS TIMESTAMPTZ)"
    return value


TRACE = "trace"


def log_sql(path: str, pattern: str, columns: dict[str, Column] | None = None, multiline: bool = False) -> str:
    """One row per matching line of ``path``, one column per named group of ``pattern``.

    ``columns`` types the groups; a group it does not name is text. A value that does not convert
    (a ``-`` for a size, a malformed date) is NULL, not an error.

    ``multiline``: a line the pattern does not match continues the entry above it (a stack trace) and goes in a
    ``trace`` column, its lines joined by newlines, instead of being left out. Lines before the first match are.
    """
    names = groups(pattern)
    if multiline and TRACE in names:
        raise ValueError(f"group name {TRACE!r} is the stack trace's column: rename the group")
    columns = columns or {}
    slots = [n or f"_g{i}" for i, n in enumerate(names, 1)]  # regexp_extract maps names to groups by position
    select = ",\n       ".join(f'{_cast(columns.get(n, Column(n)))} AS "{n}"' for n in names if n)
    rx = literal(pattern)
    keys = ", ".join(literal(s) for s in slots)

    def extract(arg: str) -> str:  # not %-formatting: the pattern may hold a %
        return f"regexp_extract({arg}, {rx}, [{keys}])"

    if not multiline:
        return (f"SELECT {select}\n"
                f"FROM (\n    SELECT {extract('line')} AS m\n"
                f"    FROM {reader(path, Format.LOG)}\n    WHERE regexp_matches(line, {rx})\n) AS parsed")
    # Lines are numbered right on the read, by an OVER () with nothing to reorder them: the files in name order, each
    # line in its file's order (measured on DuckDB 1.5.6, 8 threads: 9 million lines of plain and .gz files, none out
    # of order; a PARTITION BY there does reorder them). An entry is a matching line and the lines after it.
    return (f'SELECT {select},\n       {TRACE}\n'
            f"FROM (\n"
            f"    SELECT entry, {extract('arg_min(line, n)')} AS m,\n"
            f"           string_agg(line, chr(10) ORDER BY n) FILTER (WHERE NOT head) AS {TRACE}\n"
            f"    FROM (\n"
            f"        SELECT line, n, head, sum(CAST(head AS INTEGER)) OVER (ORDER BY n ROWS UNBOUNDED PRECEDING) AS entry\n"
            f"        FROM (\n"
            f"            SELECT line, n, regexp_matches(line, {rx}) AS head\n"
            f"            FROM (SELECT line, row_number() OVER () AS n FROM {reader(path, Format.LOG)}) AS numbered\n"
            f"        ) AS marked\n"
            f"    ) AS grouped\n"
            f"    WHERE entry > 0\n"
            f"    GROUP BY entry\n"
            f") AS parsed\n"
            f"ORDER BY entry")  # a GROUP BY keeps no order: the entries in the files' order


def match_sql(path: str, pattern: str) -> str:
    """How many lines ``path`` has (``lines``) and how many match (``matched``)."""
    return (f"SELECT count(*) AS lines, count(*) FILTER (WHERE regexp_matches(line, {literal(pattern)})) AS matched\n"
            f"FROM {reader(path, Format.LOG)}")


def unmatched_sql(path: str, pattern: str, limit: int = 5) -> str:
    """A few lines the pattern misses, to fix it."""
    return (f"SELECT line\nFROM {reader(path, Format.LOG)}\n"
            f"WHERE NOT regexp_matches(line, {literal(pattern)})\nLIMIT {int(limit)}")
