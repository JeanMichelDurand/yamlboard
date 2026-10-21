"""Pull the entries of a rotated log set that fall in a time window. Runs on the VM, sent on stdin to ``python3 -``.

Standard library only, Python 3.10 and later, and nothing written on the VM. yamlboard appends
``main('<params JSON>')`` to this source and reads the frames it writes on stdout, each a kind byte, a 4-byte
big-endian length and the bytes:

* ``D``: gzip data. After a ``D`` frame, what was sent so far is a valid gzip file (whole members), so an
  interrupted pull can be cut back to the last one;
* ``R``: a ``.gz`` file passed through as it is, a part of a member: valid again after the next ``D``;
* ``M``: the summary, sent last, as JSON: the files, what was read of each, the resume state.

An *entry* is a line that starts with a timestamp, and the lines after it that do not (a stack trace): it is kept or
left out whole. The timestamp format is detected on the newest file, or given (``time_format``, a strptime format).
A timestamp with no offset is the VM's local time, and so is a window bound with none.

A file is known across rotations by its first bytes (``HEAD``), whatever its name and compression: the resume state
holds, for each file read, a hash of them and the offset reached, so a refresh reads only what was added, from the
live file or from its rotated copy (renamed, compressed or copied before ``copytruncate``).
"""

import fnmatch
import gzip
import hashlib
import json
import os
import re
import stat
import struct
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone

HEAD = 1024  # bytes that identify a file across rotations
MEMBER = 1 << 20  # uncompressed bytes per gzip member
CHUNK = 1 << 16  # bytes read at a time when a file is passed through
SAMPLE = 200  # lines read to detect the timestamp format
FIRST_LINES = 1000  # lines read to find a file's first timestamp
GZIP_MAGIC = b"\x1f\x8b"
MONTHS = {m.encode(): i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


class Fail(Exception):
    """A pull that cannot run: its message goes to stderr, and the exit code is 2."""


# --- timestamps ---------------------------------------------------------------------------------------------------

_ISO = re.compile(rb"^\[?(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))? ?(Z|[+-]\d{2}:?\d{2})?")
_CLF = re.compile(rb"\[(\d{2})/([A-Z][a-z]{2})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\]")
_SYSLOG = re.compile(rb"^([A-Z][a-z]{2}) ([ \d]\d) (\d{2}):(\d{2}):(\d{2})\b")
JSON_KEYS = ("@timestamp", "timestamp", "time", "ts", "datetime", "date", "asctime")


def _tz(text):
    """``Z``, ``+0200`` or ``+02:00`` as a timezone; none (the VM's local time) for no text."""
    if not text:
        return None
    if text == b"Z":
        return timezone.utc
    t = text.replace(b":", b"")
    sign = -1 if t[:1] == b"-" else 1
    return timezone(sign * timedelta(hours=int(t[1:3]), minutes=int(t[3:5])))


def _fraction(frac):
    return int(frac) / 10 ** len(frac) if frac else 0.0


class Iso:
    """``2026-10-08 14:02:11``, ``T`` or a space, an optional fraction (``.`` or ``,``) and offset, in brackets or not."""

    name = "iso"

    def __init__(self):
        self._key, self._base = None, None

    def parse(self, line, mtime):
        m = _ISO.match(line)
        if not m:
            return None
        key = m.group(1, 2, 3, 4, 5, 6, 8)
        if key != self._key:  # lines of the same second share it: one datetime per second, not per line
            try:
                self._base = datetime(*(int(g) for g in key[:6]), tzinfo=_tz(key[6])).timestamp()
            except (ValueError, OverflowError):
                return None
            self._key = key
        return self._base + _fraction(m.group(7))


class Clf:
    """The access log's ``[08/Oct/2026:14:02:11 +0200]``, anywhere in the line's first 300 bytes."""

    name = "clf"

    def __init__(self):
        self._key, self._base = None, None

    def parse(self, line, mtime):
        m = _CLF.search(line, 0, 300)
        if not m or m.group(2) not in MONTHS:
            return None
        key = m.groups()
        if key != self._key:
            d, mon, y, h, mi, s, z = key
            try:
                self._base = datetime(int(y), MONTHS[mon], int(d), int(h), int(mi), int(s), tzinfo=_tz(z)).timestamp()
            except (ValueError, OverflowError):
                return None
            self._key = key
        return self._base


class Syslog:
    """``Oct  8 14:02:11``: no year, so the file's: the last one that does not put the line after the file's mtime."""

    name = "syslog"

    def __init__(self):
        self._key, self._base = None, None

    def parse(self, line, mtime):
        m = _SYSLOG.match(line)
        if not m or m.group(1) not in MONTHS:
            return None
        year = datetime.fromtimestamp(mtime).year  # noqa: DTZ006 — the VM's local year, as syslog writes it
        key = (*m.groups(), year)
        if key != self._key:
            mon, d, h, mi, s = m.groups()
            try:
                ts = datetime(year, MONTHS[mon], int(d), int(h), int(mi), int(s)).timestamp()
                if ts > mtime + 86400:  # December's lines in a file written in January
                    ts = datetime(year - 1, MONTHS[mon], int(d), int(h), int(mi), int(s)).timestamp()
            except (ValueError, OverflowError):
                return None
            self._key, self._base = key, ts
        return self._base


class JsonLines:
    """A JSON object per line, its time under one of ``JSON_KEYS``: ISO text, or epoch seconds or milliseconds."""

    name = "json"

    def __init__(self):
        self._iso = Iso()

    def parse(self, line, mtime):
        if line[:1] != b"{":
            return None
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        for key in JSON_KEYS:
            value = record.get(key)
            if isinstance(value, str):
                return self._iso.parse(value.encode(), mtime)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value / 1000 if value > 1e11 else float(value)
        return None


class Strptime:
    """A given ``strptime`` format, at the start of the line (after a ``[``)."""

    name = "strptime"

    def __init__(self, fmt):
        self.fmt = fmt
        # The format's width: fixed for the usual directives (%Y %m %d %H %M %S %b %z), so a line's prefix is cut to it.
        self.width = len(datetime(2000, 12, 28, 23, 59, 59, 999999, tzinfo=timezone.utc).strftime(fmt))
        self._key, self._base = None, None

    def parse(self, line, mtime):
        start = 1 if line[:1] == b"[" else 0
        key = line[start:start + self.width]
        if key != self._key:
            try:
                self._base = datetime.strptime(key.decode("ascii"), self.fmt).timestamp()  # noqa: DTZ007 — local time
            except (ValueError, UnicodeDecodeError, OverflowError):
                return None
            self._key = key
        return self._base


DETECTORS = (Iso, Clf, JsonLines, Syslog)


def detect(lines, mtime):
    """The parser matching most of ``lines``, or None when none matches any."""
    best, best_hits = None, 0
    for cls in DETECTORS:
        parser = cls()
        hits = sum(parser.parse(line, mtime) is not None for line in lines)
        if hits > best_hits:
            best, best_hits = cls(), hits
    return best


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NO_SECONDS = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})(?!:\d)")


