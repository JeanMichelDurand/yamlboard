"""Dates and times: ISO-8601 in, UTC inside, ISO-8601 out.

- In: a string must be ISO-8601, ``YYYY-MM-DD`` for a day or ``YYYY-MM-DDTHH:MM[:SS[.ffffff]]`` followed by
  ``Z`` or ``±HH:MM``. A time of day without a zone is rejected: it would mean the server's zone.
- Inside: every timestamp is converted to UTC and handed to the database as a naive UTC value, and every
  session runs with ``TimeZone = 'UTC'`` (``engine.engine_for``), so ``current_date``, ``date_trunc`` and
  ``timestamptz`` columns agree with the values bound. A naive ``datetime`` object is taken as UTC.
- Out: result timestamps are UTC (``to_utc``) and exported as ISO-8601 with ``Z`` (``for_export``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIMESTAMP = re.compile(
    r"(?P<day>\d{4}-\d{2}-\d{2})T(?P<hm>\d{2}:\d{2})(?P<s>:\d{2})?(?:\.(?P<frac>\d{1,6}))?(?P<zone>Z|[+-]\d{2}:\d{2})"
)


class NotISO(ValueError):
    """A string that is not the ISO-8601 date or timestamp expected."""


def today() -> date:
    """Today in UTC, whatever the server's zone."""
    return datetime.now(timezone.utc).date()


def is_day(value: Any) -> bool:
    """A calendar day with no time of day: a ``date``, or a ``YYYY-MM-DD`` string."""
    if isinstance(value, datetime):
        return False
    return isinstance(value, date) or (isinstance(value, str) and _DAY.fullmatch(value.strip()) is not None)


def parse_date(value: Any) -> date:
    """A UTC calendar day from a ``date``, a ``datetime`` (converted to UTC first) or ``YYYY-MM-DD``."""
    if isinstance(value, datetime):
        return _utc(value).date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not _DAY.fullmatch(text):
        raise NotISO(f"{value!r} is not an ISO-8601 date (YYYY-MM-DD)")
    return date.fromisoformat(text)


