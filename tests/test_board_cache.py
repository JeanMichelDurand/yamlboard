import pytest

pytest.importorskip("streamlit")

from yamlboard import board


def test_cache_ttl_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("YAMLBOARD_CACHE_TTL", raising=False)
    assert board.cache_ttl() == 600 and board.cache_ttl(60) == 60
    monkeypatch.setenv("YAMLBOARD_CACHE_TTL", "0")
    assert board.cache_key() is None
    monkeypatch.setenv("YAMLBOARD_CACHE_TTL", "999999")
    assert board.cache_ttl() == board.MAX_CACHE_TTL


def test_the_slot_moves_with_time_and_refreshes(monkeypatch):
    monkeypatch.setenv("YAMLBOARD_CACHE_TTL", "30")
    monkeypatch.setattr(board.time, "time", lambda: 95.0)
    state = {}
    monkeypatch.setattr(board.st, "session_state", state)
    assert board.cache_key() == (3, 0)
    board.refresh()
    assert board.cache_key() == (3, 1)
    monkeypatch.setattr(board.time, "time", lambda: 125.0)
    assert board.cache_key() == (4, 1)


def test_cli_passes_the_cache_ttl(monkeypatch, tmp_path):
    from yamlboard import cli

    seen = {}
    monkeypatch.setattr(cli, "_streamlit", lambda app, env, *a: seen.update(env) or 0)
    assert cli.main(["run", str(tmp_path), "--cache-ttl", "15"]) == 0 and seen["YAMLBOARD_CACHE_TTL"] == "15"
    with pytest.raises(SystemExit):
        cli.main(["dev", "--cache-ttl", "-1"])


def test_cli_sets_the_clock(monkeypatch, tmp_path):
    from yamlboard import cli, times

    seen = {}
    monkeypatch.delenv(times.TODAY_VAR, raising=False)
    monkeypatch.setattr(cli, "_streamlit", lambda app, env, *a: seen.update(env) or 0)
    assert cli.main(["run", str(tmp_path), "--today", "2026-08-17"]) == 0 and seen[times.TODAY_VAR] == "2026-08-17"
    seen.clear()
    assert cli.main(["dev", "--today", "2026-08-17"]) == 0 and seen[times.TODAY_VAR] == "2026-08-17"
    seen.clear()
    assert cli.main(["run", str(tmp_path)]) == 0 and times.TODAY_VAR not in seen
    for bad in ("2026-8-17", "2026-02-30", "today"):
        with pytest.raises(SystemExit):
            cli.main(["run", str(tmp_path), "--today", bad])
