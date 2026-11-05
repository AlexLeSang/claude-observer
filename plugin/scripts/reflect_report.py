#!/usr/bin/env python3
"""The one command the usage-reflection skill runs: a bounded `agent_telemetry.py report` over this project's plugin capture.

    reflect_report.py <view> [--limit N] [--capture PATH]

Run it from the project directory. It finds the project home the way the /observer pane does (the main checkout,
also from a linked worktree), reads only the usage-observer plugin's own capture for that home, runs one report
view from a fixed list and prints a `source:` line first so every number can be cited. It never passes
`--all-projects`, never starts the receiver and refuses any file that does not start with plugin records.

Exit codes: 0 report printed; 2 bad arguments (unknown view); 3 nothing usable to report on (Python older than 3.10,
no kanban board for `tickets`, no capture, not a plugin capture, or a project label that matched none of the
capture's sessions).
Any other code is the report's own.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from capture_paths import project_home
from safe_name import printable_text

__all__ = ["project_home"]  # re-exported: the lookup is shared with agent_telemetry

VIEWS = (
    "sessions",
    "subagents",
    "models",
    "tickets",
    "context",
    "fixed-context",
    "skills",
    "plugins",
    "digest",
    "prompts",
    "personas",
    "friction",
)
# `personas` is the old name of `subagents`, kept so existing calls still run; it is run, and
# printed in the `view:` line, as `subagents`.
VIEW_ALIASES = {"personas": "subagents"}
MIN_PYTHON = (3, 10)
DEFAULT_LIMIT = 5
DEFAULT_CAPTURE = "logs/agent-telemetry.mod.jsonl"
MARKER_RECORDS = 5
MARKER_BYTES = 64 * 1024
REPORT_TIMEOUT_S = 300
BOM = b"\xef\xbb\xbf"
ASCII_DIGITS = re.compile(r"[0-9]+")
FILTER_LINE = re.compile(r"^project filter: .* -- kept (\d+) by label, (\d+) by session; dropped (\d+)", re.MULTILINE)


class Refusal(Exception):  # noqa: N818 - not an error condition: the message is what the user sees
    """Nothing usable to report on: the runner prints the message and exits with `code`."""

    def __init__(self, message: str, code: int = 3) -> None:
        super().__init__(message)
        self.code = code


def rotated_files(capture: Path) -> list[Path]:
    """`<capture>.N` files (N only ASCII digits: `str.isdigit` also accepts `²`, which `int` rejects), oldest
    (highest N) first."""
    prefix = capture.name + "."
    try:
        names = [
            p
            for p in capture.parent.iterdir()
            if p.name.startswith(prefix) and ASCII_DIGITS.fullmatch(p.name[len(prefix) :])
        ]
    except OSError:
        return []
    return sorted(names, key=lambda p: int(p.name[len(prefix) :]), reverse=True)


def _read_head(path: Path) -> bytes | None:
    """Up to MARKER_BYTES of a regular file, or None if it is not one (or vanished). A FIFO is not opened blocking."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        size = 0
        while size < MARKER_BYTES:
            chunk = os.read(fd, MARKER_BYTES - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def head_state(path: Path) -> str:
    """`plugin` when the file's first records are all plugin records, `empty` for a zero-byte regular file, else
    `other` (not a regular file, torn or oversized first line, bad UTF-8, a record that is not JSON, not an object
    or not `"source": "mod"`). Only complete (newline-terminated) lines inside the first 64 KiB count."""
    head = _read_head(path)
    if head is None:
        return "other"
    if not head:
        return "empty"
    head = head.removeprefix(BOM)
    *complete, _unfinished = head.split(b"\n")
    records = 0
    for raw in complete:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
            return "other"
        if not isinstance(record, dict) or record.get("source") != "mod":
            return "other"
        records += 1
        if records == MARKER_RECORDS:
            break
    return "plugin" if records else "other"


def resolve_capture(home: Path, capture_arg: str | None) -> tuple[Path, str]:
    """(path to pass as `--source`, text for the `source:` line). Raises Refusal unless the live capture, or the
    rotated files when it is absent or empty, are plugin captures."""
    shown = capture_arg if capture_arg is not None else DEFAULT_CAPTURE
    capture = Path(shown) if Path(shown).is_absolute() else home / shown
    live = head_state(capture) if (capture.exists() or capture.is_symlink()) else "absent"
    rotated = [(path, head_state(path)) for path in rotated_files(capture)]
    states = [live, *(state for _, state in rotated)]
    if "other" in states:
        bad = capture if live == "other" else next(path for path, state in rotated if state == "other")
        raise Refusal(
            f"{bad} is not a usage-observer plugin capture (its first records must be plugin records with "
            f'"source": "mod"), so no report was run. Pass the plugin capture with --capture PATH.'
        )
    if "plugin" not in states:
        raise Refusal(
            f"no plugin capture at {capture} (nor a rotated {capture.name}.N). Run this from the project directory; "
            "if the usage-observer plugin's `capture` option was customised, pass that path with --capture PATH."
        )
    return capture, shown


def report_argv(view: str, capture: Path, limit: int, board: Path | None) -> list[str]:
    """The exact command line of the report: one fixed view, never `--all-projects`."""
    argv = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve().parent / "agent_telemetry.py"),
        "report",
        f"--{view}",
        "--all-projects-if-unlabelled",
        "--source",
        str(capture),
        "--limit",
        str(limit),
    ]
    if board is not None:
        argv += ["--board", str(board)]
    if view == "digest":
        argv += ["--out", "-"]  # to stdout: the runner never writes a file
    return argv


