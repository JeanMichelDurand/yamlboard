import gzip
import io
import json
import struct
import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest

from yamlboard import cases, files
from yamlboard import fetch as f
from yamlboard.cli import main

from .test_pull_script import DAY0, day_text, end_of, entry, messages, put


@pytest.fixture
def logdir(tmp_path) -> Path:
    d = tmp_path / "var-log-app"
    d.mkdir()
    put(d / "app.log.2.gz", day_text(0), end_of(0), gz=True)
    put(d / "app.log.1", day_text(1), end_of(1))
    put(d / "app.log", day_text(2), end_of(2))
    return d


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "cases"
    monkeypatch.setenv("YAMLBOARD_CASES", str(d))
    return d


def lines_of(path) -> list[str]:
    sql = files.reader(str(path), files.Format.LOG)
    return [r[0] for r in duckdb.sql(f"SELECT line FROM {sql}").fetchall()]


def test_fetch_local_writes_a_case_duckdb_reads(logdir, root, capsys):
    code = main(["fetch", "--local", "--path", str(logdir), "--pattern", "app.log*", "--case", "bug-1",
                 "--from", "2026-10-01 22:00Z", "--to", "2026-10-02 01:00Z"])
    out = capsys.readouterr().out
    assert code == 0, out
    case = root / "bug-1"
    assert messages(lines_of(f"{case}/*/*.log.gz")) == ["d0h22", "d0h23", "d1h0", "d1h1"]
    manifest = cases.load(case)
    host = manifest["hosts"]["local"]
    assert host["format"] == "iso" and host["parts"][0]["file"] == "part-0001.log.gz" and host["state"]
    assert manifest["source"]["pattern"] == "app.log*" and manifest["by"]
    assert cases.expires_at(manifest) - cases.now() <= cases.MAX_TTL
    assert f"yamlboard dev --file '{case}/*/*.log.gz'" in out


def test_fetch_needs_a_window_and_hosts_or_local(logdir, root, capsys):
    assert main(["fetch", "--local", "--path", str(logdir)]) == 2
    assert main(["fetch", "--path", str(logdir), "--all"]) == 2
    assert main(["fetch", "--local", "--path", str(logdir), "--last", "1h", "--to", "2026-10-02", "--follow", "1m"]) == 2
    assert main(["fetch", "--local", "--path", str(logdir), "--all", "--ttl", "25h"]) == 1
    assert "24 h at most" in capsys.readouterr().err
    assert not any(root.glob("*/manifest.json"))