def parse_timestamp(value: Any) -> datetime:
    """A naive UTC ``datetime``: a day is its midnight UTC; a string with a time needs ``Z`` or an offset."""
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if _DAY.fullmatch(text):
        return datetime.fromisoformat(text)
    m = _TIMESTAMP.fullmatch(text)
    if not m:
        raise NotISO(f"{value!r} is not an ISO-8601 timestamp with a zone (YYYY-MM-DDTHH:MM:SSZ or ±HH:MM)")
    zone = "+00:00" if m["zone"] == "Z" else m["zone"]
    frac = f".{m['frac'].ljust(6, '0')}" if m["frac"] else ""  # Python 3.10 reads 3 or 6 digits only
    return _utc(datetime.fromisoformat(f"{m['day']}T{m['hm']}{m['s'] or ':00'}{frac}{zone}"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def to_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Timestamp columns as UTC: aware ones converted, naive ones (UTC by contract) labelled."""
    out = {}
    for name, col in df.items():
        if isinstance(col.dtype, pd.DatetimeTZDtype):
            out[name] = col.dt.tz_convert("UTC")
        elif pd.api.types.is_datetime64_dtype(col.dtype):
            out[name] = col.dt.tz_localize("UTC")
    return df.assign(**out) if out else df


def iso(value: Any) -> Any:
    """``YYYY-MM-DD`` for a day, ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z`` for a timestamp, anything else unchanged."""
    if value is None or value is pd.NaT or (isinstance(value, float) and pd.isna(value)):
        return value
    if isinstance(value, datetime):
        return _utc(value).isoformat() + "Z"
    if isinstance(value, date):
        return value.isoformat()
    return value


def for_export(df: pd.DataFrame) -> pd.DataFrame:
    """Dates and timestamps as ISO-8601 strings, timestamps in UTC with ``Z``, for CSV and JSON files."""
    out = {}
    for name, col in to_utc(df).items():
        if isinstance(col.dtype, pd.DatetimeTZDtype) or col.dtype == object:
            out[name] = col.map(iso).astype(object).where(col.notna(), None)
    return df.assign(**out) if out else df


_PERIOD_FORMATS = {"minute": "%Y-%m-%d %H:%M", "hour": "%Y-%m-%d %H:00", "day": "%Y-%m-%d", "month": "%Y-%m",
                   "year": "%Y"}


def period_label(values: pd.Series, grain: Any) -> pd.Series:
    """Each value as the period of ``grain`` it starts (UTC): ``2026-03``, ``2026-W10`` (ISO), ``2026-Q1``…

    The labels sort in time order as text, so a chart can put the periods on a band axis, one per bar.
    An empty value is ``(empty)``.
    """
    g = getattr(grain, "value", grain)
    ts = pd.to_datetime(values, utc=True)
    if g == "week":
        iso = ts.dt.isocalendar()
        out = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    elif g == "quarter":
        out = ts.dt.year.astype(str) + "-Q" + ts.dt.quarter.astype(str)
    else:
        out = ts.dt.strftime(_PERIOD_FORMATS[g])
    return out.where(ts.notna(), "(empty)").astype(object)


GRAINS = ("minute", "hour", "day", "week", "month", "quarter", "year")  # finest first, as ``schema.Grain``
# The most periods a default grain gives: a month by day, a year by week, five years by month.
AUTO_PERIODS = 60


def period_index(value: datetime, grain: Any) -> int:
    """The number of the ``grain`` period that holds ``value`` (naive UTC): consecutive periods differ by one."""
    g = getattr(grain, "value", grain)
    if g in ("minute", "hour"):
        seconds = (value - datetime(1970, 1, 1)) // timedelta(seconds=1)
        return seconds // (60 if g == "minute" else 3600)
    day = value.toordinal()
    if g == "day":
        return day
    if g == "week":  # ISO weeks start on Monday
        return (day - value.weekday()) // 7
    if g == "month":
        return value.year * 12 + value.month - 1
    if g == "quarter":
        return value.year * 4 + (value.month - 1) // 3
    if g == "year":
        return value.year
    raise ValueError(f"unknown grain {g!r}")


def period_count(first: datetime, last: datetime, grain: Any) -> int:
    """How many ``grain`` periods the instants from ``first`` to ``last`` (both included) touch."""
    return period_index(last, grain) - period_index(first, grain) + 1


def auto_grain(first: datetime, last: datetime, grains: Iterable[Any]) -> Any:
    """The finest of ``grains`` that cuts ``first``..``last`` into at most ``AUTO_PERIODS`` periods, else the
    coarsest. Returns the item of ``grains`` itself (a ``Grain`` or its name)."""
    ordered = sorted(grains, key=lambda g: GRAINS.index(getattr(g, "value", g)))
    if not ordered:
        raise ValueError("no grain to choose from")
    return next((g for g in ordered if period_count(first, last, g) <= AUTO_PERIODS), ordered[-1])


# Periods ending now: the UI keeps the name and resolves it on every run (``period_bounds``), so a period
# picked an hour ago still ends now. Two dates would go stale.
PERIODS: dict[str, tuple[str, pd.DateOffset]] = {
    "last_hour": ("Last hour", pd.DateOffset(hours=1)),
    "last_day": ("Last day", pd.DateOffset(days=1)),
    "last_7_days": ("Last 7 days", pd.DateOffset(days=7)),
    "last_month": ("Last month", pd.DateOffset(months=1)),
    "last_3_months": ("Last 3 months", pd.DateOffset(months=3)),
    "last_year": ("Last year", pd.DateOffset(years=1)),
}
DAY_PERIODS = tuple(p for p in PERIODS if p != "last_hour")  # a date parameter has no hour


def now() -> datetime:
    """Now in UTC, naive, to the minute: the queries of one minute share their binds, and their cache."""
    return datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)


def period_bounds(name: str, days: bool, at: datetime | None = None) -> tuple[Any, Any]:
    """The start and end of a period ending ``at`` (default: ``now()``).

    ``days``: for date parameters, the start's day to today; else two naive UTC instants.
    """
    end = at or now()
    start = (pd.Timestamp(end) - PERIODS[name][1]).to_pydatetime()
    return (start.date(), end.date()) if days else (start, end)
