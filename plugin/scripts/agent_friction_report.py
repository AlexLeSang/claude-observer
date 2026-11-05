#!/usr/bin/env python3
"""Friction view for `agent_telemetry.py report --friction [--json]`.

Two counts, both from the OTLP mirror in the capture, never from error text:

- failing hook executions: `hook_execution_complete` events with `num_non_blocking_error > 0`, per session and
  `hook_name`. `hook_name` is kept opaque (never split on ':'). Blocking and cancelled executions have their own
  columns and never count as failing. A byte-identical capture line counts once.
- recurring tool error signatures: `tool_result` events with `success = false`, grouped by
  `(tool_name, error_type)`, listed as recurring when they occur in at least `RECURRING_SESSIONS` sessions.

It prints `tool_name`, `error_type` and `hook_name` (control characters replaced, length capped) and counts. It
never prints the `error` attribute, a hook command, stderr, stdout, a path, or a tool-use or prompt id, and it never
prints a session id. A scope line is printed on every run, so an empty result reads as "none seen", not as "healthy".

Attribution reads the project's transcripts for `hook_non_blocking_error` attachments (one per failed hook run). The
reader keeps only the hook event, the command and the exit code of each attachment in memory; it matches the command
exactly against the installed plugins' `hooks/hooks.json` (as `report --plugins` does) and prints only the plugin name,
`(not a plugin)` or `(ambiguous)`, and an exit-code kind. It also compares, per session, the transcript's failed runs
with the mirror's (`num_non_blocking_error` summed) and reports the sessions where the two differ.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_plugin_report as pr
import agent_tool_report as tr
import transcript_iterator as ti

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_telemetry import ProjectFilter

HOOK_COMPLETE = "hook_execution_complete"
TOOL_RESULT = "tool_result"
ATTACHMENT = "hook_non_blocking_error"
KIND_PLUGIN, KIND_NONE, KIND_AMBIGUOUS = "plugin", "not_a_plugin", "ambiguous"
EXIT_KINDS = {127: "127 (command not found)", 126: "126 (not executable)"}
EXIT_OTHER, EXIT_UNKNOWN = "other exit code", "unknown"
MAX_EXIT_CODE = 1_000_000
RECURRING_SESSIONS = 2  # design D2, set from the operator's probe
FIX_MIN_EXECUTIONS = 2  # design D2: "fails every execution" is 100% of at least 2 executions
TABLE_LIMIT = 10
NAME_WIDTH = 64
NONE = "(none)"
UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
DATE = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})")


def clean(value: object) -> str:
    """A name safe to print in a table row: lone surrogates, control and format characters (newlines, escapes,
    bidi marks) become '?', and the text is capped at `NAME_WIDTH` characters."""
    text = tr.encodable(str(value))
    text = "".join("?" if unicodedata.category(ch) in UNPRINTABLE else ch for ch in text)
    return text if len(text) <= NAME_WIDTH else text[: NAME_WIDTH - 1] + "…"


def name_of(value: object) -> str | None:
    """A string name as recorded, else None: an empty or whitespace-only name is no name."""
    return value if isinstance(value, str) and value.strip() else None


@dataclasses.dataclass
class Pair:
    """One (session, hook_name)."""

    executions: int = 0
    failing: int = 0  # executions with num_non_blocking_error > 0
    failed_runs: int = 0  # the sum of num_non_blocking_error over them: failed hook runs, the transcript's unit
    blocking: int = 0  # executions with num_blocking > 0
    cancelled: int = 0
    failing_ms: float = 0.0
    first: str | None = None
    last: str | None = None


@dataclasses.dataclass
class Signature:
    sessions: set[str] = dataclasses.field(default_factory=set)
    failures: int = 0


@dataclasses.dataclass
class Friction:
    pairs: dict[tuple[str, str | None], Pair] = dataclasses.field(default_factory=lambda: defaultdict(Pair))
    signatures: dict[tuple[str | None, str | None], Signature] = dataclasses.field(
        default_factory=lambda: defaultdict(Signature)
    )
    sessions: set[str] = dataclasses.field(default_factory=set)
    executions: int = 0
    blocking_executions: int = 0
    duplicate_lines: int = 0
    skipped_lines: int = 0  # candidate lines that were not a JSON object
    no_session: int = 0  # events without a usable session id, left out
    unreadable_errors: int = 0  # hook executions whose error count is present but unreadable (see refused_positive)


def count(value: object) -> int:
    """A non-negative whole count from an attribute (string or number); anything unreadable, a fraction included, is 0."""
    number = tr.as_number(value)
    return int(number) if number is not None and number == int(number) else 0


def refused_positive(value: object) -> bool:
    """True for a present error count `count` cannot use, so that it is reported and never silently dropped.

    Rule: an absent count (None) is no error. Any other value that is not a finite whole-or-larger number within the
    cap is unreadable: a bool, "nan", "Infinity", "1e999", an Arabic-Indic digit string, an empty or garbled string,
    a negative, an int past the cap, and any fraction (0.5, 1.5, "2.9"). A readable 0 and a readable count are not."""
    if value is None:
        return False
    number = tr.as_number(value)
    return number is None or number != int(number)


def is_false(value: object) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() == "false")


def add_hook(data: Friction, session: str, attrs: dict[str, Any]) -> None:
    pair = data.pairs[(session, name_of(attrs.get("hook_name")))]
    pair.executions += 1
    data.executions += 1
    if count(attrs.get("num_blocking")) > 0:
        pair.blocking += 1
        data.blocking_executions += 1
    if count(attrs.get("num_cancelled")) > 0:
        pair.cancelled += 1
    errors = attrs.get("num_non_blocking_error")
    if refused_positive(errors):
        data.unreadable_errors += 1
    if count(errors) > 0:
        pair.failing += 1
        pair.failed_runs += count(errors)
        pair.failing_ms += tr.as_number(attrs.get("total_duration_ms")) or 0.0
        stamp = attrs.get("event.timestamp")
        match = DATE.match(stamp) if isinstance(stamp, str) else None
        if match:
            day = match.group(1)
            pair.first = day if pair.first is None else min(pair.first, day)
            pair.last = day if pair.last is None else max(pair.last, day)


def is_object(line: str) -> bool:
    """Whether a line parses as a JSON object (a number past the digit limit or nesting past the recursion limit is not)."""
    try:
        return isinstance(json.loads(line), dict)
    except (ValueError, RecursionError):
        return False


def load(capture: Path, project_filter: ProjectFilter | None = None) -> Friction:
    """Count the capture's hook executions and failed tool results (rotated files included)."""
    data = Friction()
    seen: set[bytes] = set()
    for line in tr.capture_lines(capture):
        # A JSON escape may spell the event name (`\\u0068ook_...`): such a line is parsed, not skipped.
        if HOOK_COMPLETE not in line and TOOL_RESULT not in line and "\\u" not in line:
            if line.strip() and not is_object(line):  # a torn line cut before the marker is still a lost line
                data.skipped_lines += 1
            continue
        digest = hashlib.blake2b(line.encode("utf-8", "replace"), digest_size=16).digest()
        if digest in seen:
            data.duplicate_lines += 1
            continue
        seen.add(digest)
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            record = None
        if not isinstance(record, dict):
            data.skipped_lines += 1
            continue
        for name, attrs in tr.iter_log_events(record.get("payload")):
            if name not in (HOOK_COMPLETE, TOOL_RESULT):
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            session = attrs.get("session.id")
            if not isinstance(session, str) or not session:
                data.no_session += 1
                continue
            data.sessions.add(session)
            if name == HOOK_COMPLETE:
                add_hook(data, session, attrs)
            elif is_false(attrs.get("success")):
                sig = data.signatures[(name_of(attrs.get("tool_name")), name_of(attrs.get("error_type")))]
                sig.sessions.add(session)
                sig.failures += 1
    return data


