import ast
import gzip
import io
import json
import os
import shutil
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yamlboard import pull_script as ps

DAY0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
SCRIPT = Path(ps.__file__)


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.000Z")


def entry(t: datetime, msg: str, level: str = "INFO", trace: int = 0) -> str:
    lines = [f"{stamp(t)} {level} app - {msg}\n"]
    lines += [f"    at com.acme.Frame{i}(Frame.java:{i})\n" for i in range(trace)]
    return "".join(lines)


def day_text(day: int, hours=range(24)) -> str:
    return "".join(entry(DAY0 + timedelta(days=day, hours=h), f"d{day}h{h}") for h in hours)


def put(path: Path, text: str, mtime: datetime, gz: bool = False) -> Path:
    data = text.encode()
    path.write_bytes(gzip.compress(data) if gz else data)
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def end_of(day: int) -> datetime:
    return DAY0 + timedelta(days=day, hours=23, minutes=30)


@pytest.fixture
def rotated(tmp_path) -> Path:
    """Five days, logrotate daily with delaycompress: app.log (day 4), app.log.1 (day 3), app.log.{2,3,4}.gz."""
    put(tmp_path / "app.log.4.gz", day_text(0), end_of(0), gz=True)
    put(tmp_path / "app.log.3.gz", day_text(1), end_of(1), gz=True)
    put(tmp_path / "app.log.2.gz", day_text(2), end_of(2), gz=True)
    put(tmp_path / "app.log.1", day_text(3), end_of(3))
    put(tmp_path / "app.log", day_text(4), end_of(4))
    put(tmp_path / "other.log", day_text(4), end_of(4))
    return tmp_path


def frames(data: bytes) -> list[tuple[bytes, bytes]]:
    out, i = [], 0
    while i < len(data):
        kind, n = data[i:i + 1], struct.unpack(">I", data[i + 1:i + 5])[0]
        out.append((kind, data[i + 5:i + 5 + n]))
        i += 5 + n
    return out


def pull(directory: Path, **params) -> tuple[list[str], dict]:
    out = io.BytesIO()
    summary = ps.pull({"path": str(directory), "pattern": "app.log*", **params}, out)
    payload = b"".join(d for k, d in frames(out.getvalue()) if k in (b"D", b"R"))
    text = gzip.decompress(payload).decode() if payload else ""
    return text.splitlines(), summary


def actions(summary: dict) -> dict[str, str]:
    return {f["name"]: f["action"] for f in summary["files"]}


def messages(lines: list[str]) -> list[str]:
    return [line.rsplit(" - ", 1)[1] for line in lines if " - " in line]


def test_window_reads_only_the_files_it_overlaps(rotated):
    lines, summary = pull(rotated, start="2026-10-03 10:00Z", end="2026-10-03 12:00Z")
    assert messages(lines) == ["d2h10", "d2h11", "d2h12"]
    assert actions(summary) == {"app.log.4.gz": "before", "app.log.3.gz": "before", "app.log.2.gz": "lines",
                                "app.log.1": "after", "app.log": "after"}
    assert summary["format"] == "iso" and not summary["truncated"]


def test_a_file_inside_the_window_is_passed_through(rotated):
    lines, summary = pull(rotated, start="2026-10-02 00:00Z", end="2026-10-04 23:59Z")
    assert actions(summary)["app.log.3.gz"] == "pass" and actions(summary)["app.log.1"] == "pass"
    assert messages(lines) == [f"d{d}h{h}" for d in (1, 2, 3) for h in range(24)]  # members concatenated: one stream


def test_an_open_window_reads_up_to_the_live_file(rotated):
    lines, summary = pull(rotated, last=None, start="2026-10-05 20:00Z")
    assert messages(lines) == ["d4h20", "d4h21", "d4h22", "d4h23"]
    assert summary["window"]["end"] is None


def test_an_entry_keeps_its_stack_trace_and_the_edge_cuts_whole_entries(tmp_path):
    text = (entry(DAY0, "before", trace=2) + entry(DAY0 + timedelta(minutes=1), "boom", "ERROR", trace=3)
            + entry(DAY0 + timedelta(minutes=2), "after", trace=1))
    put(tmp_path / "app.log", text, DAY0 + timedelta(minutes=3))
    lines, _ = pull(tmp_path, start=stamp(DAY0 + timedelta(seconds=30)), end=stamp(DAY0 + timedelta(seconds=90)))
    assert lines[0].endswith("boom") and len(lines) == 4 and all(x.startswith("    at") for x in lines[1:])


def test_grep_keeps_matching_entries_with_their_traces(tmp_path):
    text = "".join(entry(DAY0 + timedelta(minutes=i), f"m{i}", "ERROR" if i % 3 == 0 else "INFO", trace=1)
                   for i in range(9))
    put(tmp_path / "app.log", text, DAY0 + timedelta(hours=1))
    lines, summary = pull(tmp_path, start="2026-09-30", grep=r" ERROR ")
    assert messages(lines) == ["m0", "m3", "m6"] and len(lines) == 6
    assert actions(summary)["app.log"] == "lines"  # grep never passes a file through


