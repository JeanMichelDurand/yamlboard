"""``yamlboard fetch``: pull a time window of a rotated log set from VMs over SSH, into a case.

Each VM runs ``pull_script.py`` with its own ``python3``, sent on stdin (nothing installed there, nothing written),
at the lowest CPU and I/O priority. Only the entries in the window travel, gzip-compressed, and only the files that
overlap it are opened. Locally they land in ``<case>/<host>/part-0001.log.gz``, a log file like any other:
``yamlboard dev --file '<case>/*/*.log.gz'``.

``--follow`` keeps the SSH connection open and pulls what was added since, every interval, until the case expires
or Ctrl+C. Each pull is a new part, written aside then renamed, so a query never reads half of one.
"""

from __future__ import annotations

import getpass
import json
import re
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Any, Protocol

from yamlboard import cases

SCRIPT = Path(__file__).with_name("pull_script.py")
# nice and ionice: the VM's work comes first. ionice is tried first: it is not everywhere, and where it is, a
# container may refuse it.
REMOTE = ("if ionice -c3 true >/dev/null 2>&1; then exec nice -n 19 ionice -c3 {python} -; "
          "else exec nice -n 19 {python} -; fi")
_PYTHON = re.compile(r"^[A-Za-z0-9_./+-]+$")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)
UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
SIZES = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


class FetchError(Exception):
    """A pull that failed: the VM's message, a refused connection, a broken stream."""


def duration(text: str) -> float:
    """``90s``, ``5m``, ``2h``, ``1d``; a bare number is seconds."""
    m = _DURATION.match(text)
    if not m:
        raise ValueError(f"not a duration: {text!r} (write 90s, 5m, 2h or 1d)")
    return float(m.group(1)) * UNITS[m.group(2)]


def size(text: str) -> int:
    """``500MB``, ``2GB``, ``2G``; a bare number is bytes."""
    m = _SIZE.match(text)
    if not m:
        raise ValueError(f"not a size: {text!r} (write 500MB or 2GB)")
    return int(float(m.group(1)) * SIZES[m.group(2).lower()])


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def payload(params: dict[str, Any]) -> bytes:
    """The script and the call that runs it: a JSON string literal is a Python one."""
    return (SCRIPT.read_text(encoding="utf-8") + f"\nmain({json.dumps(json.dumps(params))})\n").encode()


# --- where the script runs ------------------------------------------------------------------------------------------


class Process(Protocol):
    stdout: IO[bytes]

    def finish(self) -> tuple[int, str]: ...  # exit code, stderr

    def kill(self) -> None: ...


class Runner(Protocol):
    label: str

    def start(self, stdin: bytes) -> Process: ...

    def close(self) -> None: ...


class _LocalProcess:
    def __init__(self, proc: subprocess.Popen[bytes]):
        self.proc, self.stdout = proc, proc.stdout

    def finish(self) -> tuple[int, str]:
        assert self.proc.stderr is not None
        err = self.proc.stderr.read()  # stdout was read by the receiver; stdin is closed already
        self.proc.stdout.close()
        self.proc.stderr.close()
        return self.proc.wait(), err.decode(errors="replace")

    def kill(self) -> None:
        self.proc.kill()


