#!/usr/bin/env python3
"""The latest account quota reading of this machine: the writer's rules and the reader.

    quota_latest.py [--json] [--max-age S]

The plugin's appender (`mod_capture_append.py`) calls `write_latest` after a batch that holds a `measure` record with
a quota window. The file is `~/.claude/observer/quota-latest.json` (override `QUOTA_LATEST_FILE`) and holds account
percentages only: `{"v": 1, "written_at", "observed_at", "windows": [{"kind", "percent", "resets_at"}]}`. It never holds
a session id, prompt id, project, path, model or tool data. A write keeps the existing file when its `observed_at` is
newer (concurrent sessions); it goes through a temporary file in the same directory, `fsync` and `os.replace`, mode 0600.

The reader prints the reading with its age and, per window, one state: `reset` when `resets_at` is earlier than now (the
old percent is not shown), else `stale` when the reading is older than the limit (default 360 s), else `fresh`. A window
without `resets_at` is `fresh` or `stale` by age only. Without a usable file it prints `unknown` and exits 0. The reader
and the writer never start a process or open a socket. Advisory only: a guard decides what to do with the answer.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from instant_util import format_instant, normalise_instant, parse_instant

__all__ = [  # the instant helpers are re-exported: callers and tests reach them here
    "format_instant",
    "normalise_instant",
    "parse_instant",
]

UTC = timezone.utc
ENV_FILE = "QUOTA_LATEST_FILE"
VERSION = 1
KINDS = ("five_hour", "seven_day", "spend_limit")
WINDOW_LENGTH = {"five_hour": timedelta(hours=5), "seven_day": timedelta(days=7)}
DEFAULT_MAX_AGE = 360.0  # the probe's same-session age p90 at dispatch (333 s) rounded up; never below 300 s
MAX_FILE_BYTES = 64 * 1024  # a larger file is not ours
MAX_WINDOWS = len(KINDS)
MAX_PERCENT = 1000.0
SKEW = timedelta(minutes=5)  # an observed_at further ahead than this is a wrong clock, not the newest reading
MAX_AGE_ARG = 1e9  # seconds; an --max-age beyond this is a typo, and keeps timedelta arithmetic in range


def latest_path() -> Path:
    override = os.environ.get(ENV_FILE)
    return Path(override) if override else Path.home() / ".claude" / "observer" / "quota-latest.json"


def clean_percent(value: object) -> float | int | None:
    if type(value) is int:
        return value if 0 <= value <= MAX_PERCENT else None
    if type(value) is float and math.isfinite(value) and 0 <= value <= MAX_PERCENT:
        return round(value, 4) + 0.0  # + 0.0: a negative zero would print as -0%
    return None


def clean_windows(raw: object) -> list[dict[str, Any]]:
    """The windows of a record, reduced to the file's fields: a known kind (first of each), a percent in range and,
    when the instant is valid, `resets_at`. Anything else is dropped."""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for item in raw[: MAX_WINDOWS * 4]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        percent = clean_percent(item.get("percent"))
        if not isinstance(kind, str) or kind not in KINDS or kind in seen or percent is None:
            continue
        seen.add(kind)
        window: dict[str, Any] = {"kind": kind, "percent": percent}
        resets = normalise_instant(item.get("resets_at"))
        if resets is not None:
            window["resets_at"] = resets
        out.append(window)
    return out


def load_latest(path: Path) -> dict[str, Any] | None:
    """The stored reading, validated, or None for a missing, unreadable, oversize, torn, wrong-typed or foreign file.
    A symlink is not followed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        return None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or type(doc.get("v")) is not int or doc["v"] != VERSION:
        return None
    observed = parse_instant(doc.get("observed_at"))
    written = parse_instant(doc.get("written_at"))
    windows = clean_windows(doc.get("windows"))
    if observed is None or written is None or not windows:
        return None
    return {"observed": observed, "written": written, "windows": windows}


def _write_temp(directory: Path, content: bytes) -> Path:
    for attempt in range(100):
        temp = directory / f".quota-latest.{os.getpid()}.{attempt}.tmp"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                temp.unlink()
            raise
        return temp
    raise OSError("no free temporary name")


