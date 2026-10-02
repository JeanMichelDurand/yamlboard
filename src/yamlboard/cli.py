"""``yamlboard validate | sql | run | dev``."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

from yamlboard import logs
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


def _run(args: argparse.Namespace, extra: list[str]) -> int:
    env = dict(os.environ, YAMLBOARD_REPORTS=str(Path(args.reports).resolve()))
    if args.datasources:
        env["YAMLBOARD_DATASOURCES"] = str(Path(args.datasources).resolve())
    return _streamlit("app.py", env, extra, args.log_dir, "yamlboard.log")


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

    p = sub.add_parser("dev", help="open the dev app: browse a database or a file, test a query, draft a report YAML")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--url", help="connection: SQLAlchemy URL, JDBC URL or DuckDB file (default: in-memory DuckDB)")
    source.add_argument("--file", help="a CSV, JSON, Parquet or log file to open instead (a glob reads several)")
    p.add_argument("--yaml", help="report YAML file to open on the Report page")
    p.add_argument("--log-dir", help=LOG_HELP)

    p = sub.add_parser("lite", help="write yamlboard lite, the dev app's file side as a static site run in the browser")
    p.add_argument("out", help="directory to write the site into (its app/ is replaced)")

    args, extra = parser.parse_known_args(argv)
    if args.command == "run":
        return _run(args, extra)
    if args.command == "dev":
        return _dev(args, extra)
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if args.command == "lite":
        return _lite(args)
    return _validate(args) if args.command == "validate" else _sql(args)


if __name__ == "__main__":
    sys.exit(main())
