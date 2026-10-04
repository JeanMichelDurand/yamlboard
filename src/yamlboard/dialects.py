"""The few SQL fragments that differ between databases."""

from __future__ import annotations

import re

from yamlboard.schema import Aggregation, Grain

_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Reserved words: a plain name that is one of them is quoted. DuckDB's from ``duckdb_keywords()``, PostgreSQL's
# from its SQL keywords appendix.
_DUCKDB_WORDS = """
all analyse analyze and any array as asc asymmetric both case cast check collate column constraint create default
deferrable desc describe distinct do else end except false fetch for foreign from group having in initially
intersect into lambda lateral leading limit not null offset on only or order pivot pivot_longer pivot_wider placing
primary qualify references returning select show some summarize symmetric table then to trailing true union unique
unpivot using variadic when where window with
"""
_POSTGRES_WORDS = """
all analyse analyze and any array as asc asymmetric authorization between binary both case cast check collate column
constraint create cross current_catalog current_date current_role current_schema current_time current_timestamp
current_user default deferrable desc distinct do else end except false fetch for foreign freeze from full grant
group having ilike in initially inner intersect into is isnull join lateral leading left like limit localtime
localtimestamp natural new not notnull null of off offset old on only or order outer over overlaps placing primary
references returning right select session_user similar some symmetric table then to trailing true union unique user
using variadic verbose when where window with
"""


class Dialect:
    name = "ansi"
    reserved = frozenset(_DUCKDB_WORDS.split()) | frozenset(_POSTGRES_WORDS.split())

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def ref(self, ident: str) -> str:
        """A reference to a column of the report's SQL.

        Plain names stay unquoted so they case-fold like the report's own unquoted aliases
        (``AS dateDebut`` is ``datedebut`` on PostgreSQL). A reserved word is quoted as written: the report's SQL
        had to quote it too.
        """
        return ident if _PLAIN.match(ident) and ident.lower() not in self.reserved else self.quote(ident)

    def trunc(self, grain: Grain, expr: str) -> str:
        return f"date_trunc('{grain.value}', {expr})"

    def aggregate(self, agg: Aggregation, expr: str) -> str:
        if agg is Aggregation.COUNT:
            return f"count({expr})"
        if agg is Aggregation.COUNT_DISTINCT:
            return f"count(DISTINCT {expr})"
        if agg is Aggregation.MEDIAN:
            return self.median(expr)
        return f"{agg.value}({expr})"

    def median(self, expr: str) -> str:
        return f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {expr})"

    def utc_session(self) -> str:
        """Run on every new connection: the session computes in UTC (``times``)."""
        return "SET TimeZone = 'UTC'"

    def limit(self, limit: int, offset: int = 0) -> str:
        return f"LIMIT {int(limit)}" + (f" OFFSET {int(offset)}" if offset else "")


class DuckDB(Dialect):
    name = "duckdb"
    reserved = frozenset(_DUCKDB_WORDS.split())

    def median(self, expr: str) -> str:
        return f"median({expr})"


class Postgres(Dialect):
    name = "postgresql"
    reserved = frozenset(_POSTGRES_WORDS.split())


DIALECTS: dict[str, Dialect] = {d.name: d for d in (DuckDB(), Postgres())}


def for_name(name: str) -> Dialect:
    """The dialect for a SQLAlchemy dialect name (``engine.dialect.name``)."""
    try:
        return DIALECTS[name]
    except KeyError:
        raise ValueError(f"unsupported database {name!r}; supported: {', '.join(sorted(DIALECTS))}") from None
