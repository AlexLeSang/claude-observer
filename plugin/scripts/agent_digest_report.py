#!/usr/bin/env python3
"""Periodic Markdown digest for `agent_telemetry.py report --digest`.

Summarizes the sessions of a window (the last N days, an explicit `--since`/`--until`
window, or just the latest session) as a Markdown file the operator can read without
running a report: totals, the costliest sessions, and the sessions worth a look
(context nearly full, compacted, mostly side requests). It is written to a file
without asking, so it can run from cron or a systemd timer; the path is printed.

It reads the same per-session sums as `report --sessions` (see agent_session_report.py)
after the same window cut as `report --tickets --since/--until`, so the numbers
match those views. Requests without a usable timestamp cannot be placed in a window and
are kept, and the digest says so.

Everything from the capture is untrusted text. Only session id prefixes, model names
and effort levels are copied out, each cut short, reduced to ASCII letters, digits and
`._-[]` (anything else becomes `?`) and put in a code span, so a hostile name cannot
add Markdown, HTML, links, table cells or lines. Timestamps are shown only when they
have the shape `YYYY-MM-DD HH:MM`. No prompt text, tool text, path, host, identity
attribute (`user.*`, `organization.id`, `vcs.*`) or project label is written. The file
is owner-only (0600) and is replaced atomically, never written through a symlink.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_session_report as sr
import agent_tool_report as tr
from agent_iteration_report import as_int, plural, unreadable_note

if TYPE_CHECKING:
    from collections import Counter
    from collections.abc import Sequence

    from agent_telemetry import ProjectFilter

DEFAULT_DAYS = 7
DEFAULT_TOP = 5
DIGEST_NAME = "agent-digest.md"
DIGEST_HEADING = "# Usage digest"
HIGH_CONTEXT_PERCENT = 80.0
HIGH_SIDE_COST_PERCENT = 25.0
NAME_LIMIT = 32
NAMES_SHOWN = 3
_UNSAFE = re.compile(r"[^A-Za-z0-9._\[\]-]")
_WHEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}")


def default_out(capture: Path) -> Path:
    """The digest sits next to the capture it summarizes, and is overwritten each run."""
    return capture.parent / DIGEST_NAME


def since_days(days: int, now: datetime) -> datetime:
    return now.astimezone(timezone.utc) - timedelta(days=days)


# --- untrusted text -----------------------------------------------------------------


def _code(text: str, limit: int = NAME_LIMIT) -> str:
    """`text` cut to `limit` characters, reduced to a safe alphabet and put in a code span."""
    return "`" + (_UNSAFE.sub("?", text[:limit]) or "?") + "`"


def _names(names: Counter[str]) -> str:
    ordered = [name.removeprefix(sr.MODEL_PREFIX) for name, _ in names.most_common(NAMES_SHOWN)]
    return ", ".join(_code(name) for name in ordered) or sr.NONE


def _when(timestamp: str) -> str:
    label = tr.when_label(timestamp)
    return label if _WHEN.fullmatch(label) else "?"


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --- formatting ---------------------------------------------------------------------


def _total(sessions: list[sr.Session]) -> sr.Session:
    total = sr.Session("")
    for s in sessions:
        total.requests += s.requests
        total.iterations += s.iterations
        total.sub_iterations += s.sub_iterations
        total.side_requests += s.side_requests
        total.cost += s.cost
        total.side_cost += s.side_cost
        total.uncosted += s.uncosted
        total.fresh += s.fresh
        total.output += s.output
        total.cache_read += s.cache_read
        total.cache_creation += s.cache_creation
        total.compactions += s.compactions
        total.models.update(s.models)
        total.efforts.update(s.efforts)
    return total


def _flags(session: sr.Session) -> list[str]:
    flags = []
    if session.peak_context is not None and session.peak_context >= HIGH_CONTEXT_PERCENT:
        flags.append(f"context peaked at {session.peak_context:.0f}%")
    if session.compactions:
        flags.append(f"compacted {plural(session.compactions, 'time')}")
    side = session.side_cost_share
    if side is not None and side >= HIGH_SIDE_COST_PERCENT:
        flags.append(f"{side:.0f}% of cost on side requests")
    return flags


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
        *("| " + " | ".join(row) + " |" for row in rows),
    ]


def _row(session: sr.Session, whole: sr.Session | None = None) -> list[str]:
    """`whole` is the unclipped session when the window cut part of it off: the row then shows
    its real start, marked clipped, beside the in-window figures."""
    started = _when(session.first) if whole is None else f"{_when(whole.first)} (clipped)"
    return [
        _code(tr.session_label(session.session)),
        started,
        sr.format_cost(session),
        as_int(session.tokens),
        sr.format_percent(session.cache_percent),
        _names(session.models),
        _names(session.efforts),
    ]


def _notes_block(notes: list[str]) -> list[str]:
    """The `## Notes` section, or nothing when there are no notes."""
    return ["## Notes", "", *(f"- {note}" for note in notes), ""] if notes else []


def _totals_block(total: sr.Session, count: int) -> list[str]:
    """The `## Totals` section of the whole window."""
    cost, tokens = sr.format_cost(total), as_int(total.tokens)
    cache = sr.format_percent(total.cache_percent, 1)
    side_requests = sr.format_percent(total.side_request_share)
    side_cost = sr.format_percent(total.side_cost_share)
    requests = f"{total.requests:,} ({total.iterations:,} loop, {total.side_requests:,} side)"
    return [
        "## Totals",
        "",
        f"- Sessions: {count:,}; model requests: {requests}",
        f"- Cost: {cost}; tokens: {tokens}; cache read share: {cache}",
        f"- Side requests: {side_requests} of requests, {side_cost} of cost",
        f"- Models (loop requests): {_names(total.models)}; effort: {_names(total.efforts)}",
        f"- Compactions: {total.compactions:,}",
        "",
    ]


def _sessions_block(sessions: list[sr.Session], clipped: dict[str, sr.Session], top: int) -> list[str]:
    """The one-session table, or the table of the `top` costliest sessions."""
    header = ["session", "started", "cost", "tokens", "cache", "models", "effort"]
    if len(sessions) == 1:
        return ["## Session", "", *_md_table(header, [_row(sessions[0], clipped.get(sessions[0].session))]), ""]
    costliest = sorted(sessions, key=lambda s: (-s.cost, s.session))[:top]
    return [
        f"## Costliest sessions (top {len(costliest)} of {len(sessions)})",
        "",
        *_md_table(header, [_row(s, clipped.get(s.session)) for s in costliest]),
        "",
    ]


def _worth_a_look_block(sessions: list[sr.Session], clipped: dict[str, sr.Session]) -> list[str]:
    """The flagged sessions (never a clipped one), or nothing when none is flagged."""
    flagged = [(s, flags) for s in sessions if s.session not in clipped and (flags := _flags(s))]
    if not flagged:
        return []
    return [
        "## Worth a look",
        "",
        *(f"- {_code(tr.session_label(s.session))}: {'; '.join(flags)}" for s, flags in flagged),
        "",
    ]


def format_digest(
    sessions: list[sr.Session],
    scope: str,
    now: datetime,
    top: int = DEFAULT_TOP,
    undated: int = 0,
    clipped: dict[str, sr.Session] | None = None,
    unreadable: int = 0,
) -> str:
    """`clipped` maps a session id to its whole session when the window cut part of it off.
    Those rows say so and get no flags: context peak and compactions belong to the whole
    session and the shares to the slice, so neither describes the row."""
    clipped = clipped or {}
    lines = [f"{DIGEST_HEADING}: {scope}", "", f"Generated {_utc(now)}.", ""]
    notes = []
    if undated:
        notes.append(f"{plural(undated, 'request')} without a usable timestamp could not be placed in the window; kept")
    if unreadable:
        notes.append(unreadable_note(unreadable))
    if not sessions:
        lines += ["No sessions with model requests in this window.", ""]
        return "\n".join(lines + _notes_block(notes))

    total = _total(sessions)
    lines += _totals_block(total, len(sessions))
    lines += _sessions_block(sessions, clipped, top)
    lines += _worth_a_look_block(sessions, clipped)
    if clipped:
        notes.append(
            f"Clipped sessions ({len(clipped):,}): started before the window or ran past it; started is the "
            "real start, cost and tokens are the in-window part only, and no flags are shown for them"
        )
    if total.uncosted:
        notes.append(f"{plural(total.uncosted, 'request')} without cost (left out of cost)")
    missing = sum(s.peak_context is None for s in sessions)
    if missing:
        notes.append(f"peak context needs the plugin capture; {plural(missing, 'session')} without it")
    return "\n".join(lines + _notes_block(notes))


def scope_label(days: int | None, since: datetime | None, until: datetime | None, last_session: bool) -> str:
    if last_session:
        return "latest session"
    if days is not None and until is None:
        return f"last {plural(days, 'day')}"
    start = _utc(since) if since else "the beginning"
    end = _utc(until) if until else "now"
    return f"{start} to {end}"


def build(
    capture: Path,
    project_filter: ProjectFilter | None,
    *,
    days: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    last_session: bool = False,
    now: datetime | None = None,
    top: int = DEFAULT_TOP,
) -> str:
    """The digest text. `days` is a shorthand for a `since` of that many days before `now`."""
    now = now or datetime.now(timezone.utc)
    if days is not None:
        since = since_days(days, now)
    loaded, undated = sr.filter_window(
        sr.load_capture(capture, project_filter),
        None if last_session else since,
        None if last_session else until,
    )
    sessions = sr.build_report(loaded).sessions
    clipped: dict[str, sr.Session] = {}
    if not last_session and (since is not None or until is not None):
        whole = {s.session: s for s in sr.build_report(sr.load_capture(capture, project_filter)).sessions}
        clipped = {
            s.session: whole[s.session]
            for s in sessions
            if s.session in whole and whole[s.session].requests != s.requests
        }
    if last_session:
        sessions = sessions[:1]
    scope = scope_label(days, since, until, last_session)
    return format_digest(
        sessions, scope, now, top, 0 if last_session else undated, clipped, unreadable=loaded.unreadable
    )


class RefusedTargetError(Exception):
    """The digest would overwrite something that is not an earlier digest."""


def _read_head(path: Path) -> bytes | None:
    """The first bytes of `path`, as many as the digest heading; None when it has vanished. Opened with
    O_NONBLOCK and checked on the descriptor: the `stat` that said "regular file" is older than this open, and a
    FIFO put there since would block `open` forever. A symlink is followed on purpose (see `_check_target`)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RefusedTargetError(f"--out {path.name} exists and is not a regular file; refusing to replace it")
        return os.pread(fd, len(DIGEST_HEADING), 0)
    finally:
        os.close(fd)