@dataclasses.dataclass
class Source:
    """Failed hook runs of one (source, exit-code kind) in the transcripts."""

    runs: int = 0
    sessions: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class Transcripts:
    """What the transcripts say about failed hook runs; no command, output or id is kept."""

    files: int = 0
    unreadable_lines: int = 0  # lines naming the attachment that were not a JSON object
    torn_ends: int = 0  # files whose last non-blank line was not a JSON object (a torn write, cut before the marker)
    unreadable_files: int = 0  # transcript files that could not be opened or read to the end: not counted in `files`
    unreadable_plugins: int = 0  # installed plugins whose hooks.json could not be read
    seen: set[str] = dataclasses.field(default_factory=set)  # sessions with a transcript file
    runs: dict[str, int] = dataclasses.field(default_factory=lambda: defaultdict(int))  # session -> failed runs
    sources: dict[tuple[str, str | None, str], Source] = dataclasses.field(default_factory=lambda: defaultdict(Source))


def exit_kind(value: object) -> str:
    """The kind of a hook's exit code: a whole number in 0..`MAX_EXIT_CODE`, else unknown."""
    number = tr.as_number(value, MAX_EXIT_CODE)
    if number is None or number != int(number):
        return EXIT_UNKNOWN
    return EXIT_KINDS.get(int(number), EXIT_OTHER)


