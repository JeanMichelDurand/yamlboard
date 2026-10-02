"""Downloads, the run log and the CLI's log file."""

import io
import logging
import os
import signal
import sys
import threading
import time
from decimal import Decimal

import pandas as pd
import pytest

from yamlboard import cli, export, logs
from yamlboard import query as q
from yamlboard.engine import engine_for, run


def test_formats():
    assert export.formats([]) == ["CSV", "JSON", "XLSX"]
    assert export.formats(["json", "Excel"]) == ["JSON", "XLSX"]
    assert export.formats(["CSV", "PDF"]) == ["CSV"]          # not a format yamlboard writes


def test_xlsx_keeps_types_and_writes_utc():
    df = pd.DataFrame({
        "at": pd.to_datetime(["2026-03-01 10:00"]).tz_localize("UTC"),
        "region": ["north"], "amount": [Decimal("12.50")], "n": [3],
    })
    back = pd.read_excel(io.BytesIO(export.render(df, "XLSX", sheet="detail")), sheet_name="detail")
    assert back.to_dict("records") == [
        {"at": pd.Timestamp("2026-03-01 10:00"), "region": "north", "amount": 12.5, "n": 3}]


def test_csv_and_json_are_iso_utc():
    df = pd.DataFrame({"at": pd.to_datetime(["2026-03-01 10:00"]).tz_localize("UTC")})
    assert "2026-03-01T10:00:00Z" in export.render(df, "CSV")
    assert "2026-03-01T10:00:00Z" in export.render(df, "JSON")


def test_each_run_is_logged_with_its_id_and_sql(caplog):
    engine = engine_for("duckdb:///:memory:")
    with caplog.at_level(logging.INFO, logger="yamlboard"):
        df = run(engine, q.Statement("SELECT :x AS x", {"x": 7}))
    run_id = df.attrs["run_id"]
    start, done = [r.getMessage() for r in caplog.records if run_id in r.getMessage()]
    assert "SELECT :x AS x" in start and "{'x': 7}" in start
    assert done.endswith(" s") and ": 1 rows in" in done


def test_a_failed_run_is_logged(caplog):
    engine = engine_for("duckdb:///:memory:")
    with caplog.at_level(logging.INFO, logger="yamlboard"), pytest.raises(pd.errors.DatabaseError):
        run(engine, q.Statement("SELECT nope", {}))
    assert any(r.levelno == logging.WARNING and "failed" in r.getMessage() for r in caplog.records)


def test_log_dir_option_creates_the_directory(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "_call", lambda cmd, env: seen.update(env) or 0)
    assert cli.main(["dev", "--log-dir", str(tmp_path / "runs")]) == 0
    assert seen["YAMLBOARD_LOG_DIR"] == str(tmp_path / "runs")
    assert (tmp_path / "runs" / "yamlboard-dev.log").exists()


def test_default_log_dir_is_logs_here(monkeypatch, tmp_path):
    monkeypatch.delenv("YAMLBOARD_LOG_DIR")
    monkeypatch.chdir(tmp_path)
    assert logs.log_dir() == tmp_path / "logs"


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_app_does_not_start_without_a_writable_log_dir(monkeypatch, tmp_path, capsys):
    started = []
    monkeypatch.setattr(cli, "_call", lambda cmd, env: started.append(cmd) or 0)
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    assert cli.main(["run", "examples/reports", "--log-dir", str(locked)]) == 2
    assert cli.main(["run", "examples/reports", "--log-dir", str(locked / "sub")]) == 2
    (tmp_path / "file").write_text("")
    assert cli.main(["run", "examples/reports", "--log-dir", str(tmp_path / "file")]) == 2
    assert not started
    err = capsys.readouterr().err
    assert "no write permission" in err and "cannot create" in err and "not starting" in err


def test_theme_is_a_default(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "_call", lambda cmd, env: seen.update(env) or 0)
    cli.main(["dev", "--log-dir", str(tmp_path)])
    assert seen["STREAMLIT_THEME_PRIMARY_COLOR"] == "#174D98"
    monkeypatch.setenv("STREAMLIT_THEME_PRIMARY_COLOR", "#000000")
    cli.main(["dev", "--log-dir", str(tmp_path)])
    assert seen["STREAMLIT_THEME_PRIMARY_COLOR"] == "#000000"


def test_json_writes_postgres_numeric_as_numbers():
    df = pd.DataFrame({"amount": [Decimal("1.5"), None], "name": ["a", "b"]})
    assert export.render(df, "JSON") == '[{"amount":1.5,"name":"a"},{"amount":null,"name":"b"}]'


def test_ctrl_c_and_kill_15_stop_streamlit_not_the_wrapper():
    """Ctrl+C reaches the child itself, so the wrapper ignores it; kill -15 is passed on to the child."""
    def signal_me():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)  # no KeyboardInterrupt in the wrapper
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    before = signal.getsignal(signal.SIGINT)
    threading.Thread(target=signal_me).start()
    assert cli._call([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ)) == 128 + 15
    assert signal.getsignal(signal.SIGINT) is before