def bound(value):
    """A window bound: None, epoch seconds, or ISO text (a date alone is its midnight); no offset is local time."""
    if value is None or isinstance(value, (int, float)):
        return value
    text = value.strip()
    if _DATE.fullmatch(text):
        text += " 00:00:00"
    text = _NO_SECONDS.sub(r"\1:00", text)
    ts = Iso().parse(text.encode(), 0)
    if ts is None or not _ISO.fullmatch(text.encode()):
        raise Fail(f"not a time: {value!r} (write 2026-10-08 14:00, with an offset such as +02:00 or Z if needed)")
    return ts


# --- files --------------------------------------------------------------------------------------------------------


def gzip_complete(path):
    """Whether a ``.gz`` file ends where its last member does: one that logrotate is still writing does not, and
    passed through as it is, it would break the stream. Decompressed in C, the output thrown away."""
    try:
        with open(path, "rb") as fh:
            d = zlib.decompressobj(31)
            while True:
                block = d.unused_data or fh.read(CHUNK)
                if not block:
                    return d.eof
                if d.eof:  # the next member
                    d = zlib.decompressobj(31)
                d.decompress(block)
    except (OSError, zlib.error):
        return False


def is_gzip(path):
    with open(path, "rb") as f:
        return f.read(2) == GZIP_MAGIC


def opened(path, gz):
    return gzip.open(path, "rb") if gz else open(path, "rb")