@contextlib.contextmanager
def _locked(path: Path) -> Any:
    """Serialise writers on a sidecar lock file. Without flock (it raises) the write still goes through an atomic
    replace, so a reader never sees a partial file; only the newest-wins check can then race."""
    lock = path.with_name(path.name + ".lock")
    fd = None
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        pass
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def write_latest(path: Path, observed_at: datetime, windows: list[dict[str, Any]], now: datetime) -> bool:
    """Store the reading unless the file already holds a newer `observed_at`. Return whether it was written.
    Raises OSError when the path is a symlink or the write fails; the caller decides to warn."""
    clean = clean_windows(windows)
    if not clean:
        return False
    if path.is_symlink():
        raise OSError("quota-latest path is a symbolic link; not replaced")
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    document = {
        "v": VERSION,
        "written_at": format_instant(now),
        "observed_at": format_instant(observed_at),
        "windows": clean,
    }
    content = (json.dumps(document, allow_nan=False) + "\n").encode("ascii")
    with _locked(path):
        existing = load_latest(path)
        if existing is not None and observed_at.astimezone(UTC) < existing["observed"] <= now + SKEW:
            return False
        temp = _write_temp(directory, content)
        try:
            temp.replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                temp.unlink()
            raise
    return True


def elapsed_percent(kind: str, resets_at: str | None, now: datetime) -> float | None:
    """How much of the window has run, 0..100: (now - (resets_at - L)) / L, L the window length of the kind. None
    without a reset time, for a kind without a fixed length, or when the reset is more than one length ahead."""
    length = WINDOW_LENGTH.get(kind)
    end = parse_instant(resets_at)
    if length is None or end is None:
        return None
    try:
        value = (now - (end - length)) / length * 100
    except OverflowError:
        return None
    if value < 0:  # the reset is further ahead than the window is long: not a reading of this window
        return None
    return min(100.0, value)


def evaluate(reading: dict[str, Any], now: datetime, max_age: float = DEFAULT_MAX_AGE) -> dict[str, Any]:
    """The reading with its age and a state per window (see the module doc). The age is never negative."""
    age = max(0.0, (now - reading["observed"]).total_seconds())
    windows: list[dict[str, Any]] = []
    for window in reading["windows"]:
        resets = window.get("resets_at")
        end = parse_instant(resets)
        if end is not None and end < now:
            state = "reset"
        elif age > max_age:
            state = "stale"
        else:
            state = "fresh"
        windows.append(
            {
                "kind": window["kind"],
                "percent": None if state == "reset" else window["percent"],
                "resets_at": resets,
                "state": state,
            }
        )
    return {
        "state": "ok",
        "observed_at": format_instant(reading["observed"]),
        "written_at": format_instant(reading["written"]),
        "age_seconds": round(age, 1),
        "max_age_seconds": max_age,
        "windows": windows,
    }


def read_state(path: Path, now: datetime, max_age: float = DEFAULT_MAX_AGE) -> dict[str, Any]:
    reading = load_latest(path)
    if reading is None or reading["observed"] > now + SKEW:
        return {"state": "unknown"}
    return evaluate(reading, now, max_age)


def render(data: dict[str, Any]) -> str:
    if data["state"] == "unknown":
        return "quota latest: unknown (no usable reading)"
    lines = [
        f"quota latest: observed {data['observed_at']} (age {data['age_seconds']:g} s, limit {data['max_age_seconds']:g} s)"
    ]
    for window in data["windows"]:
        if window["state"] == "reset":
            lines.append(f"  {window['kind']}: reset (resets_at {window['resets_at']} has passed), no percent")
            continue
        text = f"  {window['kind']}: {window['percent']:g}% {window['state']}"
        if window["state"] == "stale":
            text += f" (age {data['age_seconds']:g} s)"
        if window["resets_at"] is not None:
            text += f", resets {window['resets_at']}"
        lines.append(text)
    return "\n".join(lines)


def _max_age(raw: str) -> float:
    try:
        value = float(raw) if raw.isascii() and "_" not in raw else math.nan
    except ValueError:
        value = math.nan
    if not 0 <= value <= MAX_AGE_ARG:
        raise argparse.ArgumentTypeError("must be a number of seconds from 0 to 1e9")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument(
        "--max-age", type=_max_age, default=DEFAULT_MAX_AGE, help="seconds after which a reading is stale (default 360)"
    )
    args = parser.parse_args(argv)
    data = read_state(latest_path(), datetime.now(UTC), args.max_age)
    print(json.dumps(data, allow_nan=False) if args.json else render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
