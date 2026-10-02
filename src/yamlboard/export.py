"""Result downloads: CSV, JSON and Excel (``.xlsx``)."""

from __future__ import annotations

import io
from collections.abc import Iterable
from decimal import Decimal

import pandas as pd

from yamlboard import times

FORMATS = ("CSV", "JSON", "XLSX")
_ALIASES = {"EXCEL": "XLSX", "XLS": "XLSX"}
LABELS = {"CSV": "CSV", "JSON": "JSON", "XLSX": "Excel"}
MIME = {"CSV": "text/csv", "JSON": "application/json",
        "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def formats(declared: Iterable[str]) -> list[str]:
    """A report's ``formats`` that yamlboard can write, in a fixed order; all of them when it declares none."""
    wanted = {_ALIASES.get(f.upper(), f.upper()) for f in declared}
    return [f for f in FORMATS if f in wanted] if wanted else list(FORMATS)


def to_xlsx(df: pd.DataFrame, sheet: str = "data") -> bytes:
    """One sheet, no index. Timestamps are written as UTC without a zone: Excel cells have none."""
    naive = {name: col.dt.tz_convert("UTC").dt.tz_localize(None)
             for name, col in df.items() if isinstance(col.dtype, pd.DatetimeTZDtype)}
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        (df.assign(**naive) if naive else df).to_excel(writer, sheet_name=sheet[:31], index=False)
    return buf.getvalue()


def render(df: pd.DataFrame, fmt: str, sheet: str = "data") -> str | bytes:
    """The file content of ``df`` in ``fmt`` (one of ``FORMATS``)."""
    if fmt == "XLSX":
        return to_xlsx(df, sheet)
    df = times.for_export(df)
    return df.to_csv(index=False) if fmt == "CSV" else _decimals_as_numbers(df).to_json(orient="records")


def _decimals_as_numbers(df: pd.DataFrame) -> pd.DataFrame:
    """PostgreSQL's NUMERIC comes back as ``Decimal``, which ``to_json`` writes as a string: write a number."""
    out = {name: col.map(lambda v: float(v) if isinstance(v, Decimal) else v)
           for name, col in df.items() if col.dtype == object and col.map(lambda v: isinstance(v, Decimal)).any()}
    return df.assign(**out) if out else df
