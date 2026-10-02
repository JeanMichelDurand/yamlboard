from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from yamlboard import query as q
from yamlboard import times
from yamlboard.engine import engine_for, run
from yamlboard.schema import FieldType


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-03-01", datetime(2026, 3, 1)),
        ("2026-03-01T12:30Z", datetime(2026, 3, 1, 12, 30)),
        ("2026-03-01T12:30:15.5Z", datetime(2026, 3, 1, 12, 30, 15, 500000)),
        ("2026-03-01T01:00:00+02:00", datetime(2026, 2, 28, 23, 0)),
        ("2026-03-01T22:00:00-03:00", datetime(2026, 3, 2, 1, 0)),
    ],
)
def test_timestamps_are_iso_and_end_up_utc(text, expected):
    assert times.parse_timestamp(text) == expected


@pytest.mark.parametrize(
    "text", ["2026-03-01 12:30:00Z", "2026-03-01T12:30:00", "01/03/2026", "2026-3-1", "2026-03-01T12:30:00 UTC", "now"]
)
def test_other_timestamps_are_rejected(text):
    with pytest.raises(times.NotISO):
        times.parse_timestamp(text)


def test_dates():
    assert times.parse_date("2026-03-01") == date(2026, 3, 1)
    # A datetime becomes its UTC day.
    assert times.parse_date(datetime(2026, 3, 1, 23, 30, tzinfo=timezone(timedelta(hours=-2)))) == date(2026, 3, 2)
    for bad in ("2026-03-01T00:00Z", "20260301", "1/3/2026", "2026-03-01garbage"):
        with pytest.raises(times.NotISO):
            times.parse_date(bad)
    with pytest.raises(ValueError):
        q.coerce(FieldType.DATE, "2026-02-30")


def test_parameters_say_iso_utc(ws):
    with pytest.raises(q.InvalidParameters, match=r"From: '01/03/2026' is not a valid date \(ISO-8601, UTC\)"):
        q.coerce_parameters(ws.reports["sales"], {"start": "01/03/2026", "end": "2026-03-02"})


def test_sessions_run_in_utc():
    engine = engine_for("duckdb:///:memory:")
    stmt = q.Statement("select current_setting('TimeZone') as tz, timestamptz '2026-03-01 12:00:00+02' as t", {})
    df = run(engine, stmt)
    assert df["tz"].iloc[0] == "UTC"
    assert str(df["t"].dt.tz) == "UTC" and df["t"].iloc[0] == pd.Timestamp("2026-03-01T10:00Z")
    df = run(engine, q.Statement("select timestamp '2026-03-01 12:00:00' as t", {}))
    assert df["t"].iloc[0] == pd.Timestamp("2026-03-01T12:00Z")  # naive columns are UTC by contract


def test_exports_are_iso_with_z():
    df = pd.DataFrame({"ts": pd.to_datetime(["2026-03-01T12:00:00+02:00", None], utc=True),
                       "d": [date(2026, 3, 1), None], "n": [1, 2]})
    out = times.for_export(df)
    assert out["ts"].tolist() == ["2026-03-01T10:00:00Z", None]
    assert out["d"].tolist() == ["2026-03-01", None] and out["n"].tolist() == [1, 2]
    assert '"ts":"2026-03-01T10:00:00Z"' in out.to_json(orient="records")


def test_utc_survives_the_rollback():
    engine = engine_for("duckdb:///:memory:")
    for _ in range(2):  # the second run reuses the pooled connection that run() rolled back
        assert run(engine, q.Statement("select current_setting('TimeZone') as tz", {}))["tz"].iloc[0] == "UTC"


@pytest.mark.parametrize("first, last, grain, expected", [
    (datetime(2026, 3, 1, 10, 0), datetime(2026, 3, 1, 10, 59, 59), "minute", 60),
    (datetime(2026, 3, 1, 23, 30), datetime(2026, 3, 2, 0, 30), "hour", 2),       # touches two hours
    (datetime(2026, 1, 15), datetime(2026, 3, 15, 23, 59), "month", 3),           # calendar, not length
    (datetime(2026, 3, 1), datetime(2026, 3, 2), "week", 2),                      # Sun 1st, Mon 2nd: two ISO weeks
    (datetime(2025, 12, 31), datetime(2026, 1, 1), "quarter", 2),
    (datetime(2026, 1, 1), datetime(2026, 12, 31), "year", 1),
])
def test_period_count(first, last, grain, expected):
    assert times.period_count(first, last, grain) == expected


ALL = ["minute", "hour", "day", "week", "month", "quarter", "year"]


@pytest.mark.parametrize("days, grains, expected", [
    (0, ALL, "hour"),                       # one day: 24 hours (1440 minutes is too many)
    (7, ALL, "day"),
    (59, ALL, "day"),                       # 60 days, the most periods a default gives
    (366, ALL, "week"),
    (366, ["year", "month", "day"], "month"),   # the finest allowed, in any order
    (5 * 365, ALL, "month"),
    (20 * 365, ALL, "year"),
    (20 * 365, ["day", "week"], "week"),    # none fits: the coarsest allowed
])
def test_auto_grain(days, grains, expected):
    first = datetime(2026, 1, 1)
    last = first + timedelta(days=days + 1, microseconds=-1)
    assert times.auto_grain(first, last, grains) == expected


def test_period_bounds():
    at = datetime(2026, 3, 31, 14, 5)
    assert times.period_bounds("last_hour", False, at) == (datetime(2026, 3, 31, 13, 5), at)
    assert times.period_bounds("last_month", False, at) == (datetime(2026, 2, 28, 14, 5), at)   # calendar month
    assert times.period_bounds("last_year", True, at) == (date(2025, 3, 31), date(2026, 3, 31))
    assert "last_hour" not in times.DAY_PERIODS
    assert times.now().second == 0 and times.now().tzinfo is None
