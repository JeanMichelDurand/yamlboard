from tests.conftest import EXAMPLES
from yamlboard.cli import main


def test_validate(capsys):
    assert main(["validate", str(EXAMPLES)]) == 0
    assert "ok     sales" in capsys.readouterr().out


def test_validate_reports_errors(tmp_path, capsys):
    (tmp_path / "bad.yml").write_text("id: x\ntitle: X\nsql: select 1\nfields: [{name: a, grains: [day]}]\n")
    assert main(["validate", str(tmp_path)]) == 1
    assert "grains need a date" in capsys.readouterr().err


def test_sql(capsys):
    assert main(["sql", str(EXAMPLES / "sales.yml"), "-p", "start=2026-01-01", "-p", "end=2026-06-30",
                 "--dialect", "postgresql"]) == 0
    out = capsys.readouterr().out
    assert "date_trunc('week', order_date)" in out and "sum(amount)" in out  # six months: weeks


def test_sql_bad_params(capsys):
    assert main(["sql", str(EXAMPLES / "sales.yml")]) == 1
    assert "is required" in capsys.readouterr().err


def test_datasource_env(tmp_path, monkeypatch):
    import pytest

    from yamlboard.engine import expand_env, load_datasources

    monkeypatch.setenv("YB_PW", "s3cret")
    assert expand_env("postgresql://u:${YB_PW}@h/db") == "postgresql://u:s3cret@h/db"
    with pytest.raises(ValueError, match="YB_MISSING is not set"):
        expand_env("${YB_MISSING}")
    (tmp_path / "ds.yml").write_text("default: duckdb:///:memory:\n")
    with pytest.raises(ValueError, match="quote URLs"):
        load_datasources(tmp_path / "ds.yml")


def test_dev_listens_on_localhost_unless_told(monkeypatch, tmp_path, capsys):
    from yamlboard import cli

    seen = {}
    monkeypatch.setattr(cli, "_call", lambda cmd, env: seen.update(cmd=cmd, env=env) or 0)
    monkeypatch.delenv("STREAMLIT_SERVER_ADDRESS", raising=False)
    assert main(["dev", "--log-dir", str(tmp_path)]) == 0
    assert seen["env"]["STREAMLIT_SERVER_ADDRESS"] == "localhost" and "warning" not in capsys.readouterr().err
    assert main(["dev", "--log-dir", str(tmp_path), "--server.address", "0.0.0.0"]) == 0
    assert "STREAMLIT_SERVER_ADDRESS" not in seen["env"] and "--server.address" in seen["cmd"]
    assert "listens on 0.0.0.0" in capsys.readouterr().err
    assert main(["dev", "--log-dir", str(tmp_path), "--server.address=127.0.0.1"]) == 0
    assert "warning" not in capsys.readouterr().err