class LocalRunner:
    """The script on this machine, on a log folder of this machine: the same pull, without SSH."""

    label = "local"

    def __init__(self, python: str = sys.executable):
        self.python = python

    def start(self, stdin: bytes) -> Process:
        proc = subprocess.Popen([self.python, "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        assert proc.stdin is not None
        proc.stdin.write(stdin)  # python - reads its whole program before it runs: no deadlock on stdout
        proc.stdin.close()
        return _LocalProcess(proc)

    def close(self) -> None:
        pass


class _SSHProcess:
    def __init__(self, channel: Any):
        self.channel, self.stdout = channel, channel.makefile("rb")

    def finish(self) -> tuple[int, str]:
        code = self.channel.recv_exit_status()
        err = self.channel.makefile_stderr("rb").read().decode(errors="replace")
        self.channel.close()
        return code, err

    def kill(self) -> None:
        self.channel.close()


@dataclass
class SSHTarget:
    """``[user@]host[:port]`` and how to log in. ``~/.ssh/config`` fills what is not given (HostName, User, Port,
    IdentityFile), as ``ssh`` would."""

    spec: str
    user: str | None = None
    port: int | None = None
    key: str | None = None
    password: str | None = None
    known_hosts: str | None = None
    python: str = "python3"
    timeout: float = 30.0

    def resolved(self) -> tuple[str, str, int, str | None]:
        import paramiko

        spec_user, _, rest = self.spec.rpartition("@")
        alias, _, spec_port = rest.partition(":")
        config_file = Path("~/.ssh/config").expanduser()
        cfg: dict[str, Any] = {}
        if config_file.exists():
            cfg = paramiko.SSHConfig.from_path(str(config_file)).lookup(alias)
        hostname = cfg.get("hostname", alias)
        user = self.user or spec_user or cfg.get("user") or getpass.getuser()
        port = self.port or int(spec_port or cfg.get("port", 22))
        key = self.key or (cfg.get("identityfile") or [None])[0]
        return hostname, user, port, (str(Path(key).expanduser()) if key else None)


class SSHRunner:
    """One SSH connection, opened on first use and kept for ``--follow``: one channel per pull.

    The host's key must already be known (``~/.ssh/known_hosts``, or ``known_hosts``): an unknown host is refused,
    never trusted on first use. A key file, the agent and a password typed in are tried, as given.
    """

    def __init__(self, target: SSHTarget, ask: Callable[[str], str] = getpass.getpass):
        if not _PYTHON.match(target.python):
            raise FetchError(f"--python {target.python!r}: a command name or path, nothing else")
        self.target, self.ask, self.client = target, ask, None
        self.label = target.spec

    def _connect(self) -> Any:
        try:
            import paramiko
        except ImportError:
            raise FetchError("SSH needs paramiko: pip install 'yamlboard[ssh]'") from None
        t = self.target
        hostname, user, port, key = t.resolved()
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known = Path(t.known_hosts or "~/.ssh/known_hosts").expanduser()
        if known.exists():
            client.get_host_keys().load(str(known))  # not load_host_keys: that would write the file back
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        # No SSH compression: what travels is gzip already, compressing it again would only cost the VM CPU.
        kwargs = {"hostname": hostname, "port": port, "username": user, "key_filename": key,
                  "password": t.password, "allow_agent": True, "look_for_keys": key is None and t.password is None,
                  "compress": False, "timeout": t.timeout, "banner_timeout": t.timeout, "auth_timeout": t.timeout}
        try:
            try:
                client.connect(**kwargs)
            except paramiko.PasswordRequiredException:
                client.connect(**kwargs, passphrase=self.ask(f"Passphrase for {key}: "))
        except paramiko.AuthenticationException as e:
            raise FetchError(f"{t.spec}: authentication failed as {user} ({e})") from None
        except paramiko.SSHException as e:
            if "not found in known_hosts" in str(e):
                raise FetchError(f"{t.spec}: {hostname}:{port} is not in {known}. Check its key fingerprint with the "
                                 f"VM's owner, then connect once with ssh to add it") from None
            raise FetchError(f"{t.spec}: {e}") from None
        except OSError as e:
            raise FetchError(f"{t.spec}: cannot connect to {hostname}:{port}: {e}") from None
        return client

    def start(self, stdin: bytes) -> Process:
        if self.client is None:
            self.client = self._connect()
        channel = self.client.get_transport().open_session()
        channel.exec_command(REMOTE.format(python=self.target.python))
        channel.sendall(stdin)
        channel.shutdown_write()
        return _SSHProcess(channel)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


# --- one pull -------------------------------------------------------------------------------------------------------


def frames(stream: IO[bytes]) -> Iterator[tuple[bytes, bytes]]:
    while True:
        header = _read(stream, 5)
        if not header:
            return
        if len(header) < 5:
            raise FetchError("the stream stopped in a frame header")
        n = struct.unpack(">I", header[1:])[0]
        data = _read(stream, n)
        if len(data) < n:
            raise FetchError("the stream stopped in the middle of a frame")
        yield header[:1], data


def _read(stream: IO[bytes], n: int) -> bytes:
    parts, left = [], n
    while left:
        chunk = stream.read(left)
        if not chunk:
            break
        parts.append(chunk)
        left -= len(chunk)
    return b"".join(parts)


@dataclass
class Received:
    summary: dict[str, Any] | None  # None: the pull was cut, what came is kept up to the last whole member
    bytes: int
    part: Path | None  # None: nothing in the window
    error: str | None = None


def receive(proc: Process, part: Path, max_bytes: int | None = None, timeout: float | None = None) -> Received:
    """Write the gzip frames to ``part`` and return the summary. An interrupted pull (timeout, Ctrl+C, a broken
    stream, more than ``max_bytes``) keeps what came, cut back to the last whole member: a valid gzip file."""
    tmp = part.with_name(part.name + ".tmp")
    timed_out = threading.Event()

    def stop() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, stop) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    summary, error, got, clean = None, None, 0, 0
    try:
        with tmp.open("wb") as fh:
            try:
                for kind, data in frames(proc.stdout):
                    if kind in (b"D", b"R"):
                        fh.write(data)
                        got += len(data)
                        if kind == b"D":
                            clean = got
                        if max_bytes is not None and got > max_bytes:
                            error = f"more than {human(max_bytes)} received: stopped"
                            proc.kill()
                            break
                    elif kind == b"M":
                        summary = json.loads(data)
            except (FetchError, OSError) as e:
                error = str(e)
            except KeyboardInterrupt:
                proc.kill()
                error = "interrupted"
                raise
            finally:
                fh.truncate(clean if summary is None else got)
    finally:
        if timer:
            timer.cancel()
        code, err = (0, "") if error == "interrupted" else proc.finish()
        if timed_out.is_set():
            error, summary = f"timed out after {timeout:g} s", None
        elif code != 0 and error is None:
            error = err.strip().splitlines()[-1] if err.strip() else f"exit code {code}"
        if summary is None:
            got = clean
        if got:
            tmp.replace(part)
        else:
            tmp.unlink(missing_ok=True)
    if summary is None and error is None:
        error = "the pull ended without its summary"
    return Received(summary, got, part if got else None, error)


# --- a case ---------------------------------------------------------------------------------------------------------


@dataclass
class Window:
    start: str | float | None = None
    end: str | float | None = None
    last: float | None = None  # seconds before now, on the VM's clock


@dataclass
class Source:
    path: str
    pattern: str = "*"
    grep: str | None = None
    time_format: str | None = None


def params_for(source: Source, window: Window, entry: dict[str, Any] | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"path": source.path, "pattern": source.pattern, "grep": source.grep,
                         "time_format": source.time_format}
    if entry and entry.get("state") is not None:  # a refresh: the first pull's window, as it resolved on the VM
        p.update(start=entry["window"]["start"], end=entry["window"]["end"], state=entry["state"],
                 format=entry["format"])
    else:
        p.update(start=window.start, end=window.end, last=window.last)
    return p


def plan(runner: Runner, source: Source, window: Window) -> dict[str, Any]:
    """What a pull would read, from the VM's listing: nothing is sent but the summary."""
    proc = runner.start(payload({**params_for(source, window), "plan": True}))
    summary, error = None, None
    try:
        for kind, data in frames(proc.stdout):
            if kind == b"M":
                summary = json.loads(data)
    except FetchError as e:
        error = str(e)
    code, err = proc.finish()
    if summary is None:
        raise FetchError(err.strip() or error or f"exit code {code}")
    return summary


def pull(case_dir: Path, host: str, runner: Runner, source: Source, window: Window,
         max_bytes: int | None = None, timeout: float | None = None) -> Received:
    """One pull from ``host`` into a new part of the case; the manifest records it, and the resume state."""
    manifest = cases.load(case_dir)
    folder = cases.check_host_dir(host)
    entry = manifest["hosts"].setdefault(folder, {"spec": host, "parts": [], "state": None})
    if entry["spec"] != host:
        raise FetchError(f"{host}: the case's folder {folder!r} is {entry['spec']}'s already")
    (case_dir / folder).mkdir(exist_ok=True, mode=0o700)
    part = case_dir / folder / f"part-{sum(1 for p in entry['parts'] if p['file']) + 1:04d}.log.gz"
    started, t0 = datetime.now(timezone.utc), time.monotonic()
    received = receive(runner.start(payload({**params_for(source, window, entry), "max_bytes": max_bytes})), part,
                       max_bytes, timeout)
    s = received.summary
    if s is not None:
        entry.update(state=s["state"], format=s["format"], tz=s["tz"], python=s["python"],
                     files=s["files"])
        entry.setdefault("window", s["window"])
    else:
        entry["state"] = None  # what was cut is unknown: no refresh after it
    entry["parts"].append({"file": part.name if received.part else None, "bytes": received.bytes,
                           "at": started.isoformat(), "seconds": round(time.monotonic() - t0, 3),
                           "truncated": bool(s and s["truncated"]), "error": received.error})
    cases.save(case_dir, manifest)
    return received


def total_bytes(manifest: dict[str, Any]) -> int:
    return sum(p["bytes"] for h in manifest["hosts"].values() for p in h["parts"])


def follow(case_dir: Path, runners: dict[str, Runner], source: Source, interval: float,
           max_bytes: int | None = None, timeout: float | None = None, echo: Callable[[str], None] = print,
           sleep: Callable[[float], None] = time.sleep, clock: Callable[[], datetime] = cases.now) -> None:
    """Pull what was added, every ``interval`` seconds, until the case expires, every host stops, or Ctrl+C.
    Pulls never overlap: the next one starts ``interval`` after the previous one began, or right after it ended."""
    next_at = time.monotonic() + interval
    while True:
        manifest = cases.load(case_dir)
        expires = cases.expires_at(manifest)
        live = {h: e for h, e in manifest["hosts"].items() if e.get("state") is not None}
        if not live:
            echo("follow: no host can be refreshed any more (a pull was cut): stopping")
            return
        wait = max(0.0, next_at - time.monotonic())
        if clock() + timedelta(seconds=wait) >= expires:
            echo(f"follow: the case expires at {expires:%H:%M %Z}: stopping")
            return
        sleep(wait)
        next_at = time.monotonic() + interval
        for entry in live.values():
            left = None if max_bytes is None else max_bytes - total_bytes(cases.load(case_dir))
            if left is not None and left <= 0:
                echo(f"follow: {human(max_bytes)} received in all: stopping")
                return
            r = pull(case_dir, entry["spec"], runners[entry["spec"]], source, Window(), left, timeout)
            report(echo, entry["spec"], r)


def report(echo: Callable[[str], None], host: str, r: Received) -> None:
    if r.error:
        echo(f"{host}: {r.error}" + (f" (kept {human(r.bytes)} in {r.part.name})" if r.part else ""))
    elif r.part:
        echo(f"{host}: {human(r.bytes)} → {r.part}")
    else:
        echo(f"{host}: nothing new")


def print_plan(echo: Callable[[str], None], host: str, summary: dict[str, Any]) -> None:
    echo(f"{host}: format {summary['format']}, VM time zone {summary['tz']}, Python {summary['python']}")
    read = 0
    for f in summary["files"]:
        stamp = datetime.fromtimestamp(f["mtime"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        echo(f"  {f['action']:<10} {human(f['size']):>9}  {stamp}Z  {f['name']}")
        if f["action"] in ("pass", "lines"):
            read += f["size"] - f.get("from", 0)
    echo(f"  at most {human(read)} read on the VM (sizes on disk); what travels is the part in the window, compressed")