def listing(directory, pattern):
    """The regular files of ``directory`` matching ``pattern``, oldest first."""
    try:
        names = os.listdir(directory)
    except OSError as e:
        raise Fail(f"cannot list {directory}: {e.strerror}") from None
    files = []
    for name in names:
        if not fnmatch.fnmatchcase(name, pattern):
            continue
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            files.append({"name": name, "path": path, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda f: (f["mtime"], f["name"]))
    return files


def head(f):
    """The file's first ``HEAD`` bytes, uncompressed; None for a file that cannot be read yet (a ``.gz`` that
    logrotate is still writing)."""
    try:
        f["gz"] = is_gzip(f["path"])
        with opened(f["path"], f["gz"]) as fh:
            return fh.read(HEAD)
    except (OSError, EOFError):
        return None


def sample(f, n):
    lines = []
    with opened(f["path"], f["gz"]) as fh:
        for line in fh:
            lines.append(line)
            if len(lines) >= n:
                break
    return lines


def first_time(f, parser):
    with opened(f["path"], f["gz"]) as fh:
        for i, line in enumerate(fh):
            ts = parser.parse(line, f["mtime"])
            if ts is not None or i >= FIRST_LINES:
                return ts
    return None


def complete_size(path):
    """Where the last complete line of a plain file ends: a line still being written is not read yet."""
    with open(path, "rb") as fh:
        end = fh.seek(0, os.SEEK_END)
        pos = end
        while pos > 0:
            step = min(CHUNK, pos)
            fh.seek(pos - step)
            block = fh.read(step)
            i = block.rfind(b"\n")
            if i >= 0:
                return pos - step + i + 1
            pos -= step
    return 0


def fingerprint(data):
    return hashlib.sha1(data).hexdigest()


def match(data, known, claimed):
    """The known file whose first bytes ``data`` starts with (the longest such), not yet claimed by another file."""
    best = None
    for i, k in enumerate(known):
        n = k["head_len"]
        if i in claimed or n == 0 or len(data) < n or fingerprint(data[:n]) != k["head"]:
            continue
        if best is None or n > known[best]["head_len"]:
            best = i
    return best


# --- output -------------------------------------------------------------------------------------------------------


class Writer:
    """Lines in, gzip members out, as ``D`` frames, until ``max_bytes`` of them are sent."""

    def __init__(self, out, max_bytes):
        self.out, self.max = out, max_bytes
        self.sent, self.full = 0, False
        self.buf, self.size = [], 0

    def frame(self, kind, data):
        self.out.write(kind + struct.pack(">I", len(data)))
        self.out.write(data)
        self.out.flush()
        self.sent += len(data)

    def fits(self, n):
        return self.max is None or self.sent + n <= self.max

    def write(self, data):
        self.buf.append(data)
        self.size += len(data)
        if self.size >= MEMBER:
            self.flush()

    def flush(self):
        if not self.buf or self.full:
            return
        data = gzip.compress(b"".join(self.buf), compresslevel=6, mtime=0)
        self.buf, self.size = [], 0
        if not self.fits(len(data)):
            self.full = True  # the member is dropped whole: what was sent stays a valid gzip file
            return
        self.frame(b"D", data)

    def raw(self, path):
        """A ``.gz`` file as it is: whole members, so the stream is valid after it."""
        self.flush()
        with open(path, "rb") as fh:
            block = fh.read(CHUNK)
            while block:
                nxt = fh.read(CHUNK)
                self.frame(b"R" if nxt else b"D", block)
                block = nxt


# --- the pull -----------------------------------------------------------------------------------------------------


def read_entries(f, offset, keep, ctx, writer):
    """Send the entries of ``f`` from ``offset`` that are in the window. Returns the offset reached, the last entry's
    decision (the next lines, if any, continue it), the lines sent, and whether the file was read to its end: a
    ``.gz`` cut short (logrotate still writing it) is read on from where it stopped, on the next refresh."""
    start, end, grep, parser = ctx["start"], ctx["end"], ctx["grep"], ctx["parser"]
    pos, lines, complete = offset, 0, True
    try:
        with opened(f["path"], f["gz"]) as fh:
            if offset:
                fh.seek(offset)
            for line in fh:
                if not line.endswith(b"\n"):
                    if not f["gz"]:
                        break  # a plain file's last line may still be being written: read it next time
                    line += b"\n"
                ts = parser.parse(line, f["mtime"])
                if ts is not None:
                    keep = ((start is None or ts >= start) and (end is None or ts <= end)
                            and (grep is None or grep.search(line) is not None))
                if keep:
                    writer.write(line)
                    lines += 1
                    if writer.full:
                        break
                pos += len(line)
    except (OSError, EOFError, zlib.error):
        complete = False
    return pos, keep, lines, complete and not writer.full


def pass_plain(f, offset, writer):
    """Send a plain file from ``offset`` to its last complete line, unparsed."""
    end = complete_size(f["path"])
    with open(f["path"], "rb") as fh:
        fh.seek(offset)
        left = end - offset
        while left > 0 and not writer.full:
            block = fh.read(min(CHUNK, left))
            if not block:
                break
            writer.write(block)
            left -= len(block)
    return end - left


def pull(params, out):
    directory, pattern = params["path"], params.get("pattern") or "*"
    start, end = bound(params.get("start")), bound(params.get("end"))
    if params.get("last"):
        start = time.time() - float(params["last"])
    grep = re.compile(params["grep"].encode()) if params.get("grep") else None
    plan_only = bool(params.get("plan"))
    known = (params.get("state") or {}).get("files", [])

    files = listing(directory, pattern)
    if not files:
        raise Fail(f"no file matches {os.path.join(directory, pattern)}")
    for f in files:
        f["head_bytes"] = head(f)
    readable = [f for f in files if f["head_bytes"] is not None]

    if params.get("time_format"):
        parser = Strptime(params["time_format"])
    elif params.get("format"):  # a refresh reuses the first pull's format
        parser = next(cls() for cls in DETECTORS if cls.name == params["format"])
    else:
        parser, where = None, pattern
        for f in reversed(readable):  # the newest file that has lines and reads to the sample's end
            if not f["head_bytes"]:
                continue
            try:
                lines = sample(f, SAMPLE)
            except (OSError, EOFError, zlib.error):
                continue
            parser, where = detect(lines, f["mtime"]), f["name"]
            break
        if parser is None:
            raise Fail(f"no timestamp format recognised in {where}: give one with --time-format")
    ctx = {"start": start, "end": end, "grep": grep, "parser": parser}

    writer = Writer(out, params.get("max_bytes"))
    claimed, report, state, carry = set(), [], [], False
    for f in files:
        data = f.pop("head_bytes")
        row = {k: f[k] for k in ("name", "size", "mtime")}
        report.append(row)
        if data is None:
            row["action"] = "unreadable"
            continue
        k = match(data, known, claimed)
        if k is not None:
            claimed.add(k)
            offset, keep = known[k]["offset"], known[k]["keep"]
            if offset is None:
                row["action"] = "done"
                state.append(known[k])
                continue
            action = "lines"
        else:
            offset, keep = 0, carry
            try:
                action = "before" if start is not None and f["mtime"] < start else None
                if action is None:
                    row["first"] = first = first_time(f, parser)
                    if end is not None and first is not None and first > end:
                        action = "after"
                    elif (first is not None and (start is None or first >= start)
                          and (end is None or f["mtime"] <= end) and grep is None):
                        action = "pass"
                    else:
                        action = "lines"
            except (OSError, EOFError, zlib.error):
                row["action"] = "unreadable"
                continue
        row["action"], row["from"] = action, offset

        if action in ("before", "after"):
            # Nothing in the window. A plain file may still grow: its next lines are read on a refresh.
            reached = None if f["gz"] else complete_size(f["path"])
        elif plan_only or writer.full:
            reached = offset
        elif action == "pass" and f["gz"] and writer.fits(f["size"]) and gzip_complete(f["path"]):
            writer.raw(f["path"])
            reached = None
        elif action == "pass" and not f["gz"]:
            reached = pass_plain(f, offset, writer)
            keep = True
        else:
            row["action"] = "lines"
            pos, keep, row["lines"], complete = read_entries(f, offset, keep, ctx, writer)
            reached = None if f["gz"] and complete else pos
            if not complete and not writer.full:
                row["action"] = "cut"
        if reached is not None:
            row["to"] = reached
        carry = keep
        stable = len(data) if reached is None else min(len(data), reached)
        if stable:
            state.append({"head": fingerprint(data[:stable]), "head_len": stable, "offset": reached, "keep": keep})
    writer.flush()
    return {
        "files": report,
        "state": None if writer.full else {"files": state},  # a cut pull cannot be resumed: what was cut is lost
        "truncated": writer.full,
        "bytes": writer.sent,
        "format": parser.name,
        "window": {"start": start, "end": end},
        "now": time.time(),
        "tz": time.strftime("%z"),
        "python": sys.version.split()[0],
    }


def main(params_json):
    out = sys.stdout.buffer
    try:
        summary = pull(json.loads(params_json), out)
    except Fail as e:
        sys.stderr.write(f"yamlboard pull: {e}\n")
        sys.exit(2)
    data = json.dumps(summary).encode()
    out.write(b"M" + struct.pack(">I", len(data)))
    out.write(data)
    out.flush()
