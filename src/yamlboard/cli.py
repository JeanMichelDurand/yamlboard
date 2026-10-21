"""``yamlboard validate | sql | run | dev | lite | fetch | cases``."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from yamlboard import cases, logs, times
from yamlboard import query as q
from yamlboard.dialects import DIALECTS
from yamlboard.engine import Workspace
from yamlboard.schema import ReportError, load_report

LOG_HELP = ("directory of the run log (run id, SQL, binds, rows, time), rotated; default: logs/ here. "
            "The app does not start if it cannot write there")

# Navy for the selected controls and the main button, instead of Streamlit's red (red means an error here),
# a blue-grey for the sidebar and the inputs. The same colours as the icon (assets/yamlboard.svg).
THEME = {
    "STREAMLIT_THEME_BASE": "light",
    "STREAMLIT_THEME_PRIMARY_COLOR": "#174D98",
    "STREAMLIT_THEME_BACKGROUND_COLOR": "#FFFFFF",
    "STREAMLIT_THEME_SECONDARY_BACKGROUND_COLOR": "#EEF3F8",
    "STREAMLIT_THEME_TEXT_COLOR": "#0B0B0B",
}


def _validate(args: argparse.Namespace) -> int:
    ws = Workspace.open(args.reports, args.datasources)
    for report in ws.reports.values():
        print(f"ok     {report.id}")
    for err in ws.errors.values():
        print(f"error  {err}", file=sys.stderr)
    return 1 if ws.errors else 0


def _sql(args: argparse.Namespace) -> int:
    """Print the SQL of a report's default view: what the app sends before any user choice."""
    try:
        report = load_report(args.report)
    except ReportError as e:
        print(e, file=sys.stderr)
        return 1
    params = dict(p.split("=", 1) for p in args.param)
    try:
        stmt = q.default_view(report, DIALECTS[args.dialect], params)
    except (q.InvalidParameters, q.NotAllowed) as e:
        print(e, file=sys.stderr)
        return 1
    print(stmt.sql)
    print(f"-- binds: {stmt.binds}")
    return 0


def _call(cmd: list[str], env: dict[str, str]) -> int:
    """Run ``cmd`` until it exits. Ctrl+C reaches it directly (same process group), so this process ignores
    it rather than dying with a traceback; ``kill -15`` reaches only this process, so it is passed on.
    Streamlit stops cleanly on either."""
    proc = subprocess.Popen(cmd, env=env)
    previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: proc.terminate())
    try:
        code = proc.wait()
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
    return 128 - code if code < 0 else code  # killed by signal N: the shell's 128 + N


def _streamlit(app: str, env: dict[str, str], extra: list[str], log_dir: str | None, log_name: str) -> int:
    """Start ``app`` once its log directory is writable; exit code 2 without starting it otherwise."""
    script = Path(__file__).with_name(app)
    try:
        d = logs.check_dir(log_dir, log_name)
    except logs.LogDirError as e:
        print(f"yamlboard: {e}: not starting", file=sys.stderr)
        return 2
    env["YAMLBOARD_LOG_DIR"] = str(d)
    for name, value in THEME.items():  # a default: a STREAMLIT_THEME_* variable or a --theme.* option wins
        env.setdefault(name, value)
    return _call([sys.executable, "-m", "streamlit", "run", str(script), *extra], env=env)


def _clock_and_cache(env: dict[str, str], args: argparse.Namespace) -> None:
    if args.cache_ttl is not None:
        env["YAMLBOARD_CACHE_TTL"] = str(args.cache_ttl)
    if args.today is not None:
        env[times.TODAY_VAR] = args.today


def _run(args: argparse.Namespace, extra: list[str]) -> int:
    env = dict(os.environ, YAMLBOARD_REPORTS=str(Path(args.reports).resolve()))
    _clock_and_cache(env, args)
    if args.datasources:
        env["YAMLBOARD_DATASOURCES"] = str(Path(args.datasources).resolve())
    return _streamlit("app.py", env, extra, args.log_dir, "yamlboard.log")