def attachments(path: Path, data: Transcripts) -> list[dict[str, Any]]:
    """The `hook_non_blocking_error` attachments of one transcript file, or none when the file cannot be opened or
    read to the end (counted in `data.unreadable_files`; nothing of a partly read file is kept, its lines are not
    counted either). A candidate line that is not a JSON object is counted in `data.unreadable_lines`, never kept
    silently. The file's last non-blank line, when it is not a JSON object and does not carry the marker as plain
    text (cut off before it: a torn write, perhaps inside an escape), is counted in `data.torn_ends` instead."""
    found: list[dict[str, Any]] = []
    unreadable = 0
    last = ""  # the last non-blank line that the prefilter did not parse
    torn = ""  # a line that failed to parse without the marker as plain text: a torn end if nothing follows it
    fd = ti._open_regular(path)  # noqa: SLF001  # never blocks on a FIFO, refuses a symlink and a non-regular file
    if fd is None:
        data.unreadable_files += 1
        return []
    try:
        with open(fd, encoding="utf-8", errors="replace") as handle:  # noqa: PTH123  # closes the fd
            for line in handle:
                if line.strip() and torn:  # something follows it, so it was not the end of the file
                    unreadable += 1
                    torn = ""
                # A JSON escape may spell the type (`\\u0068ook_...`): such a line is parsed, not skipped.
                if ATTACHMENT not in line and "\\u" not in line:
                    last = line if line.strip() else last
                    continue
                last = ""
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):  # ValueError: an integer past the digit limit
                    record = None
                if not isinstance(record, dict):
                    if ATTACHMENT in line:
                        unreadable += 1
                    else:
                        torn = line
                    continue
                attachment = record.get("attachment")
                if isinstance(attachment, dict) and attachment.get("type") == ATTACHMENT:
                    found.append(attachment)
    except OSError:
        data.unreadable_files += 1
        return []
    data.unreadable_lines += unreadable
    if torn or (last and not is_object(last)):
        data.torn_ends += 1
    return found


def owner_of(declared: dict[tuple[str, str], set[str]], attachment: dict[str, Any]) -> tuple[str, str | None]:
    """(kind, plugin name): the one installed plugin whose hooks.json declares exactly this command for this event,
    none (`not_a_plugin`), or several or an unreadable event or command (`ambiguous`)."""
    event, command = attachment.get("hookEvent"), attachment.get("command")
    if not isinstance(event, str) or not isinstance(command, str):
        return KIND_AMBIGUOUS, None
    owners = declared.get((event, command), set())
    if len(owners) == 1:
        return KIND_PLUGIN, next(iter(owners))
    return (KIND_AMBIGUOUS, None) if owners else (KIND_NONE, None)


