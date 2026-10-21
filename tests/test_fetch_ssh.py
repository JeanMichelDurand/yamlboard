"""fetch over real SSH: a paramiko server in the test runs the command it is sent with ``sh -c``, as sshd would."""

import socket
import subprocess
import sys
import threading
from datetime import timedelta

import pytest

from yamlboard import cases
from yamlboard import fetch as f

from .test_fetch import lines_of
from .test_pull_script import PY310, day_text, end_of, messages, put

paramiko = pytest.importorskip("paramiko")

PASSWORD = "s3cret"


class Server(paramiko.ServerInterface):
    def __init__(self, client_key):
        self.client_key = client_key

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == PASSWORD else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if key.get_base64() == self.client_key.get_base64() else paramiko.AUTH_FAILED

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=run, args=(channel, command.decode()), daemon=True).start()
        return True


def run(channel, command):
    """``sh -c command``: stdin from the channel until EOF, stdout and stderr back, then the exit status."""
    stdin = b""
    while chunk := channel.recv(1 << 16):
        stdin += chunk
    done = subprocess.run(["sh", "-c", command], input=stdin, capture_output=True, check=False)
    channel.sendall(done.stdout)
    channel.sendall_stderr(done.stderr)
    channel.send_exit_status(done.returncode)
    channel.close()


@pytest.fixture
def sshd(tmp_path, monkeypatch):
    """A server on a free local port; HOME and the agent out of the way, so only what the test gives is used."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    host_key = paramiko.ECDSAKey.generate()
    client_key = paramiko.ECDSAKey.generate()
    key_file = tmp_path / "id_ecdsa"
    client_key.write_private_key_file(str(key_file))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    known = tmp_path / "known_hosts"
    hk = paramiko.HostKeys()
    hk.add(f"[127.0.0.1]:{port}", host_key.get_name(), host_key)
    hk.save(str(known))

    def serve():
        while True:
            try:
                sock, _ = listener.accept()
            except OSError:
                return
            t = paramiko.Transport(sock)
            t.add_server_key(host_key)
            try:
                t.start_server(server=Server(client_key))
            except (paramiko.SSHException, EOFError):
                continue

    threading.Thread(target=serve, daemon=True).start()
    yield {"port": port, "known": str(known), "key": str(key_file)}
    listener.close()


@pytest.fixture
def logdir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    put(d / "app.log.1.gz", day_text(0), end_of(0), gz=True)
    put(d / "app.log", day_text(1), end_of(1))
    return d


def target(sshd, **kw):
    return f.SSHTarget(f"tester@127.0.0.1:{sshd['port']}", known_hosts=sshd["known"],
                       python=PY310 or sys.executable, timeout=10, **kw)


def test_pull_over_ssh_with_a_key_then_refresh_on_the_same_connection(sshd, logdir, tmp_path, monkeypatch):
    monkeypatch.setenv("YAMLBOARD_CASES", str(tmp_path / "cases"))
    runner = f.SSHRunner(target(sshd, key=sshd["key"]))
    case = cases.create("ssh", timedelta(hours=1), {})
    source = f.Source(str(logdir), "app.log*")
    try:
        r = f.pull(case, "vm1", runner, source, f.Window(start="2026-10-01 22:00Z", end="2026-10-02 01:00Z"))
        assert r.error is None and messages(lines_of(r.part)) == ["d0h22", "d0h23", "d1h0", "d1h1"]
        transport = runner.client.get_transport()
        with (logdir / "app.log").open("a") as fh:
            fh.write(day_text(2, range(1)))
        r = f.pull(case, "vm1", runner, source, f.Window())
        assert r.error is None and r.part is None  # past the window's end: read on the VM, and left out
        assert runner.client.get_transport() is transport  # the refresh used the same connection
    finally:
        runner.close()


def test_pull_over_ssh_with_a_password(sshd, logdir, tmp_path):
    runner = f.SSHRunner(target(sshd, password=PASSWORD))
    try:
        summary = f.plan(runner, f.Source(str(logdir), "app.log*"), f.Window(start="2026-10-02 12:00Z"))
    finally:
        runner.close()
    assert {x["name"]: x["action"] for x in summary["files"]} == {"app.log.1.gz": "before", "app.log": "lines"}


def test_an_unknown_host_is_refused(sshd, tmp_path):
    (tmp_path / "empty").write_text("")
    runner = f.SSHRunner(f.SSHTarget(f"127.0.0.1:{sshd['port']}", known_hosts=str(tmp_path / "empty"),
                                     password=PASSWORD, timeout=10))
    with pytest.raises(f.FetchError, match="is not in"):
        runner.start(b"")


def test_a_wrong_password_is_refused(sshd):
    runner = f.SSHRunner(target(sshd, password="wrong"))
    with pytest.raises(f.FetchError, match="authentication failed"):
        runner.start(b"")


def test_a_failure_on_the_vm_comes_back_over_ssh(sshd, tmp_path):
    runner = f.SSHRunner(target(sshd, password=PASSWORD))
    try:
        with pytest.raises(f.FetchError, match="cannot list"):
            f.plan(runner, f.Source(str(tmp_path / "nope")), f.Window())
    finally:
        runner.close()