def _day(text: str) -> str:
    if not times.is_day(text):
        raise argparse.ArgumentTypeError("a day, YYYY-MM-DD")
    return times.parse_date(text).isoformat()


def _seconds(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 86400:
        raise argparse.ArgumentTypeError("from 0 (no cache) to 86400 seconds")
    return value


LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _address(env: dict[str, str], extra: list[str]) -> str | None:
    """The address Streamlit is told to listen on: a ``--server.address`` option, else its variable, else none."""
    for i, arg in enumerate(extra):
        if arg.startswith("--server.address="):
            return arg.split("=", 1)[1]
        if arg == "--server.address" and i + 1 < len(extra):
            return extra[i + 1]
    return env.get("STREAMLIT_SERVER_ADDRESS")


def _dev(args: argparse.Namespace, extra: list[str]) -> int:
    """The dev app runs any SQL typed in, opens any file of this machine and reads ``${VAR}`` in a typed URL: it
    listens on this machine only, unless told otherwise."""
    env = dict(os.environ)
    address = _address(env, extra)
    if address is None:
        env["STREAMLIT_SERVER_ADDRESS"] = "localhost"
    elif address not in LOOPBACK:
        print(f"yamlboard: warning: the dev app listens on {address}. Whoever reaches it runs SQL, reads this "
              "machine's files and its environment variables (${VAR} in a URL): keep it on localhost.",
              file=sys.stderr)
    _clock_and_cache(env, args)
    if args.url:
        env["YAMLBOARD_DEV_URL"] = args.url
    if args.file:
        env["YAMLBOARD_DEV_FILE"] = str(Path(args.file).expanduser().absolute())
    if args.yaml:
        env["YAMLBOARD_DEV_YAML"] = str(Path(args.yaml).resolve())
    return _streamlit("dev.py", env, extra, args.log_dir, "yamlboard-dev.log")


def _lite(args: argparse.Namespace) -> int:
    from yamlboard.lite.build import build

    index = build(args.out)
    print(f"wrote {index}; serve its directory (python -m http.server -d {args.out}) or publish it on GitHub Pages")
    return 0


def _fetch(args: argparse.Namespace) -> int:
    from getpass import getpass

    from yamlboard import fetch as f

    if bool(args.hosts) == args.local:
        print("yamlboard fetch: give the hosts to pull from, or --local for a folder of this machine", file=sys.stderr)
        return 2
    if not (args.start or args.last or args.all):
        print("yamlboard fetch: give a window (--from, --last), or --all to read every file", file=sys.stderr)
        return 2
    if args.follow and args.end:
        print("yamlboard fetch: --follow reads up to now: no --to with it", file=sys.stderr)
        return 2
    try:
        window = f.Window(args.start, args.end, f.duration(args.last) if args.last else None)
        max_bytes = f.size(args.max_bytes) if args.max_bytes else None
        timeout = f.duration(args.timeout) if args.timeout else None
        interval = f.duration(args.follow) if args.follow else None
        ttl = timedelta(seconds=f.duration(args.ttl))
    except ValueError as e:
        print(f"yamlboard fetch: {e}", file=sys.stderr)
        return 2
    source = f.Source(args.path, args.pattern, args.grep, args.time_format)
    runners: dict[str, f.Runner] = {}
    try:
        if args.local:
            runners["local"] = f.LocalRunner()
        else:
            folders = [cases.check_host_dir(h) for h in args.hosts]
            twice = sorted({d for d in folders if folders.count(d) > 1})
            if twice:  # a case keeps one folder, and one resume state, per host name
                print(f"yamlboard fetch: one host name, several hosts ({', '.join(twice)}): one case per host",
                      file=sys.stderr)
                return 2
            password = getpass("SSH password: ") if args.password else None  # typed in, never stored or on a command line
            for h in args.hosts:
                runners[h] = f.SSHRunner(f.SSHTarget(h, args.user, args.port, args.key, password, args.known_hosts,
                                                     args.python))
        if args.dry_run:
            for host, runner in runners.items():
                f.print_plan(print, host, f.plan(runner, source, window))
            return 0
        stamp = datetime.now()  # noqa: DTZ005 — a name read by the person who pulled it: their clock
        name = args.case or f"{stamp:%Y%m%d-%H%M}-{cases.check_host_dir(next(iter(runners)))}"
        case_dir = cases.create(name, ttl, {
            "source": {"path": args.path, "pattern": args.pattern, "grep": args.grep, "time_format": args.time_format},
            "window": {"from": args.start, "to": args.end, "last": args.last},
            "limits": {"max_bytes": max_bytes, "timeout": timeout, "follow": interval}})
        print(f"case {case_dir} (expires in {ttl.total_seconds() / 3600:g} h)")
        failed = False
        for host, runner in runners.items():
            left = None if max_bytes is None else max_bytes - f.total_bytes(cases.load(case_dir))
            if left is not None and left <= 0:
                print(f"{f.human(max_bytes)} received in all: {host} and the hosts after it not pulled")
                break
            r = f.pull(case_dir, host, runner, source, window, left, timeout)
            f.report(print, host, r)
            failed |= r.error is not None
        print(f"open it: yamlboard dev --file '{case_dir}/*/*.log.gz'")
        if interval:
            print(f"following every {args.follow}: Ctrl+C to stop")
            f.follow(case_dir, runners, source, interval, max_bytes, timeout)
        return 1 if failed else 0
    except (f.FetchError, cases.CaseError) as e:
        print(f"yamlboard fetch: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
    finally:
        for runner in runners.values():
            runner.close()


def _cases(args: argparse.Namespace) -> int:
    if args.delete:
        d = cases.root() / cases.check_name(args.delete)
        if not (d / cases.MANIFEST).exists():
            print(f"yamlboard cases: no case {args.delete!r} in {cases.root()}", file=sys.stderr)
            return 1
        shutil.rmtree(d)
        print(f"deleted {d}")
        return 0
    found = cases.cases()
    if not found:
        print(f"no case in {cases.root()}")
    for d, m in found:
        if m is None:
            print(f"{d.name:<32} (no manifest: not a case, left alone)")
            continue
        left = cases.expires_at(m) - cases.now()
        hosts = ", ".join(m.get("hosts", {})) or "-"
        print(f"{d.name:<32} {cases.size(d) / 2**20:>9.1f} MB  expires in {left.total_seconds() / 3600:4.1f} h  {hosts}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yamlboard", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="load every report of a directory and report errors")
    p.add_argument("reports", help="directory of report YAML files")
    p.add_argument("--datasources", help="datasources file (default: <reports>/datasources.yml)")

    p = sub.add_parser("sql", help="print the SQL of a report's default view")
    p.add_argument("report", help="report YAML file")
    p.add_argument("-p", "--param", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--dialect", choices=sorted(DIALECTS), default="duckdb")

    p = sub.add_parser("run", help="open the reports in the Streamlit app (extra args go to streamlit)")
    p.add_argument("reports", help="directory of report YAML files")
    p.add_argument("--datasources", help="datasources file (default: <reports>/datasources.yml)")
    p.add_argument("--log-dir", help=LOG_HELP)
    p.add_argument("--cache-ttl", type=_seconds, metavar="SECONDS",
                   help="how long a query result is reused (default 600; 0: never)")
    p.add_argument("--today", type=_day, metavar="DAY",
                   help="set the clock to DAY (YYYY-MM-DD): every period ends on it, for data that stops then (an export)")

    p = sub.add_parser("dev", help="open the dev app: browse a database or a file, test a query, draft a report YAML")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--url", help="connection: SQLAlchemy URL, JDBC URL or DuckDB file (default: in-memory DuckDB)")
    source.add_argument("--file", help="a CSV, JSON, Parquet or log file to open instead (a glob reads several)")
    p.add_argument("--yaml", help="report YAML file to open on the Report page")
    p.add_argument("--log-dir", help=LOG_HELP)
    p.add_argument("--cache-ttl", type=_seconds, metavar="SECONDS",
                   help="how long a query result is reused (default: 600 for reports, 60 for a file; 0: never)")
    p.add_argument("--today", type=_day, metavar="DAY",
                   help="set the clock to DAY (YYYY-MM-DD): every period ends on it, for data that stops then (an export)")

    p = sub.add_parser("lite", help="write yamlboard lite, the dev app's file side as a static site run in the browser")
    p.add_argument("out", help="directory to write the site into (its app/ is replaced)")

    p = sub.add_parser("fetch", help="pull a time window of a rotated log set from VMs over SSH into a case (24 h max)")
    p.add_argument("hosts", nargs="*", metavar="HOST", help="[user@]host[:port], or an alias of ~/.ssh/config")
    p.add_argument("--local", action="store_true", help="a folder of this machine instead of hosts")
    p.add_argument("--path", required=True, help="the log folder on the VM")
    p.add_argument("--pattern", default="*", help="file name pattern, e.g. 'app.log*' (default: every file)")
    p.add_argument("--from", dest="start", metavar="TIME", help="window start: 2026-10-08 14:00, +02:00 or Z for an "
                   "offset (none: the VM's local time)")
    p.add_argument("--to", dest="end", metavar="TIME", help="window end (default: now)")
    p.add_argument("--last", metavar="DURATION", help="the window is the last 2h, 30m, 1d (on the VM's clock)")
    p.add_argument("--all", action="store_true", help="no window: every file matching the pattern")
    p.add_argument("--grep", metavar="REGEX", help="keep only the entries whose first line matches")
    p.add_argument("--time-format", metavar="STRPTIME", help="the timestamp's format when it is not recognised")
    p.add_argument("--case", metavar="NAME", help="case name (default: date-time and first host)")
    p.add_argument("--ttl", default="24h", help="how long the case is kept, 24h at most (default)")
    p.add_argument("--max-bytes", metavar="SIZE", help="stop once this much has been received in all, e.g. 2GB")
    p.add_argument("--timeout", metavar="DURATION", help="stop a pull that takes longer, e.g. 30m")
    p.add_argument("--follow", metavar="INTERVAL", help="then pull what is added every INTERVAL (e.g. 60s) until "
                   "Ctrl+C or the case expires")
    p.add_argument("--dry-run", action="store_true", help="list the files a pull would read, and pull nothing")
    p.add_argument("--user", help="SSH user (default: from the host, ~/.ssh/config, or yours)")
    p.add_argument("--port", type=int, help="SSH port (default: from the host, ~/.ssh/config, or 22)")
    p.add_argument("--key", help="private key file (default: the agent and ~/.ssh's keys)")
    p.add_argument("--password", action="store_true", help="log in with a password, typed in at the prompt")
    p.add_argument("--known-hosts", help="known_hosts file (default: ~/.ssh/known_hosts); unknown hosts are refused")
    p.add_argument("--python", default="python3", help="the VM's Python (default: python3)")

    p = sub.add_parser("cases", help="list the cases fetch made; expired ones are deleted by every command")
    p.add_argument("--delete", metavar="NAME", help="delete a case now")

    args, extra = parser.parse_known_args(argv)
    for name in cases.purge():
        print(f"yamlboard: deleted the expired case {name}", file=sys.stderr)
    if args.command == "run":
        return _run(args, extra)
    if args.command == "dev":
        return _dev(args, extra)
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if args.command == "lite":
        return _lite(args)
    if args.command == "fetch":
        return _fetch(args)
    if args.command == "cases":
        return _cases(args)
    return _validate(args) if args.command == "validate" else _sql(args)


if __name__ == "__main__":
    sys.exit(main())