def read_transcripts(dirs: Sequence[Path], local: pr.LocalPlugins) -> Transcripts:
    """Count the failed hook runs of every transcript under `dirs` (subagent transcripts count for their session)."""
    data = Transcripts()
    declared, data.unreadable_plugins = pr.declared_commands(local)
    for directory in dirs:
        for context in ti.discover_transcripts(directory):
            failed_before = data.unreadable_files
            for attachment in attachments(context.transcript_path, data):
                kind, plugin = owner_of(declared, attachment)
                source = data.sources[(kind, plugin, exit_kind(attachment.get("exitCode")))]
                source.runs += 1
                source.sessions.add(context.session_id)
                data.runs[context.session_id] += 1
            if data.unreadable_files == failed_before:  # an unreadable file says nothing about its session
                data.files += 1
                data.seen.add(context.session_id)
    return data


def agreement(data: Friction, found: Transcripts) -> dict[str, int]:
    """Per session, the mirror's failed runs against the transcript's. `no_transcript` is a mirror failure whose
    session has no transcript file here (not a disagreement: it cannot be compared)."""
    mirror: dict[str, int] = defaultdict(int)
    for (session, _), pair in data.pairs.items():
        mirror[session] += pair.failed_runs
    out = dict.fromkeys(("agree", "counts_differ", "mirror_only", "transcript_only", "no_transcript"), 0)
    for session in {s for s, n in mirror.items() if n} | set(found.runs):
        m, t = mirror.get(session, 0), found.runs.get(session, 0)
        if m == t:
            out["agree"] += 1
        elif m and t:
            out["counts_differ"] += 1
        elif t:
            out["transcript_only"] += 1
        elif session in found.seen:
            out["mirror_only"] += 1
        else:
            out["no_transcript"] += 1
    return out


def transcript_report(data: Friction, found: Transcripts | None) -> dict[str, Any]:
    """The attribution part of the report: counts and labels only."""
    found = found or Transcripts()
    rows: list[dict[str, Any]] = [
        {
            "kind": kind,
            "plugin": None if plugin is None else clean(plugin),
            "exit_code_kind": exit_,
            "runs": source.runs,
            "sessions": len(source.sessions),
        }
        for (kind, plugin, exit_), source in found.sources.items()
    ]
    rows.sort(key=lambda r: (-r["runs"], r["kind"], r["plugin"] or "", r["exit_code_kind"]))
    return {
        "files": found.files,
        "failed_runs": sum(found.runs.values()),
        "unreadable_lines": found.unreadable_lines,
        "unreadable_files": found.unreadable_files,
        "torn_ends": found.torn_ends,
        "unreadable_plugins": found.unreadable_plugins,
        "agreement": agreement(data, found) if found.files else None,
        "sources": rows,
    }


def build(data: Friction, found: Transcripts | None = None) -> dict[str, Any]:
    """The report as plain data (the `--json` form); no session id, command, path or error text."""
    failing = {key: pair for key, pair in data.pairs.items() if pair.failing > 0}
    by_hook: dict[str | None, list[Pair]] = defaultdict(list)
    for (_, hook), pair in failing.items():
        by_hook[hook].append(pair)
    ran_in: dict[str | None, int] = defaultdict(int)  # sessions that ran the hook at all, failing or not
    for _, hook in data.pairs:
        ran_in[hook] += 1
    hooks: list[dict[str, Any]] = []
    for hook, pairs in by_hook.items():
        days = [d for p in pairs for d in (p.first, p.last) if d]
        hooks.append(
            {
                "hook_name": None if hook is None else clean(hook),
                "sessions": len(pairs),
                "clean_sessions": ran_in[hook] - len(pairs),
                "executions": sum(p.executions for p in pairs),
                "failing": sum(p.failing for p in pairs),
                "blocking": sum(p.blocking for p in pairs),
                "cancelled": sum(p.cancelled for p in pairs),
                "failing_duration_ms": sum(p.failing_ms for p in pairs),
                "fails_every_execution": any(
                    p.failing == p.executions and p.executions >= FIX_MIN_EXECUTIONS for p in pairs
                ),
                "first_seen": min(days) if days else None,
                "last_seen": max(days) if days else None,
            }
        )
    hooks.sort(key=lambda h: (-h["failing"], h["hook_name"] or ""))
    signatures: list[dict[str, Any]] = []
    for (tool, error_type), sig in data.signatures.items():
        signatures.append(
            {
                "tool_name": None if tool is None else clean(tool),
                "error_type": None if error_type is None else clean(error_type),
                "sessions": len(sig.sessions),
                "failures": sig.failures,
                "share_of_sessions": len(sig.sessions) / len(data.sessions) if data.sessions else 0.0,
                "recurring": len(sig.sessions) >= RECURRING_SESSIONS,
            }
        )
    signatures.sort(key=lambda s: (-s["sessions"], -s["failures"], s["tool_name"] or "", s["error_type"] or ""))
    return {
        "scope": {
            "sessions": len(data.sessions),
            "executions": data.executions,
            "session_hook_pairs": len(data.pairs),
            "failing_session_hook_pairs": len(failing),
            "failing_executions": sum(p.failing for p in failing.values()),
            "blocking_executions": data.blocking_executions,
            "signatures": len(signatures),
            "recurring_signatures": sum(1 for s in signatures if s["recurring"]),
            "tool_failures": sum(s["failures"] for s in signatures),
            "recurring_sessions_threshold": RECURRING_SESSIONS,
            "duplicate_lines": data.duplicate_lines,
            "skipped_lines": data.skipped_lines,
            "events_without_session": data.no_session,
            "unreadable_error_counts": data.unreadable_errors,
        },
        "hooks": hooks,
        "signatures": signatures,
        "transcripts": transcript_report(data, found),
    }


