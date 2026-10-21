"""Cases: the folders ``yamlboard fetch`` pulls production logs into, kept 24 hours at most.

A case is ``<root>/<name>/``: a sub-folder per host holding the pulled parts (``part-0001.log.gz``, …) and a
``manifest.json`` saying who pulled what, when, from where, and when the case expires. Production logs can hold
personal data, so a case is deleted once expired: on the next ``yamlboard`` command (each one purges first), or by
``yamlboard cases --purge`` from a timer. An expired case that is still there is refused to the readers.

The root is ``~/yamlboard-cases``, or ``$YAMLBOARD_CASES``: out of any repository, so a case is never committed.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_TTL = timedelta(hours=24)
MANIFEST = "manifest.json"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class CaseError(Exception):
    """A case that cannot be created or read."""


def root() -> Path:
    return Path(os.environ.get("YAMLBOARD_CASES") or Path.home() / "yamlboard-cases").expanduser()


def now() -> datetime:
    return datetime.now(timezone.utc)


def check_name(name: str) -> str:
    if not _NAME.match(name) or name in (".", ".."):
        raise CaseError(f"case name {name!r}: letters, digits, '.', '_' and '-', not starting with '.', '_' or '-'")
    return name


def check_host_dir(host: str) -> str:
    """A host as a folder name: ``user@vm-1:2222`` is ``vm-1``."""
    name = host.rsplit("@", 1)[-1].split(":", 1)[0]
    if not _NAME.match(name):
        raise CaseError(f"host {host!r} cannot be a folder name")
    return name


def create(name: str, ttl: timedelta, info: dict[str, Any], base: Path | None = None) -> Path:
    """A new, empty case; ``ttl`` is capped at ``MAX_TTL`` and must be positive."""
    if ttl <= timedelta(0):
        raise CaseError("the case's TTL must be positive")
    if ttl > MAX_TTL:
        raise CaseError(f"the case's TTL is {_hours(MAX_TTL)} at most: production logs are not kept longer")
    d = (base or root()) / check_name(name)
    if d.exists():
        raise CaseError(f"case {name!r} exists already ({d}): choose another name, or delete it")
    d.mkdir(parents=True, mode=0o700)
    created = now()
    save(d, {"name": name, "created": created.isoformat(), "expires_at": (created + ttl).isoformat(),
             "by": f"{getpass.getuser()}@{socket.gethostname()}", **info, "hosts": {}})
    return d


def _hours(td: timedelta) -> str:
    return f"{td.total_seconds() / 3600:g} h"


def load(d: Path) -> dict[str, Any]:
    try:
        return json.loads((d / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CaseError(f"{d}: no readable {MANIFEST}: {e}") from e


def save(d: Path, manifest: dict[str, Any]) -> None:
    """Written to a temporary file then renamed: a reader never sees half a manifest."""
    tmp = d / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    tmp.replace(d / MANIFEST)


def expires_at(manifest: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(manifest["expires_at"])


def expired(manifest: dict[str, Any], at: datetime | None = None) -> bool:
    return expires_at(manifest) <= (at or now())


def cases(base: Path | None = None) -> list[tuple[Path, dict[str, Any] | None]]:
    """Every case folder of the root, with its manifest (None when unreadable), by name."""
    base = base or root()
    if not base.is_dir():
        return []
    out = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        try:
            out.append((d, load(d)))
        except CaseError:
            out.append((d, None))
    return out


def purge(base: Path | None = None, at: datetime | None = None) -> list[str]:
    """Delete the expired cases; returns their names. A folder without a readable manifest is left alone: it may not
    be a case, and what is not known is not deleted."""
    removed = []
    for d, manifest in cases(base):
        if manifest is not None and expired(manifest, at):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed


def size(d: Path) -> int:
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())


def case_of(path: str | Path, base: Path | None = None) -> Path | None:
    """The case folder ``path`` (a file or a glob) is in, or None."""
    base = (base or root()).absolute()
    p = Path(str(path).split("*", 1)[0]).expanduser().absolute()
    try:
        rel = p.relative_to(base)
    except ValueError:
        return None
    return base / rel.parts[0] if rel.parts else None


def check_readable(path: str | Path, base: Path | None = None) -> None:
    """Refuse a path in an expired case: it is deleted on the next command, and is not read before."""
    d = case_of(path, base)
    if d is None or not (d / MANIFEST).exists():
        return
    manifest = load(d)
    if expired(manifest):
        raise CaseError(f"case {d.name!r} expired at {expires_at(manifest):%Y-%m-%d %H:%M %Z}: it is not read any more")