def test_plan_lists_the_files_and_sends_nothing(rotated):
    out = io.BytesIO()
    summary = ps.pull({"path": str(rotated), "pattern": "app.log*", "start": "2026-10-03 10:00Z", "plan": True}, out)
    assert out.getvalue() == b""
    assert actions(summary)["app.log.2.gz"] == "lines" and actions(summary)["app.log"] == "pass"


def test_max_bytes_cuts_on_a_member_and_cannot_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "MEMBER", 2000)
    put(tmp_path / "app.log", "".join(day_text(d) for d in range(10)), end_of(9))
    lines, summary = pull(tmp_path, start="2026-09-30", grep="INFO", max_bytes=1000)
    assert summary["truncated"] and summary["state"] is None and 0 < summary["bytes"] <= 1000
    assert messages(lines)[:2] == ["d0h0", "d0h1"]  # a valid gzip stream, from the start


# --- refresh: only what was added, through every kind of rotation --------------------------------------------------


def refresh(directory: Path, first: dict) -> tuple[list[str], dict]:
    return pull(directory, start="2026-09-30", state=first["state"], format=first["format"])


def test_refresh_reads_only_the_new_lines(tmp_path):
    log = put(tmp_path / "app.log", day_text(0, range(3)), end_of(0))
    _, first = pull(tmp_path, start="2026-09-30")
    with log.open("a") as f:
        f.write(day_text(0, range(3, 5)))
    lines, _ = refresh(tmp_path, first)
    assert messages(lines) == ["d0h3", "d0h4"]


def test_a_line_being_written_waits_for_the_next_refresh(tmp_path):
    log = put(tmp_path / "app.log", day_text(0, range(2)) + entry(DAY0 + timedelta(hours=2), "half")[:20], end_of(0))
    lines, first = pull(tmp_path, start="2026-09-30")
    assert messages(lines) == ["d0h0", "d0h1"]
    with log.open("a") as f:
        f.write(entry(DAY0 + timedelta(hours=2), "half")[20:])
    lines, _ = refresh(tmp_path, first)
    assert messages(lines) == ["half"]


@pytest.mark.parametrize("mode", ["create", "delaycompress", "compress", "copytruncate"])
def test_refresh_through_a_rotation_neither_loses_nor_repeats(tmp_path, mode):
    log = put(tmp_path / "app.log", day_text(0, range(3)), end_of(0))
    _, first = pull(tmp_path, start="2026-09-30")
    with log.open("a") as f:  # written after the pull, before the rotation: only in the rotated copy
        f.write(day_text(0, range(3, 5)))
    os.utime(log, (end_of(0).timestamp(), end_of(0).timestamp()))
    old = log.read_text()
    if mode in ("create", "delaycompress"):
        log.rename(tmp_path / "app.log.1")
    elif mode == "compress":
        put(tmp_path / "app.log.1.gz", old, end_of(0), gz=True)
        log.unlink()
    else:
        put(tmp_path / "app.log.1", old, end_of(0))
        log.write_text("")
    put(tmp_path / "app.log", day_text(1, range(2)), end_of(1))
    lines, second = refresh(tmp_path, first)
    assert messages(lines) == ["d0h3", "d0h4", "d1h0", "d1h1"]
    if mode == "delaycompress":  # the next rotation compresses app.log.1: nothing new in it
        (tmp_path / "app.log.1").rename(tmp_path / "app.log.2.gz")
        put(tmp_path / "app.log.2.gz", old, end_of(0), gz=True)
        lines, _ = refresh(tmp_path, second)
        assert lines == []


def test_a_file_being_compressed_is_left_for_the_next_refresh(tmp_path):
    put(tmp_path / "app.log", day_text(0, range(2)), end_of(0))
    (tmp_path / "app.log.1.gz").write_bytes(gzip.compress(b"x" * 5000)[:30])
    lines, summary = pull(tmp_path, start="2026-09-30")
    assert actions(summary)["app.log.1.gz"] in ("unreadable", "cut") and messages(lines) == ["d0h0", "d0h1"]


def test_a_gz_still_being_written_is_never_passed_through(tmp_path):
    full = gzip.compress(day_text(0).encode() * 50)
    put(tmp_path / "app.log.1.gz", "", end_of(0))
    (tmp_path / "app.log.1.gz").write_bytes(full[: len(full) - 40])
    os.utime(tmp_path / "app.log.1.gz", (end_of(0).timestamp(), end_of(0).timestamp()))
    put(tmp_path / "app.log", day_text(1, range(2)), end_of(1))
    lines, summary = pull(tmp_path, start="2026-09-30")
    assert actions(summary)["app.log.1.gz"] == "cut"  # read as lines up to the cut, not passed through broken
    assert messages(lines)[-2:] == ["d1h0", "d1h1"] and messages(lines)[0] == "d0h0"
    assert ps.gzip_complete(tmp_path / "app.log") is False and ps.gzip_complete(_gz(tmp_path)) is True