def scope_line(scope: dict[str, Any]) -> str:
    counts = (
        f"{scope['failing_session_hook_pairs']} of {scope['session_hook_pairs']} session-hook pairs failed, "
        f"{scope['executions']} executions, {scope['sessions']} sessions; non-blocking errors only"
    )
    hooks = (
        f"failing hooks seen ({counts})."
        if scope["failing_session_hook_pairs"]
        else f"no failing hooks seen ({counts})."
    )
    total = (
        f"{scope['signatures']} signature{'s' if scope['signatures'] != 1 else ''}, "
        f"{scope['tool_failures']} failure{'s' if scope['tool_failures'] != 1 else ''}"
    )
    n = scope["recurring_signatures"]
    plural = "s" if n != 1 else ""
    tools = (
        f"{n} tool-error signature{plural} in {RECURRING_SESSIONS} or more sessions ({total})."
        if n
        else f"No tool-error signature in {RECURRING_SESSIONS} or more sessions ({total})."
    )
    return f"friction: {hooks} {tools}"


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    return [fmt(headers), *(fmt(r) for r in rows)]


SOURCE_LABELS = {KIND_NONE: pr.NOT_A_PLUGIN, KIND_AMBIGUOUS: pr.AMBIGUOUS}


def transcript_lines(found: dict[str, Any], mirror_failing: int) -> list[str]:
    """The transcript attribution lines; empty when no transcript was read and the mirror saw no failure."""
    notes = []
    if found["unreadable_files"]:
        notes.append(f"{found['unreadable_files']} transcript file(s) could not be read and are left out")
    if not found["files"]:
        head = (
            ["", "Transcripts: none found for this project, so failing hooks are not attributed to a plugin."]
            if mirror_failing or found["unreadable_files"]
            else []
        )
        return [*head, "note: " + "; ".join(notes) + "."] if notes else head
    out = ["", f"Transcripts: {found['files']} read, {found['failed_runs']} failed hook run(s) in them."]
    if found["unreadable_lines"]:
        notes.append(f"{found['unreadable_lines']} transcript line(s) naming a hook error were not JSON objects")
    if found["torn_ends"]:
        notes.append(f"{found['torn_ends']} transcript file(s) end on a line that is not a JSON object")
    if found["unreadable_plugins"]:
        notes.append(f"{found['unreadable_plugins']} installed plugin(s)' hooks.json could not be read")
    if notes:
        out.append("note: " + "; ".join(notes) + ".")
    ag = found["agreement"]
    if any(ag.values()):
        compared = ag["agree"] + ag["counts_differ"] + ag["mirror_only"] + ag["transcript_only"]
        text = (
            f"Mirror and transcript agree on failed hook runs in {ag['agree']} of {compared} session(s) compared"
            f" ({ag['counts_differ']} with different counts, {ag['mirror_only']} mirror only,"
            f" {ag['transcript_only']} transcript only)"
        )
        if ag["no_transcript"]:
            text += f"; {ag['no_transcript']} session(s) with a mirror failure have no transcript here"
        out.append(text + ".")
    if found["sources"]:
        rows = [
            [
                r["plugin"] or SOURCE_LABELS.get(r["kind"], ""),
                r["exit_code_kind"],
                str(r["runs"]),
                str(r["sessions"]),
            ]
            for r in found["sources"]
        ]
        out += ["", "Failed hook runs by source (transcripts)"]
        out += table(["plugin", "exit code", "runs", "sessions"], rows)
    return out