def test_dry_run_lists_and_pulls_nothing(logdir, root, capsys):
    assert main(["fetch", "--local", "--path", str(logdir), "--pattern", "app.log*", "--from", "2026-10-02 12:00Z",
                 "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "before" in out and "app.log.2.gz" in out and "at most" in out and "format iso" in out
    assert not root.exists()


def test_a_failure_on_the_vm_is_reported(tmp_path, root, capsys):
    assert main(["fetch", "--local", "--path", str(tmp_path / "nope"), "--all", "--case", "x"]) == 1
    assert "cannot list" in capsys.readouterr().out


def test_follow_pulls_only_what_was_added_until_the_case_expires(logdir, root):
    case = cases.create("follow", timedelta(hours=1), {})
    runners = {"local": f.LocalRunner()}
    source = f.Source(str(logdir), "app.log*")
    assert f.pull(case, "local", runners["local"], source, f.Window(start="2026-10-03 23:00Z")).part
    added = iter([day_text(3, range(2)), "", day_text(3, range(2, 3))])
    ticks = iter(range(10))

    def sleep(_):
        text = next(added)
        if text:
            with (logdir / "app.log").open("a") as fh:
                fh.write(text)

    seen = []
    f.follow(case, runners, source, 60, sleep=sleep, echo=seen.append,
             clock=lambda: cases.now() + timedelta(minutes=20 * next(ticks)))
    parts = sorted((case / "local").glob("part-*.log.gz"))
    assert [p.name for p in parts] == ["part-0001.log.gz", "part-0002.log.gz", "part-0003.log.gz"]
    assert messages(lines_of(parts[1])) == ["d3h0", "d3h1"] and messages(lines_of(parts[2])) == ["d3h2"]
    assert "local: nothing new" in seen and "expires" in seen[-1]
    assert len(cases.load(case)["hosts"]["local"]["parts"]) == 4  # the empty pull is recorded, with no file


# --- receiving ------------------------------------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, data: bytes, code: int = 0, err: str = ""):
        self.stdout, self.code, self.err, self.killed = io.BytesIO(data), code, err, False

    def finish(self):
        return self.code, self.err

    def kill(self):
        self.killed = True


def frame(kind: bytes, data: bytes) -> bytes:
    return kind + struct.pack(">I", len(data)) + data


def test_a_cut_stream_is_kept_up_to_the_last_whole_member(tmp_path):
    a, b = gzip.compress(b"one\n"), gzip.compress(b"two\n")
    data = frame(b"D", a) + frame(b"R", b[:10]) + frame(b"D", b[10:]) + frame(b"R", b"\x1f\x8b broken")[:9]
    r = f.receive(FakeProcess(data), tmp_path / "p.log.gz")
    assert r.summary is None and "middle of a frame" in r.error
    assert gzip.decompress((tmp_path / "p.log.gz").read_bytes()) == b"one\ntwo\n"


def test_max_bytes_stops_the_pull(tmp_path):
    a = gzip.compress(b"x" * 1000)
    proc = FakeProcess(frame(b"D", a) * 5 + frame(b"M", b"{}"))
    r = f.receive(proc, tmp_path / "p.log.gz", max_bytes=len(a) * 2)
    assert proc.killed and "more than" in r.error and r.summary is None and r.bytes > len(a) * 2


def test_nothing_in_the_window_leaves_no_file(tmp_path):
    r = f.receive(FakeProcess(frame(b"M", json.dumps({"state": None}).encode())), tmp_path / "p.log.gz")
    assert r.part is None and r.error is None and not list(tmp_path.iterdir())


def test_a_slow_pull_times_out(tmp_path):
    class Sleeper(f.LocalRunner):
        def start(self, stdin):
            return super().start(b"import time\ntime.sleep(30)\n")

    r = f.receive(Sleeper().start(b""), tmp_path / "p.log.gz", timeout=0.3)
    assert r.error == "timed out after 0.3 s" and r.part is None


def test_the_payload_is_the_script_and_its_call():
    text = f.payload({"path": "/var/log/it's", "pattern": "a\\b"}).decode()
    call = text.rsplit("\n", 2)[-2]
    assert call.startswith("main(") and json.loads(eval(call[5:-1])) == {"path": "/var/log/it's", "pattern": "a\\b"}


@pytest.mark.parametrize(("text", "seconds"), [("90s", 90), ("5m", 300), ("2h", 7200), ("1d", 86400), ("30", 30)])
def test_durations(text, seconds):
    assert f.duration(text) == seconds


def test_sizes():
    assert f.size("2GB") == 2 << 30 and f.size("500mb") == 500 << 20 and f.size("10") == 10
    with pytest.raises(ValueError):
        f.size("lots")


def test_ssh_refuses_a_python_that_is_not_a_command_name():
    with pytest.raises(f.FetchError, match="--python"):
        f.SSHRunner(f.SSHTarget("vm1", python="python3; rm -rf ~"))


def test_ssh_target_reads_the_spec_and_ssh_config(tmp_path, monkeypatch):
    pytest.importorskip("paramiko")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "config").write_text("Host prod1\n  HostName 10.0.0.7\n  User app\n  Port 2222\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert f.SSHTarget("prod1").resolved()[:3] == ("10.0.0.7", "app", 2222)
    assert f.SSHTarget("ops@prod1:22").resolved()[:3] == ("10.0.0.7", "ops", 22)


# --- cases ----------------------------------------------------------------------------------------------------------


def test_case_ttl_is_capped_and_names_are_checked(root):
    with pytest.raises(cases.CaseError, match="24 h at most"):
        cases.create("a", timedelta(hours=25), {})
    with pytest.raises(cases.CaseError, match="case name"):
        cases.create("../escape", timedelta(hours=1), {})
    cases.create("a", timedelta(hours=1), {})
    with pytest.raises(cases.CaseError, match="exists already"):
        cases.create("a", timedelta(hours=1), {})


def test_expired_cases_are_refused_then_purged(root, capsys):
    d = cases.create("old", timedelta(hours=1), {})
    (d / "local").mkdir()
    put(d / "local" / "part-0001.log.gz", entry(DAY0, "x"), end_of(0), gz=True)
    (root / "not-a-case").mkdir()
    files.resolve(f"{d}/*/*.log.gz")
    later = cases.now() + timedelta(hours=2)
    m = cases.load(d)
    m["expires_at"] = (cases.now() - timedelta(seconds=1)).isoformat()
    cases.save(d, m)
    with pytest.raises(cases.CaseError, match="expired"):
        files.resolve(f"{d}/*/*.log.gz")
    assert cases.purge(at=later) == ["old"] and not d.exists() and (root / "not-a-case").exists()


def test_every_command_purges_expired_cases(root, capsys):
    d = cases.create("gone", timedelta(hours=1), {})
    m = cases.load(d)
    m["expires_at"] = (cases.now() - timedelta(seconds=1)).isoformat()
    cases.save(d, m)
    assert main(["cases"]) == 0
    assert "deleted the expired case gone" in capsys.readouterr().err and not d.exists()


def test_cases_lists_and_deletes(root, capsys):
    cases.create("kept", timedelta(hours=2), {})
    assert main(["cases"]) == 0 and "kept" in capsys.readouterr().out
    assert main(["cases", "--delete", "kept"]) == 0 and not (root / "kept").exists()
    assert main(["cases", "--delete", "kept"]) == 1


def test_local_runner_uses_this_python():
    assert f.LocalRunner().python == sys.executable


def test_two_hosts_of_one_name_are_refused(root, capsys):
    assert main(["fetch", "u@vm-1:2222", "vm-1:2223", "--path", "/var/log", "--all"]) == 2
    assert "vm-1" in capsys.readouterr().err and not root.exists()


def test_a_bad_runner_is_reported_not_raised(root, capsys):
    assert main(["fetch", "vm1", "--python", "python3; rm", "--path", "/var/log", "--all"]) == 1
    assert capsys.readouterr().err.startswith("yamlboard fetch:")


def test_a_used_up_byte_budget_pulls_no_more_hosts(logdir, root, monkeypatch, capsys):
    pulled = []
    real = f.pull

    def pull(case_dir, host, *a, **k):
        pulled.append(host)
        return real(case_dir, host, *a, **k)

    monkeypatch.setattr(f, "SSHRunner", lambda target: f.LocalRunner())   # two "hosts", both this machine
    monkeypatch.setattr(f, "pull", pull)
    monkeypatch.setattr(f, "total_bytes", lambda manifest: 10 if pulled else 0)
    assert main(["fetch", "vm1", "vm2", "--path", str(logdir), "--pattern", "app.log*", "--all", "--case", "b",
                 "--max-bytes", "10"]) == 0
    assert pulled == ["vm1"] and "vm2" in capsys.readouterr().out
