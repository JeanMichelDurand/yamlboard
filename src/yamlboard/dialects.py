"""The few SQL fragments that differ between databases."""

from __future__ import annotations

import re

from yamlboard.schema import Aggregation, Grain

_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class Dialect:
    name = "ansi"

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def ref(self, ident: str) -> str:
        """A reference to a column of the report's SQL.

        Plain names stay unquoted so they case-fold like the report's own unquoted aliases
        (``AS dateDebut`` is ``datedebut`` on PostgreSQL).
        """
        return ident if _PLAIN.match(ident) else self.quote(ident)

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

    def median(self, expr: str) -> str:
        return f"median({expr})"


class Postgres(Dialect):
    name = "postgresql"


DIALECTS: dict[str, Dialect] = {d.name: d for d in (DuckDB(), Postgres())}


def for_name(name: str) -> Dialect:
    """The dialect for a SQLAlchemy dialect name (``engine.dialect.name``)."""
    try:
        return DIALECTS[name]
    except KeyError:
        raise ValueError(f"unsupported database {name!r}; supported: {', '.join(sorted(DIALECTS))}") from None