def label_filter_kept_nothing(report: str) -> bool:
    """Whether the report's filter line says the label kept nothing (a + b == 0) from a non-empty capture (c > 0)."""
    found = FILTER_LINE.search(report)
    if found is None:
        return False
    by_label, by_session, dropped = (int(group) for group in found.groups())
    return by_label + by_session == 0 and dropped > 0


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text!r}")
    return value


class _Parser(argparse.ArgumentParser):
    """Every usage error (a missing view, a view that looks like an option) lists the allowed views."""

    def error(self, message: str) -> NoReturn:
        self.exit(2, f"{self.format_usage()}{self.prog}: error: {message}\nallowed views: {', '.join(VIEWS)}\n")


def run(view: str, limit: int, capture_arg: str | None, cwd: Path) -> int:
    home = project_home(cwd)
    board: Path | None = None
    if view == "tickets":
        if not (home / "kanban" / "tasks").is_dir():
            raise Refusal(f"this project has no kanban board ({home / 'kanban' / 'tasks'} is not a directory)")
        board = home / "kanban"
    capture, shown = resolve_capture(home, capture_arg)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        ran = subprocess.run(
            report_argv(view, capture, limit, board),
            cwd=home,
            env=env,
            capture_output=True,
            timeout=REPORT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"the {view} report did not finish within {REPORT_TIMEOUT_S} seconds", file=sys.stderr)
        return 1
    # What a view printed is read by the model next: nothing a terminal or a model could take for a command
    # gets through, whichever name or value the view failed to clean.
    out = printable_text(ran.stdout.decode("utf-8", errors="replace"))
    if ran.returncode == 0 and label_filter_kept_nothing(out):
        raise Refusal(
            "the project label matched none of this capture's sessions, so the report would be empty "
            "(the label in .claude/settings.json, or the transcripts the session fallback reads, do not match "
            f"{shown})"
        )
    sys.stdout.write(f"source: {shown} view: --{view}\n{out}")
    sys.stdout.flush()
    sys.stderr.write(printable_text(ran.stderr.decode("utf-8", errors="replace")))
    return ran.returncode


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        found = ".".join(str(part) for part in sys.version_info[:3])
        print(
            f"Python {found} is too old: the report needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer, "
            "so no report was run. Run this with `python`, or `py -3`, if that is 3.10 or newer.",
            file=sys.stderr,
        )
        return 3
    # No abbreviations: `--lim` and `--cap` would run, but the plugin's recording of the call reads only the full options.
    parser = _Parser(description="Run one bounded usage report over this project's plugin capture.", allow_abbrev=False)
    parser.add_argument("view", help="one of: " + ", ".join(VIEWS))
    parser.add_argument(
        "--limit", type=_positive_int, default=DEFAULT_LIMIT, help=f"rows per list (default {DEFAULT_LIMIT})"
    )
    parser.add_argument("--capture", help="the plugin capture, relative to the project home or absolute")
    args = parser.parse_args(argv)
    if args.view not in VIEWS:
        print(f"unknown view {args.view!r}; allowed views: {', '.join(VIEWS)}", file=sys.stderr)
        return 2
    try:
        return run(VIEW_ALIASES.get(args.view, args.view), args.limit, args.capture, Path.cwd())
    except Refusal as refusal:
        print(str(refusal), file=sys.stderr)
        return refusal.code


if __name__ == "__main__":
    raise SystemExit(main())