def _check_target(path: Path, protect: Sequence[Path]) -> None:
    """Refuse to replace the capture (same path, symlink or hardlink), any regular file
    that does not begin with the digest heading, and any existing pipe, socket or device (opening
    one to read the heading could block, and the rename would replace it), so a mistyped --out
    cannot destroy data. A directory passes: writing to it fails cleanly with an OSError."""
    for other in protect:
        try:
            same = path.resolve() == other.resolve() or (path.exists() and path.samefile(other))
        except OSError:
            same = False
        if same:
            raise RefusedTargetError(f"--out {path.name} is the capture file; the digest would replace your telemetry")
    try:
        mode = path.stat().st_mode  # follows a symlink: what the rename would hide is the target's concern
    except OSError:
        return  # missing (or a dangling link): nothing to protect
    if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
        raise RefusedTargetError(f"--out {path.name} exists and is not a regular file; refusing to replace it")
    if stat.S_ISREG(mode):
        head = _read_head(path)
        if head is None:
            return  # gone since the stat above: nothing to protect
        if head != DIGEST_HEADING.encode():
            raise RefusedTargetError(
                f"--out {path.name} exists and does not begin with '{DIGEST_HEADING}'; "
                "refusing to overwrite a file that is not a digest"
            )


def _make_private_dirs(directory: Path) -> None:
    """Create `directory` and every missing parent owner-only: `mkdir(parents=True, mode=...)` applies
    the mode to the leaf only, so the parents it makes would get the umask default.
    A directory that already exists keeps its mode."""
    missing = []
    for candidate in (directory, *directory.parents):
        if candidate.is_dir():
            break
        missing.append(candidate)
    for candidate in reversed(missing):
        candidate.mkdir(mode=0o700, exist_ok=True)


def write_digest(path: Path, text: str, protect: Sequence[Path] = ()) -> None:
    """Replace `path` with `text`, owner-only from the moment it exists. The text goes to a
    fresh sibling file (never an existing one, so a planted symlink is not followed) that
    is flushed to disk and renamed over `path`; a symlink at `path` is replaced, not written
    through. Raises RefusedTargetError when `path` is a `protect`ed file or not an earlier digest."""
    _check_target(path, protect)
    _make_private_dirs(path.parent)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