def format_report(report: dict[str, Any], limit: int = TABLE_LIMIT) -> str:
    scope = report["scope"]
    out = [scope_line(scope)]
    if scope["blocking_executions"]:
        out.append(
            f"Blocking executions (a guard doing its job, not counted as failing): {scope['blocking_executions']}."
        )
    notes = []
    if scope["duplicate_lines"]:
        notes.append(f"{scope['duplicate_lines']} byte-identical capture line(s) counted once")
    if scope["skipped_lines"]:
        notes.append(f"{scope['skipped_lines']} capture line(s) were not JSON objects and were left out")
    if scope["events_without_session"]:
        notes.append(f"{scope['events_without_session']} event(s) without a session id were left out")
    if scope["unreadable_error_counts"]:
        notes.append(
            f"{scope['unreadable_error_counts']} hook execution(s) had an unreadable, oversized or fractional error count "
            "and were not counted as failing"
        )
    if notes:
        out.append("note: " + "; ".join(notes) + ".")
    if report["hooks"]:
        rows = [
            [
                h["hook_name"] or NONE,
                f"{h['failing']} of {h['executions']}",
                str(h["blocking"]),
                str(h["sessions"]),
                str(h["clean_sessions"]),
                f"{h['failing_duration_ms']:.0f}",
                "every run" if h["fails_every_execution"] else "",
                f"{h['first_seen'] or '-'} .. {h['last_seen'] or '-'}",
            ]
            for h in report["hooks"]
        ]
        out += [
            "",
            "Failing hooks (failing and executions cover the failing sessions only; clean_sessions ran the hook without a failure)",
        ]
        out += table(
            ["hook_name", "failing", "blocking", "sessions", "clean_sessions", "failing_ms", "fix?", "seen"], rows
        )
    out += transcript_lines(report["transcripts"], report["scope"]["failing_executions"])
    recurring = [s for s in report["signatures"] if s["recurring"]]
    if recurring:
        shown = recurring[:limit]
        rows = [
            [
                s["tool_name"] or NONE,
                s["error_type"] or NONE,
                str(s["sessions"]),
                str(s["failures"]),
                f"{100 * s['share_of_sessions']:.1f}%",
            ]
            for s in shown
        ]
        out += ["", "Recurring tool error signatures (a signature recurring is not proof of a config problem)"]
        out += table(["tool_name", "error_type", "sessions", "failures", "share of sessions"], rows)
        if len(recurring) > len(shown):
            out.append(f"... and {len(recurring) - len(shown)} more")
    return "\n".join(out)


def render(
    capture: Path,
    project_filter: ProjectFilter | None,
    *,
    as_json: bool,
    transcript_dirs: Sequence[Path] = (),
    claude_dir: Path | None = None,
    limit: int = TABLE_LIMIT,
) -> str:
    data = load(capture, project_filter)
    found = None
    dirs = [d for d in transcript_dirs if d.is_dir()]
    if dirs:
        local = pr.load_local(claude_dir or pr.claude_config_dir(), None)
        found = read_transcripts(dirs, local)
    report = build(data, found)
    if as_json:
        return json.dumps(report, indent=2, allow_nan=False, ensure_ascii=True)
    return format_report(report, limit)
