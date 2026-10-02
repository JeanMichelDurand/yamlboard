"""The ``yamlboard`` logger: one record per statement run, with its run id and its SQL (see ``engine.run``).

Logging is compulsory for the apps. ``setup()`` writes a rotating file in the log directory: ``logs/`` under
the working directory, or ``YAMLBOARD_LOG_DIR`` (``yamlboard run|dev --log-dir``). The board writes
``yamlboard.log``, the dev app ``yamlboard-dev.log``: two processes never rotate the same file. Records go to
stderr too. ``YAMLBOARD_LOG_LEVEL`` sets the level (default ``INFO``; ``WARNING`` keeps only the failed runs).
An app whose log directory is not writable does not start: ``check_dir`` raises ``LogDirError``.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER = "yamlboard"
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DEFAULT_DIR = "logs"
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5


class LogDirError(RuntimeError):
    """The log directory cannot be created or written to."""


def log_dir(path: str | os.PathLike[str] | None = None) -> Path:
    """``path``, else ``YAMLBOARD_LOG_DIR``, else ``logs/`` in the working directory; absolute."""
    return Path(path or os.environ.get("YAMLBOARD_LOG_DIR") or DEFAULT_DIR).expanduser().resolve()


def check_dir(path: str | os.PathLike[str] | None = None, filename: str = "yamlboard.log") -> Path:
    """The log directory, created if missing, once a log file in it is known to be writable.

    Checks the permissions, then opens the file for appending, writing nothing: ``os.access`` alone can be
    wrong (a read-only mount, ACLs), opening the file cannot.
    """
    d = log_dir(path)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise LogDirError(f"cannot create the log directory {d}: {e.strerror or e}") from e
    if not d.is_dir():
        raise LogDirError(f"the log directory {d} is not a directory")
    if not os.access(d, os.W_OK | os.X_OK):
        raise LogDirError(f"no write permission on the log directory {d}")
    try:
        with (d / filename).open("a", encoding="utf-8"):
            pass
    except OSError as e:
        raise LogDirError(f"cannot write {d / filename}: {e.strerror or e}") from e
    return d


def setup(filename: str = "yamlboard.log") -> logging.Logger:
    """Configure the logger once per process: Streamlit reruns the app script, which calls this every time."""
    log = logging.getLogger(LOGGER)
    if getattr(log, "_yamlboard_setup", False):
        return log
    d = check_dir(filename=filename)
    log.setLevel(os.environ.get("YAMLBOARD_LOG_LEVEL", "INFO").upper())
    log.propagate = False  # not twice through Streamlit's own root handler
    handlers: list[logging.Handler] = [
        RotatingFileHandler(d / filename, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    for h in handlers:
        h.setFormatter(logging.Formatter(FORMAT))
        log.addHandler(h)
    log._yamlboard_setup = True  # type: ignore[attr-defined]
    log.info("logging to %s", d / filename)
    return log