def _gz(tmp_path):
    p = tmp_path / "two.gz"
    p.write_bytes(gzip.compress(b"a\n") + gzip.compress(b"b\n"))
    return p


# --- timestamps ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("line", "name"), [
    (b"2026-10-08T14:02:11,481+02:00 ERROR x", "iso"),
    (b"[2026-10-08 14:02:11] local.ERROR: x", "iso"),
    (b'10.0.0.1 - - [08/Oct/2026:14:02:11 +0200] "GET / HTTP/1.1" 200 12', "clf"),
    (b'{"level": "error", "@timestamp": "2026-10-08T12:02:11.481Z"}', "json"),
    (b"Oct  8 14:02:11 vm1 sshd[123]: Accepted", "syslog"),
])
def test_formats_are_detected(line, name):
    parser = ps.detect([line] * 3 + [b"    at continuation"], datetime(2026, 10, 9).timestamp())
    assert parser.name == name


def test_offsets_are_honoured():
    utc = datetime(2026, 10, 8, 12, 2, 11, tzinfo=timezone.utc).timestamp()
    assert ps.Iso().parse(b"2026-10-08 14:02:11+02:00 x", 0) == utc
    assert ps.Clf().parse(b'h - - [08/Oct/2026:14:02:11 +0200] "GET /"', 0) == utc
    assert ps.JsonLines().parse(b'{"ts": %d}' % (utc * 1000), 0) == utc
    assert ps.Iso().parse(b"2026-10-08 12:02:11.5Z x", 0) == utc + 0.5


def test_syslog_takes_the_year_before_for_december_in_a_january_file():
    january = datetime(2027, 1, 2).timestamp()
    assert ps.Syslog().parse(b"Dec 31 23:59:00 vm1 x", january) == datetime(2026, 12, 31, 23, 59).timestamp()


def test_a_given_format_wins(tmp_path):
    put(tmp_path / "app.log", "08/10/2026 14:02:11 a\n08/10/2026 15:02:11 b\n", datetime(2026, 10, 9))
    lines, summary = pull(tmp_path, time_format="%d/%m/%Y %H:%M:%S", start="2026-10-08 15:00")
    assert lines == ["08/10/2026 15:02:11 b"] and summary["format"] == "strptime"


def test_no_format_found_is_a_clear_failure(tmp_path):
    put(tmp_path / "app.log", "hello\nworld\n", datetime(2026, 10, 9))
    with pytest.raises(ps.Fail, match="--time-format"):
        pull(tmp_path)


def test_bad_bound_and_missing_files_fail(tmp_path):
    with pytest.raises(ps.Fail, match="not a time"):
        ps.bound("yesterday")
    with pytest.raises(ps.Fail, match="no file matches"):
        pull(tmp_path)


# --- as the VM runs it ----------------------------------------------------------------------------------------------


def test_the_script_is_python_3_10():
    ast.parse(SCRIPT.read_text(), feature_version=(3, 10))


PY310 = shutil.which("python3.10") or next(
    (str(p) for p in Path.home().glob(".local/share/uv/python/cpython-3.10*/bin/python3.10")), None)


@pytest.mark.skipif(PY310 is None, reason="no Python 3.10 here")
def test_runs_on_python_3_10_from_stdin(rotated):
    params = {"path": str(rotated), "pattern": "app.log*", "start": "2026-10-03 10:00Z", "end": "2026-10-03 11:00Z"}
    stdin = SCRIPT.read_text() + f"\nmain({json.dumps(json.dumps(params))})\n"
    done = subprocess.run([PY310, "-"], input=stdin.encode(), capture_output=True, check=True)
    got = frames(done.stdout)
    assert got[-1][0] == b"M" and json.loads(got[-1][1])["python"].startswith("3.10")
    payload = b"".join(d for k, d in got if k == b"D")
    assert messages(gzip.decompress(payload).decode().splitlines()) == ["d2h10", "d2h11"]


@pytest.mark.skipif(PY310 is None, reason="no Python 3.10 here")
def test_a_failure_goes_to_stderr_with_code_2(tmp_path):
    stdin = SCRIPT.read_text() + f"\nmain({json.dumps(json.dumps({'path': str(tmp_path / 'nope')}))})\n"
    done = subprocess.run([PY310, "-"], input=stdin.encode(), capture_output=True, check=False)
    assert done.returncode == 2 and b"cannot list" in done.stderr and done.stdout == b""
