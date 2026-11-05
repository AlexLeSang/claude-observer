#!/usr/bin/env python3
"""Capture and summarize Claude Code's OTel token telemetry.

This is Claude-Code-session tooling: it measures agent dispatches and imports
nothing outside the standard library.

Why it exists: a dispatched agent's `<usage>` block reports one aggregate token
count, which hides the cache split entirely. Without that split there is no way
to know an agent workload's real cache-hit rate, and therefore no way to compare
dispatch strategies (fork vs fresh subagent, model tier) on anything but guesswork.

One subcommand:

    report    aggregate a captured file into a per-agent/per-model table

The old `receive` subcommand (a local OTLP/HTTP-JSON sink) is gone; `report`, and the
scrub, split and check tools, still read the captures it wrote.

`report --tools` instead prints per-tool calls, failures, durations, result
bytes, an approximate per-tool share of API tokens and cost, and permission
decisions (`--by-session` adds the busiest sessions and implies `--tools`).
`report --failures` lists recent failed calls grouped by tool and error class,
with input and error text joined from the project's transcripts. `--limit N`
sets the rows per list. Both views live in agent_tool_report.py.
`report --iterations` prints iterations (model requests in an agent's loop,
each with its tool calls) per run (main loop per prompt, or one subagent) by
agent type, model, task and session, the busiest runs, outliers, repeated
failing calls and side requests; it lives in agent_iteration_report.py.
`report --prompts` ranks the costliest prompts, each with the cost of the subagents it dispatched rolled up
through the plugin's agent_spawn records (`--json` for a document); it lives in agent_prompt_report.py.
`report --subagents` regroups the same runs by subagent (agent type: main loop or
subagent type, with where it is defined): tokens, cost, iterations and tool usage per
subagent, per model and per session; it lives in agent_subagent_report.py.
`report --reasoning` compares reasoning (thinking) tokens with output tokens per model,
subagent and session, with an estimated cost and a flag where reasoning dominates, and
per effort level (joined to `api_request` by request id); the counts come from local
transcripts, since OTel has none (see agent_reasoning_report.py).
`report --context` prints peak context fill and fill over time per session, and each
compaction with the cache re-created by the request after it (see agent_context_report.py).
`report --models` compares model x effort pairs: loop iterations, output tokens, cost and
stop reasons, over the capture and per session (see agent_model_report.py).
`report --fixed-context` sizes what every request carries before the prompt: system prompt
sections, context blocks, listed tool descriptions and injected attachments, per session and
against the first request's input tokens (see agent_fixed_context_report.py).
`report --skills` instead prints skill activations per skill, source and
trigger, the tokens of the prompts that activated each skill, each skill's
load overhead (plugin capture only) and the sessions with activations; redacted `custom_skill` names are recovered from
the usage-observer plugin's records and the project's transcripts. It lives in
agent_skill_report.py.
`report --friction [--json]` prints failing hook executions per hook_name and tool error signatures
recurring across sessions (tool name and error kind only, never error text), with a scope line on every run.

`report --plugins` prints plugin inventory, version drift, installs, session
cost with and without each plugin and hook overhead (see agent_plugin_report.py).
`report --sessions` prints one row per session: cost, tokens, cache share, models
and effort, iterations, side-request share, peak context and compactions (see
agent_session_report.py).
`report --digest` writes a Markdown summary of the last `--days N` days (default 7), a
`--since`/`--until` window or `--last-session` to `--out` (default: `agent-digest.md` beside the
capture, owner-only, replaced each run; refuses the capture file or any file that is not a digest) and prints the path
(`--out -` prints the digest to stdout and writes no file); nothing is asked, so a timer can run it
(see agent_digest_report.py).

Plain `report` on a capture with no token metric (the usage-observer plugin's) sums
the model requests instead and prints the same table (see agent_token_report.py).

Usage:

    scripts/agent_telemetry.py report --source logs/agent-telemetry.jsonl   # an old receiver capture
    scripts/agent_telemetry.py report --tools
    scripts/agent_telemetry.py report --failures --limit 3
    scripts/agent_telemetry.py report --iterations
    scripts/agent_telemetry.py report --subagents
    scripts/agent_telemetry.py report --reasoning
    scripts/agent_telemetry.py report --context
    scripts/agent_telemetry.py report --models
    scripts/agent_telemetry.py report --fixed-context
    scripts/agent_telemetry.py report --skills
    scripts/agent_telemetry.py report --plugins
    scripts/agent_telemetry.py report --sessions
    scripts/agent_telemetry.py report --digest
    scripts/agent_telemetry.py report --project <name>   # another project's label
    scripts/agent_telemetry.py report --all-projects     # every project, with a warning

An old receiver capture mixes every project on the machine. Its records carry
`OTEL_RESOURCE_ATTRIBUTES=project=<name>` from a project's committed
.claude/settings.json, and `report` filters on it, falling back
to session.id -> transcript lookup for unlabelled records. A report reads only
the current project's data: without `--project` it takes the label from the
nearest .claude/settings.json at or above the cwd (one owned by another user
is ignored), and with no label it fails
rather than reading the whole capture. `--all-projects` reads everything and
prints a warning line first.

PRIVACY: every payload the old receiver took carried `user.email`, `user.id`,
`organization.id`, account IDs and the repository owner, name and URL; it dropped them (IDENTITY_ATTRIBUTES)
before writing. The plugin's capture drops the same names. A capture is rotated by size, rotated files are deleted
after 90 days, and `report` reads the rotated files too. The capture is gitignored and must stay local.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from capture_paths import default_plugin_capture
from safe_name import agent_label

try:
    import fcntl

    _HAS_FLOCK = True
except ImportError:  # no flock (Windows): appenders run unlocked, as before
    _HAS_FLOCK = False

if TYPE_CHECKING:
    from collections.abc import Iterator

# The old OTLP receiver's capture. Only the old-capture tools (scrub_capture, split_capture)
# default to it; the receiver is gone.
# CLAUDE_OBSERVER_CAPTURE does not apply here: it names the plugin capture, and
# scrub or split must never default to writing or rewriting that file.
DEFAULT_CAPTURE = Path.cwd() / "logs" / "agent-telemetry.jsonl"

# Retention: the live capture is rotated to <capture>.1 before it would grow past
# DEFAULT_MAX_BYTES. A rotated file is deleted once its mtime (its newest record)
# is more than DEFAULT_RETENTION_DAYS old, so at least that much history is kept.
# Disk use is therefore age-bound, not size-bound: about 90 days of capture plus
# up to one file of slack. At the shared capture's observed rate (~573 MB in two
# weeks, identity fields included) that is about 3.7 GB, under 4 GB with slack.
# DEFAULT_BACKUPS caps the rotated files at about 6 GB (60 x 100 MiB, above the ~37 files 90 days
# need), so a flood of writes cannot fill the disk before age pruning has anything to drop
# `--backups 0` removes the cap.
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 90
DEFAULT_BACKUPS = 60

# Identity attributes Claude Code puts on every resource, data point and log
# record: the account, and (OTEL_METRICS_INCLUDE_REPOSITORY) the repository
# owner, name and URL, which carries the machine hostname. No report reads
# them, so the plugin's capture drops them before anything reaches disk. A drop list rather than a keep list: session.id, prompt.id, tool_use_id
# and project are needed by reports and transcript joins, and a keep list would
# silently starve attributes a future report needs.
IDENTITY_ATTRIBUTES = frozenset(
    {
        "user.email",
        "user.id",
        "user.account_uuid",
        "user.account_id",
        "organization.id",
        "vcs.owner.name",
        "vcs.repository.name",
        "vcs.repository.url.full",
    }
)

TOKEN_METRIC = "claude_code.token.usage"  # noqa: S105  (a metric name, not a credential)
MAIN_SESSION = "<main session>"

# The four `type` attribute values claude_code.token.usage splits on. Asserted
# as a closed set rather than discovered at runtime so that a new one shows up
# as an explicit "unknown" row instead of being silently dropped from the table.
TOKEN_TYPES = ("input", "output", "cacheRead", "cacheCreation")


def _attr_value(value: dict[str, Any]) -> Any:
    """Unwrap an OTLP AnyValue ({"stringValue": "x"} -> "x")."""
    return next(iter(value.values()), None)


def _data_point_value(point: dict[str, Any]) -> float:
    """Read a NumberDataPoint's value.

    Claude Code emits `asDouble` even though these are whole token counts;
    `asInt` is handled too because OTLP permits it and proto3 JSON would encode
    it as a *string*. Getting this wrong reads every point as zero.
    """
    # Only `report` reads points, so this may import the report module (the append path does not).
    import agent_tool_report as tool_report

    for key in ("asDouble", "asInt"):
        if key in point:
            return tool_report.as_number(point[key]) or 0.0  # a negative or implausible count reads as 0
    return 0.0


def iter_token_points(payload: object) -> list[tuple[dict[str, Any], float]]:
    """Yield (attributes, value) for every claude_code.token.usage data point.

    Resource attributes are merged underneath the point's own, so a key set only
    on the OTLP resource block (where OTEL_RESOURCE_ATTRIBUTES belongs by the
    spec) is still visible; Claude Code also copies them onto every point.
    """
    # Only `report` reads points, so this may import the report module (the append path does not).
    import agent_tool_report as tool_report

    out: list[tuple[dict[str, Any], float]] = []
    if not isinstance(payload, dict):
        return out
    dicts, attributes = tool_report.dicts, tool_report.otlp_attributes
    for resource in dicts(payload.get("resourceMetrics")):
        resource_attrs = attributes(tool_report.sub_dict(resource, "resource").get("attributes"))
        for scope in dicts(resource.get("scopeMetrics")):
            for metric in dicts(scope.get("metrics")):
                if metric.get("name") != TOKEN_METRIC:
                    continue
                for point in dicts(tool_report.sub_dict(metric, "sum").get("dataPoints")):
                    attrs = {**resource_attrs, **attributes(point.get("attributes"))}
                    out.append((attrs, _data_point_value(point)))
    return out


class ProjectFilter:
    """Decides whether a token-usage data point belongs to one project.

    A `project` attribute (from OTEL_RESOURCE_ATTRIBUTES in the project's
    committed .claude/settings.json) decides outright. Unlabelled points --
    everything captured before the label existed -- belong to the project when
    their session.id has a transcript in the project's Claude Code transcript
    directory, or in a sibling directory for sessions started in the project's
    .claude/worktrees/*. Counts of each outcome are kept for the summary line.
    """

    def __init__(self, project: str, project_dir: Path | None) -> None:
        # The root that owns the label, not the cwd: a subdirectory still finds its
        # project, and a label no project here owns (None) matches no session. Views
        # that read project files (.claude/settings.json) use it too.
        cwd = Path.cwd().resolve()
        root = project_root_for_label(cwd, project)
        # No valid label declared here: the cwd's own transcripts stand in for the project's.
        self.fallback_root = (
            cwd if root == cwd and project_dir is None and not project_label_from_settings(cwd) else None
        )
        if project_dir is None:
            # Imported here, not at module top, so the append path
            # keeps depending on this file alone.
            from agent_usage_report import default_project_dir

            project_dir = default_project_dir(root) if root is not None else None
        self.root = root
        self.project = project
        self.project_dir = project_dir
        self.sessions = self._sessions_under(project_dir) if project_dir is not None else frozenset()
        self.kept_by_label = 0
        self.kept_by_session = 0
        self.dropped = 0

    @staticmethod
    def _sessions_under(project_dir: Path) -> frozenset[str]:
        # Listed once up front: a per-point filesystem lookup over a capture of
        # hundreds of MB would dominate the report's runtime.
        dirs = [project_dir, *project_dir.parent.glob(f"{project_dir.name}--claude-worktrees-*")]
        sessions: set[str] = set()
        for directory in dirs:
            if not directory.is_dir():
                continue
            sessions.update(path.stem for path in directory.glob("*.jsonl"))
            sessions.update(path.parent.name for path in directory.glob("*/subagents"))
        return frozenset(sessions)

    def keep(self, attrs: dict[str, Any]) -> bool:
        label = attrs.get("project")
        if label is not None:
            if str(label) == self.project:
                self.kept_by_label += 1
                return True
        elif attrs.get("session.id") in self.sessions:
            self.kept_by_session += 1
            return True
        self.dropped += 1
        return False

    def reset_counts(self) -> None:
        self.kept_by_label = self.kept_by_session = self.dropped = 0

    def summary(self) -> str:
        line = (
            f"project filter: {self.project} -- kept {self.kept_by_label} by label, "
            f"{self.kept_by_session} by session; dropped {self.dropped}"
        )
        if self.fallback_root is not None:
            line += f"; session fallback: {self.fallback_root} (no label declared)"
        return line


SETTINGS_FIX = (
    'Set "env": {"OTEL_RESOURCE_ATTRIBUTES": "project=<name>"} in this project\'s '
    ".claude/settings.json, or pass --project <name>; --all-projects reads every project's data."
)
ALL_PROJECTS_WARNING = "warning: --all-projects reads every project's data in the capture, not just this project's"


LABEL_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


SETTINGS_UNTRUSTED_MODE = stat.S_IWGRP | stat.S_IWOTH  # 0o022: a file others can write is not ours alone
SETTINGS_MAX_BYTES = 1 << 20  # a real settings.json is a few KB; a bigger file is read as no label


def _read_trusted_settings(directory: Path) -> object:
    """The parsed `<directory>/.claude/settings.json`, or raises OSError/ValueError.

    Trusted only when the directory, its `.claude` and the settings file itself are owned
    by the current user, and the file is not group- or world-writable: outside HOME an ancestor such as a sticky `/tmp` lets
    another user plant a project label. The file is opened without blocking, checked with
    `fstat` on the open descriptor (no stat/open race; a symlink is followed, so its
    target's owner and mode count), and must be a regular file, read at most SETTINGS_MAX_BYTES + 1 bytes (bigger is refused)."""
    uid = os.geteuid()
    if directory.stat().st_uid != uid or (directory / ".claude").stat().st_uid != uid:
        raise PermissionError(directory)
    fd = os.open(directory / ".claude" / "settings.json", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & SETTINGS_UNTRUSTED_MODE:
            raise PermissionError(directory)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(SETTINGS_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > SETTINGS_MAX_BYTES:
        raise ValueError("settings file too large")
    return json.loads(data.decode("utf-8"))


def _settings_labels(start: Path) -> Iterator[tuple[Path, str]]:
    """Each (directory, decoded `project=` label) of a `.claude/settings.json`
    (`env.OTEL_RESOURCE_ATTRIBUTES`) at or above `start`, nearest first. Labels
    are NOT charset-checked here (see `valid_label`). The user's own
    `~/.claude/settings.json` is never a project file, so the search stops
    before the home directory. A directory whose `.claude/settings.json` is not
    owned by the current user (see `_read_trusted_settings`) is skipped, as if it
    had no file."""
    try:
        home: Path | None = Path.home().resolve()
    except (RuntimeError, OSError):  # HOME unset and no passwd entry: no home to stop at
        home = None
    for directory in (start, *start.parents):
        if directory == home:
            break
        try:
            settings = _read_trusted_settings(directory)
            attributes = settings["env"]["OTEL_RESOURCE_ATTRIBUTES"]  # type: ignore[index]
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            continue
        if not isinstance(attributes, str):
            continue
        for pair in attributes.split(","):
            key, _, value = pair.partition("=")
            if key.strip() == "project":
                # An empty value is a declared (invalid) label, not an absent one.
                yield directory, urllib.parse.unquote(value.strip())
                break


def valid_label(label: str) -> bool:
    """Labels are printed in the summary line: letters, digits, `.`, `_`, `-`, 1-64 chars,
    so no control or escape characters, spaces or non-ASCII."""
    return LABEL_RE.fullmatch(label) is not None


def rejected_label_from_settings(start: Path) -> tuple[Path, str] | None:
    """(directory, label) of the nearest declared `project=` label when it fails
    `valid_label` (the directory holds the `.claude/settings.json` declaring it), else None."""
    nearest = next(_settings_labels(start), None)
    return nearest if nearest is not None and not valid_label(nearest[1]) else None


def project_label_from_settings(start: Path) -> str | None:
    """The `project=` label of the nearest `.claude/settings.json` at or above
    `start`, or None. A nearest label that fails the charset check yields None:
    the walk never falls through to an enclosing project's label."""
    label = next((label for _, label in _settings_labels(start)), None)
    return label if label is not None and valid_label(label) else None


def project_root_for_label(start: Path, project: str) -> Path | None:
    """The directory whose transcripts a `--project` filter reads. The nearest
    directory at or above `start` whose settings label it `project`; `start`
    itself when no settings file at or above it declares a valid label (nothing to
    contradict the flag; invalid labels count as none, so the rejection message's own
    `--project <name>` advice works); None when a different label owns the tree or no
    declared label matches."""
    declared = [(directory, label) for directory, label in _settings_labels(start) if valid_label(label)]
    if not declared:
        return start
    return next((directory for directory, label in declared if label == project), None)


def aggregate(capture: Path, project_filter: ProjectFilter | None = None) -> dict[tuple[str, str], dict[str, int]]:
    """Sum token counts per (agent, model), keyed by token type.

    claude_code.token.usage is exported with `aggregationTemporality: 1`
    (DELTA), so each payload carries the increment since the previous export and
    the correct reduction is a SUM across payloads. Taking a max instead --
    which is what you would do for a CUMULATIVE sum -- silently under-reports.
    """
    totals: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for line in iter_capture_lines(capture):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):  # a bare ValueError: an integer past the digit limit
            continue
        if not isinstance(record, dict):
            continue
        for attrs, value in iter_token_points(record.get("payload")):
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            agent = agent_label(attrs.get("agent.name"), MAIN_SESSION)
            model = str(attrs.get("model") or "?")
            token_type = str(attrs.get("type") or "unknown")
            totals[(agent, model)][token_type] += int(value)
    return totals


REQUESTS_NOTE = (
    "This capture has no token metric: rows are summed from api_request events "
    "(loop and side requests) and native step records (loop requests only, for prompts without events)."
)


def format_report(totals: dict[tuple[str, str], dict[str, int]]) -> str:
    if not totals:
        return "No claude_code.token.usage data points found."

    lines = [
        f"{'agent':<20} {'model':<28} {'cacheRead':>11} {'cacheCreate':>12} {'input':>8} {'output':>8} {'cache%':>7}",
        "-" * 100,
    ]
    grand: dict[str, int] = defaultdict(int)
    for (agent, model), counts in sorted(totals.items()):
        read = counts.get("cacheRead", 0)
        created = counts.get("cacheCreation", 0)
        fresh = counts.get("input", 0)
        out = counts.get("output", 0)
        inbound = read + created + fresh
        share = (read / inbound * 100) if inbound else 0.0
        lines.append(f"{agent:<20} {model:<28} {read:>11,} {created:>12,} {fresh:>8,} {out:>8,} {share:>6.1f}%")
        for key, value in counts.items():
            grand[key] += value

        unknown = set(counts) - set(TOKEN_TYPES)
        if unknown:
            lines.append(f"{'':<20} {'':<28}   (unrecognized token types: {sorted(unknown)})")

    total_in = grand["cacheRead"] + grand["cacheCreation"] + grand["input"]
    lines.append("-" * 100)
    lines.append(
        f"{'TOTAL':<20} {'':<28} {grand['cacheRead']:>11,} {grand['cacheCreation']:>12,}"
        f" {grand['input']:>8,} {grand['output']:>8,}"
        f" {(grand['cacheRead'] / total_in * 100) if total_in else 0:>6.1f}%"
    )
    lines.append("")
    lines.append("cache% is cacheRead as a share of all inbound tokens (cacheRead + cacheCreation + input).")
    lines.append("A freshly dispatched subagent starts with no shared context, so it reads no cache and pays full")
    lines.append("cache creation -- expect ~0% for its first run.")
    return "\n".join(lines)


def is_identity_name(key: object) -> bool:
    """Whether `key` names an identity attribute, in any letter case and with space around it."""
    return isinstance(key, str) and key.strip().casefold() in IDENTITY_ATTRIBUTES


def drop_identity(value: Any) -> Any:
    """Return `value` with every OTLP attribute named in IDENTITY_ATTRIBUTES removed.

    Walks the whole payload instead of known paths (resource, data point, log
    record), so a signal Claude Code adds later cannot leak by being missed.
    """
    if isinstance(value, dict):
        return {key: drop_identity(item) for key, item in value.items()}
    if isinstance(value, list):
        return [
            drop_identity(item) for item in value if not (isinstance(item, dict) and is_identity_name(item.get("key")))
        ]
    return value


# ASCII digits only: `str.isdigit` also accepts "\u00b2", which `int` rejects.
ASCII_NUMBER = re.compile(r"[0-9]+")
_WARNED: set[str] = set()


_GROWING = "the capture keeps growing until this is fixed"


def _warn_once(kind: str, error: OSError, consequence: str = _GROWING) -> None:
    """One stderr line per process and failure kind: errno name only, never a path or content."""
    if kind in _WARNED:
        return
    _WARNED.add(kind)
    code = errno.errorcode.get(error.errno or 0, str(error.errno))
    # A warning must never fail or skip the work it describes: a closed, full or broken stderr (or none at
    # all) would otherwise lose the record being appended.
    try:
        print(f"agent_telemetry: {kind} failed ({code}); {consequence}", file=sys.stderr)
    except Exception:  # OSError, ValueError (closed file), UnicodeError, whatever a stream raises
        # The failed text stays in the stream's buffer and its flush at interpreter exit would turn a written
        # record into exit status 120, which the plugin requeues as a duplicate: drop the dead stream.
        sys.stderr = Path(os.devnull).open("w")  # noqa: SIM115


_UNLOCKED = "appending unlocked, so concurrent appenders are not serialised"


def rotated_captures(capture: Path) -> list[Path]:
    """`<capture>.N` files, oldest (highest N) first; numeric, so .10 precedes .9.

    Only regular files (or links to one) count: a directory or dangling link with such a name is not
    history, and rotating or pruning it would fail."""
    if not capture.parent.is_dir():
        return []
    prefix = capture.name + "."
    numbered = [
        (int(path.name[len(prefix) :]), path)
        for path in capture.parent.iterdir()
        if path.name.startswith(prefix) and ASCII_NUMBER.fullmatch(path.name[len(prefix) :]) and path.is_file()
    ]
    return [path for _, path in sorted(numbered, reverse=True)]


def iter_capture_lines(capture: Path) -> Iterator[str]:
    """Yield every line of a capture: rotated files oldest first, then the live file.

    The one place reports read a capture from, so a rotation never hides data.
    Every numbered file present is read, whatever the backup cap is now.

    A plugin record carries `run` (a nonce per module load) and `seq`. After an appender
    timeout the plugin re-sends the batch, which may then be on disk twice; the second
    copy of a (run, seq) is dropped here, so no report counts it. Receiver records have
    neither field and pass untouched.
    """
    seen: set[tuple[Any, Any]] = set()
    for path in [*rotated_captures(capture), capture]:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"seq"' in line and _is_repeat(line, seen):
                    continue
                yield line


def _is_repeat(line: str, seen: set[tuple[Any, Any]]) -> bool:
    """Whether `line` carries a (run, seq) already in `seen`; adds it when new."""
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return False
    if not isinstance(record, dict) or "run" not in record or "seq" not in record:
        return False
    key = (record["run"], record["seq"])
    try:
        if key in seen:
            return True
        seen.add(key)
    except TypeError:  # an unhashable run or seq is not ours: keep the line
        return False
    return False


def prune_rotated(capture: Path, backups: int, retention_days: int, now: float | None = None) -> int:
    """Delete rotated files past the retention window or numbered above `backups`.

    A rename keeps the mtime and records are only appended, so a rotated file's
    mtime is its newest record: deleting it once that is over `retention_days`
    old never loses younger data. `retention_days` 0 disables age pruning and
    `backups` 0 means no count cap. The live capture is never touched. Returns how many files the
    count cap (not their age) removed, so a caller can say that history went.
    """
    if now is None:
        now = time.time()
    cutoff = now - retention_days * 24 * 60 * 60
    capped = 0
    for path in rotated_captures(capture):
        over_cap = backups > 0 and int(path.name.rsplit(".", 1)[1]) > backups
        try:
            if over_cap or (retention_days > 0 and path.stat().st_mtime < cutoff):
                path.unlink()
                capped += over_cap
        except FileNotFoundError:  # a concurrent appender or receiver pruned or rotated it first
            continue
    return capped


def _replace_if_present(source: Path, target: Path) -> None:
    """Rename `source` to `target`, doing nothing when `source` is already gone.

    Under the lock the file is always there. Unlocked (flock refused, sidecar unwritable) a concurrent
    rotator may have moved it first; its rotation then stands and the caller's append recreates the
    live file, so the batch is written rather than dropped.
    """
    with contextlib.suppress(FileNotFoundError):
        source.replace(target)


def _rotate(capture: Path, backups: int, retention_days: int, now: float | None) -> None:
    # Same scheme as logging.handlers.RotatingFileHandler: .N -> .N+1, live -> .1.
    # Highest number first, so a target name is always free; with no count cap
    # there is no top slot, and gaps left by hand-deleted files are kept.
    for path in rotated_captures(capture):
        n = int(path.name.rsplit(".", 1)[1])
        _replace_if_present(path, capture.with_name(f"{capture.name}.{n + 1}"))
    _replace_if_present(capture, capture.with_name(f"{capture.name}.1"))
    # Pruning is housekeeping: its failure must never reach the caller, whose append would be lost.
    try:
        prune_rotated(capture, backups, retention_days, now)
    except OSError as error:
        _warn_once("pruning rotated captures", error)


@contextlib.contextmanager
def _append_lock(capture: Path) -> Iterator[None]:
    """Hold an exclusive flock on `<capture>.lock` while sizing, rotating and writing.

    Sessions (and worktrees, which share the main checkout's capture) each run
    their own appender. Without this, two that both saw the file past the limit
    both rotated, and the second `.N -> .N+1` shift left no live file to move,
    dropping its batch. The size is read under the lock, so the second appender
    sees the file already rotated and just appends. The sidecar is 0600 and has
    no numeric suffix, so `rotated_captures` and pruning never treat it as history.
    Where the sidecar cannot be opened (a read-only directory holding a writable
    capture), or where flock itself fails, the append runs unlocked, as it did before the lock existed, and
    says so once per process and failure kind on stderr (errno only). A platform without flock
    (Windows) is silent: no lock there is the design, not a failure.
    """
    if not _HAS_FLOCK:
        yield
        return
    try:
        fd = os.open(capture.with_name(capture.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        _warn_once("opening the capture lock", error, _UNLOCKED)
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as error:
        # A filesystem that refuses flock (some network homes): append unlocked, as before the lock.
        os.close(fd)
        _warn_once("locking the capture", error, _UNLOCKED)
        yield
        return
    try:
        yield
    finally:
        os.close(fd)  # closing releases the lock


# A claim whose mtime is more than this far from now (either way: a clock can step back) is a
# rotator that died mid-rotation; the next unlocked appender breaks it.
_ROTATION_CLAIM_STALE_SECONDS = 60.0


def _break_stale_claim(claim: Path, found: os.stat_result) -> None:
    """Remove the stale claim `found`, without ever removing a fresh claim that replaced it.

    Unlinking by name is not exclusive: between our look and our unlink another appender may have broken
    the same claim and created its own, which we would then delete (two holders, overwritten rotated
    files). A rename to a private name has the same hole when the grabbed file is put back. So breakers
    serialise on a second `O_EXCL` file, `<claim>.breaking`, and re-look at the claim under it: a fresh
    claim can only appear after the stale one is unlinked, and only a breaker holding the guard unlinks,
    so what it re-looks at is the file it unlinks. A breaker that finds the guard held backs off (the
    caller then just appends); a guard left by a killed breaker is itself removed once stale.
    Residual: that stale-guard removal unlinks by name, so a breaker suspended or killed inside the guard
    window for over a minute, while two others race to remove its guard, can briefly let two breakers
    hold it; the worst case is one more broken claim. Not exclusive in that case.
    """
    guard = claim.with_name(claim.name + ".breaking")
    try:
        os.close(os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    except FileExistsError:
        with contextlib.suppress(OSError):
            if abs(time.time() - os.lstat(guard).st_mtime) > _ROTATION_CLAIM_STALE_SECONDS:
                guard.unlink()
        return
    except OSError:
        return
    try:
        try:
            now_there = os.lstat(claim)
        except FileNotFoundError:
            return
        if (now_there.st_dev, now_there.st_ino, now_there.st_mtime_ns) == (
            found.st_dev,
            found.st_ino,
            found.st_mtime_ns,
        ):
            with contextlib.suppress(OSError):  # a directory or foreign file: leave it, the caller appends
                claim.unlink()
    finally:
        with contextlib.suppress(OSError):
            guard.unlink()


@contextlib.contextmanager
def _rotation_claim(capture: Path) -> Iterator[bool]:
    """Claim the right to rotate `capture`, yielding whether this caller holds it.

    The flock in `_append_lock` serialises the appenders that hold it. Where flock is unavailable or
    refused (full file table, an unopenable sidecar, a network home) an appender runs unlocked, and
    appenders that hold the flock and ones that do not can all be rotating at once, so the claim is taken
    under the flock too. Two racing `.N -> .N+1` shifts overwrite each other's files: whole
    rotated files, and the records in them, vanish. So every rotation takes an
    `O_EXCL` sidecar `<capture>.rotating`, which works where flock does not. The loser skips the
    rotation and just appends: the winner's rotation stands, and an append to a file that was just
    renamed lands in the rotated file or recreates the live one, never in the void. A leftover claim
    from a killed process stops rotation for at most 60 s (see below), never the append. The claim
    holds a nonce and is released only if it still holds ours. A stale claim (see
    `_ROTATION_CLAIM_STALE_SECONDS`) is broken by `_break_stale_claim`. Where the claim cannot be
    created at all (read-only directory) the rotation runs unguarded, as before.
    """
    claim = capture.with_name(capture.name + ".rotating")
    nonce = secrets.token_hex(8)
    held = False
    for _ in range(3):
        try:
            fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            try:
                found = os.lstat(claim)  # lstat: a symlink claim is judged by itself, never its target
            except FileNotFoundError:
                continue  # released between the open and the look: try again
            if abs(time.time() - found.st_mtime) <= _ROTATION_CLAIM_STALE_SECONDS:
                break
            _break_stale_claim(claim, found)
            continue
        except OSError:
            yield True  # no claim possible here: rotate unguarded, as before the claim existed
            return
        with os.fdopen(fd, "w") as handle:
            handle.write(nonce)
        held = True
        break
    try:
        yield held
    finally:
        if held:
            _release_claim(claim, nonce)


def _release_claim(claim: Path, nonce: str) -> None:
    """Unlink `claim` only if it still holds our nonce (a stolen-and-replaced claim is not ours)."""
    try:
        fd = os.open(claim, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            mine = handle.read() == nonce
        if mine:
            claim.unlink()
    except OSError:
        pass


def append_record(
    capture: Path,
    line: str,
    max_bytes: int,
    backups: int,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: float | None = None,
) -> None:
    """Append one line to the capture, rotating first if it would pass `max_bytes`.

    The file is created 0600 and, when missing, its directory 0700: the umask can
    only take bits away, so nothing here ends up readable by other local users.
    Only a non-empty file is rotated, so a single oversized record still gets
    written. `max_bytes` 0 disables rotation. Each rotation also prunes rotated
    files (see `prune_rotated`); `now` is injectable for tests.
    """
    if not capture.parent.exists():
        capture.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = line.encode()
    with _append_lock(capture) if max_bytes > 0 else contextlib.nullcontext():
        if max_bytes > 0:
            try:
                size = capture.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(data) > max_bytes:
                with _rotation_claim(capture) as claimed:
                    if claimed:
                        try:  # the previous holder may have rotated since the size was read
                            size = capture.stat().st_size
                        except FileNotFoundError:
                            size = 0
                        if size and size + len(data) > max_bytes:
                            # A rotation that fails (a directory in a target slot, a read-only directory)
                            # leaves the live file to grow: the record is written, never dropped.
                            try:
                                _rotate(capture, backups, retention_days, now)
                            except OSError as error:
                                _warn_once("rotating the capture", error)
        fd = os.open(capture, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(data)


def _transcript_dir(project_filter: ProjectFilter | None, project_dir: Path | None) -> Path | None:
    """The transcript directory a view reads. Without a project filter it falls
    back to the cwd's; with one it never does, so a label no root owns (None)
    reads no transcripts instead of another project's."""
    if project_dir is not None or project_filter is not None:
        return project_dir
    from agent_usage_report import default_project_dir

    return default_project_dir()


def _report_tools(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_tool_report as tool_report

    events = tool_report.load_events(capture, project_filter)
    if project_filter is not None:
        print(project_filter.summary())
    views = []
    if args.tools or args.by_session:  # --by-session implies --tools
        summary = tool_report.summarize(events)
        views.append(tool_report.format_tool_report(summary, args.by_session, args.limit))
    if args.failures:
        views.append(tool_report.format_failures(events, _transcript_dir(project_filter, project_dir), args.limit))
    print("\n\n".join(views))
    return 0


def _report_friction(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_friction_report as friction_report

    if project_filter is not None:  # stdout stays one JSON document: with --json the summary goes to stderr
        print(project_filter.summary(), file=sys.stderr if args.json else sys.stdout)
    import agent_tool_report as tool_report

    dirs = tool_report.transcript_dirs(_transcript_dir(project_filter, project_dir))
    limit = args.limit or friction_report.TABLE_LIMIT
    print(friction_report.render(capture, project_filter, as_json=args.json, transcript_dirs=dirs, limit=limit))
    return 0


def _print_undated(undated: int) -> None:
    if undated:
        print(
            f"{undated} records have no usable timestamp, so the --since/--until window kept them "
            "(they cannot be placed in time)."
        )


def _report_iterations(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_iteration_report as iteration_report
    import agent_usage_report as usage_report

    since, _ = usage_report._parse_cli_timestamp(args.since)  # noqa: SLF001
    until, _ = usage_report._parse_cli_timestamp(args.until)  # noqa: SLF001
    loaded, undated = iteration_report.filter_window(
        iteration_report.load_capture(capture, project_filter), since, until
    )
    if project_filter is not None:
        print(project_filter.summary())
    report = iteration_report.build_report(loaded, _transcript_dir(project_filter, project_dir))
    print(iteration_report.format_report(report, args.limit))
    _print_undated(undated)
    return 0


def _report_subagents(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_iteration_report as iteration_report
    import agent_subagent_report as subagent_report
    import agent_usage_report as usage_report

    since, _ = usage_report._parse_cli_timestamp(args.since)  # noqa: SLF001
    until, _ = usage_report._parse_cli_timestamp(args.until)  # noqa: SLF001
    loaded, undated = iteration_report.filter_window(
        iteration_report.load_capture(capture, project_filter), since, until
    )
    if project_filter is not None:
        print(project_filter.summary())
    report = iteration_report.build_report(loaded, _transcript_dir(project_filter, project_dir))
    print(subagent_report.format_report(report, args.limit))
    _print_undated(undated)
    return 0


def _report_reasoning(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_reasoning_report as reasoning_report

    report = reasoning_report.build_report(
        capture, project_filter, _transcript_dir(project_filter, project_dir), all_projects=project_filter is None
    )
    if project_filter is not None:
        print(project_filter.summary())
    print(reasoning_report.format_report(report, args.limit))
    return 0


def _report_context(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_context_report as context_report

    report = context_report.build_report(context_report.load_capture(capture, project_filter))
    if project_filter is not None:
        print(project_filter.summary())
    print(context_report.format_report(report, args.limit))
    return 0


def _report_models(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_model_report as model_report

    report = model_report.build_report(model_report.load_capture(capture, project_filter))
    if project_filter is not None:
        print(project_filter.summary())
    print(model_report.format_report(report, args.limit))
    return 0


def _report_fixed_context(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_fixed_context_report as fixed_report

    report = fixed_report.build_report(fixed_report.load_capture(capture, project_filter))
    if project_filter is not None:
        print(project_filter.summary())
    print(fixed_report.format_report(report, args.limit))
    return 0


def _report_skills(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_skill_report as skill_report

    report = skill_report.build_report(
        capture, project_filter, _transcript_dir(project_filter, project_dir), args.limit
    )
    if project_filter is not None:
        print(project_filter.summary())
    print(report)
    return 0


def _report_plugins(
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_plugin_report as plugin_report

    report = plugin_report.build_report(capture, project_filter, _transcript_dir(project_filter, project_dir))
    if project_filter is not None:
        print(project_filter.summary())
    print(report)
    return 0


def _report_sessions(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_session_report as session_report

    report = session_report.build_report(session_report.load_capture(capture, project_filter))
    if project_filter is not None:
        print(project_filter.summary())
    print(session_report.format_report(report, args.limit))
    return 0


def _report_prompts(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_prompt_report as prompt_report
    import agent_usage_report as usage_report

    since = usage_report._parse_cli_timestamp(args.since)[0]  # noqa: SLF001
    until = usage_report._parse_cli_timestamp(args.until)[0]  # noqa: SLF001
    data = prompt_report.read(capture, project_filter)
    result = prompt_report.build(data, since, until)
    limit = args.limit or prompt_report.DEFAULT_LIMIT
    if args.json:  # stdout stays one JSON document: the project summary goes to stderr
        if project_filter is not None:
            print(project_filter.summary(), file=sys.stderr)
        print(prompt_report.to_json(result, data, limit))
        return 0
    if project_filter is not None:
        print(project_filter.summary())
    print(prompt_report.format_table(result, data, limit))
    return 0


def _report_digest(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
) -> int:
    """Write the Markdown digest to a file and print its path."""
    # Imported here so this file keeps depending on the standard library alone.
    import agent_digest_report as digest_report
    import agent_usage_report as usage_report
    import capture_exclude

    since, _ = usage_report._parse_cli_timestamp(args.since)  # noqa: SLF001
    until, _ = usage_report._parse_cli_timestamp(args.until)  # noqa: SLF001
    days = args.days
    if days is None and since is None and until is None and not args.last_session:
        days = digest_report.DEFAULT_DAYS
    text = digest_report.build(
        capture,
        project_filter,
        days=days,
        since=since,
        until=until,
        last_session=args.last_session,
        top=args.limit or digest_report.DEFAULT_TOP,
    )
    if args.out == "-":  # stdout mode: nothing is written, the text is the answer
        if project_filter is not None:
            print(project_filter.summary())
        print(text, end="")
        return 0
    out = Path(args.out) if args.out else digest_report.default_out(capture)
    shown = usage_report._display_path(out)  # noqa: SLF001
    try:
        digest_report.write_digest(out, text, protect=[capture])
    except digest_report.RefusedTargetError as refusal:
        print(f"refusing to write the digest: {refusal}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"cannot write the digest to {shown}: {error.strerror or 'error'}", file=sys.stderr)
        return 1
    # The digest holds session ids, costs and models: keep it out of `git add -A` like the capture.
    if warning := capture_exclude.ensure_excluded(out, sidecars=False, what="digest"):
        print(warning, file=sys.stderr)
    if project_filter is not None:
        print(project_filter.summary())
    print(f"Digest written to {shown}")
    return 0


def _report_tickets(
    args: argparse.Namespace,
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
) -> int:
    # Imported here so this file keeps depending on the standard library alone.
    import agent_session_report as session_report
    import agent_ticket_report as ticket_report
    import agent_usage_report as usage_report

    since, _ = usage_report._parse_cli_timestamp(args.since)  # noqa: SLF001
    until, _ = usage_report._parse_cli_timestamp(args.until)  # noqa: SLF001
    board = ticket_report.read_board(Path(args.board)) if args.board else None
    if args.board and not (Path(args.board) / "tasks").is_dir():
        print("--board: no tasks directory under the given path, so every status is '?'", file=sys.stderr)
    loaded, undated = session_report.filter_window(session_report.load_capture(capture, project_filter), since, until)
    report = ticket_report.build_report(loaded, _transcript_dir(project_filter, project_dir), board)
    if project_filter is not None:
        print(project_filter.summary())
    window = ticket_report.window_label(since, until, args.since, args.until)
    print(ticket_report.format_report(report, args.limit, window, undated))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    import agent_tool_report as tool_report

    tool_report.reset_rejected()
    status = _cmd_report(args)
    note = tool_report.rejected_note()
    if note:
        print(note, file=sys.stderr if args.json else sys.stdout)
    return status


def _view_conflict(args: argparse.Namespace) -> str | None:
    """Which two views were asked for together, though only one prints.

    --digest, --tickets and --sessions document that they override the other views, and --skills and
    --plugins that they override --tools, --failures and --by-session; those pairs stay allowed."""
    if args.digest or args.tickets or args.sessions:
        return None
    views = [
        ("--prompts", args.prompts),
        ("--iterations", args.iterations),
        ("--subagents", args.subagents),
        ("--reasoning", args.reasoning),
        ("--context", args.context),
        ("--models", args.models),
        ("--fixed-context", args.fixed_context),
        ("--skills", args.skills),
        ("--plugins", args.plugins),
        ("--friction", args.friction),
    ]
    chosen = [name for name, on in views if on]
    if len(chosen) > 1:
        return f"{chosen[0]} and {chosen[1]} cannot be combined: each prints its own view; run them one at a time"
    if chosen and chosen[0] not in ("--skills", "--plugins") and (args.tools or args.failures or args.by_session):
        flag = "--tools" if args.tools else "--failures" if args.failures else "--by-session"
        return f"{chosen[0]} and {flag} cannot be combined: each prints its own view; run them one at a time"
    return None


def _window_error(args: argparse.Namespace) -> str | None:
    """Why `--since`/`--until` cannot be used, or None."""
    import agent_usage_report as usage_report

    # _cmd_report picks --digest, --tickets, --sessions, --iterations, --subagents in that order, so
    # --sessions hides the window from --iterations and --subagents.
    windowed = (
        args.tickets or args.digest or (not args.sessions and (args.iterations or args.subagents or args.prompts))
    )
    if (args.since is not None or args.until is not None) and not windowed:
        if args.sessions and (args.iterations or args.subagents or args.prompts):
            return (
                "--since and --until do not apply to --sessions, which takes precedence over --prompts, --subagents "
                "and --iterations; drop --sessions to window those"
            )
        return "--since and --until only apply to --tickets, --digest, --iterations, --subagents and --prompts"
    if (args.days is not None or args.last_session or args.out) and not args.digest:
        return "--days, --last-session and --out only apply to --digest"
    if args.last_session and (args.days is not None or args.since is not None or args.until is not None):
        return "--last-session cannot be combined with --days, --since or --until"
    if args.days is not None and args.since is not None:
        return "--days and --since both set the start of the window; use one"
    conflict = _view_conflict(args)
    if conflict:
        return conflict
    bounds = []
    for flag, raw in (("--since", args.since), ("--until", args.until)):
        parsed, invalid = usage_report._parse_cli_timestamp(raw)  # noqa: SLF001
        if invalid:
            return f"{flag} is not a valid ISO-8601 timestamp: {raw!r}"
        bounds.append(parsed)
    if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
        return f"--since {args.since!r} is after --until {args.until!r}"
    return None


def _cmd_report(args: argparse.Namespace) -> int:
    if args.board and not args.tickets:
        print("--board only applies to --tickets", file=sys.stderr)
        return 2
    if args.json and (args.digest or args.tickets or args.sessions or not (args.friction or args.prompts)):
        print("--json only applies to --friction and --prompts", file=sys.stderr)  # not to a view that overrides them
        return 2
    window_error = _window_error(args)
    if window_error:
        print(window_error, file=sys.stderr)
        return 2
    project_filter = None
    project_dir = Path(args.project_dir) if args.project_dir else None
    project = args.project
    if not project and not args.all_projects:
        project = project_label_from_settings(Path.cwd())
        rejected = rejected_label_from_settings(Path.cwd()) if not project else None
        if rejected is not None:
            rejected_dir, rejected_label = rejected
            print(
                f"project label {rejected_label!r} in {rejected_dir / '.claude' / 'settings.json'} is not allowed: use 1-64 letters, "
                "digits, '.', '_' or '-' (percent-encoding is decoded first). Fix OTEL_RESOURCE_ATTRIBUTES "
                "or pass --project <name>.",
                file=sys.stderr,
            )
            return 2
        if not project and not args.all_projects_if_unlabelled:
            print(f"no project label found for {Path.cwd()}. {SETTINGS_FIX}", file=sys.stderr)
            return 2
    capture = Path(args.source) if args.source is not None else default_plugin_capture()  # the lookup runs only here
    if capture.is_dir():  # read as a capture it yields nothing: an empty report with exit 0
        print(f"{capture} is a directory, not a capture file; pass --source <file>", file=sys.stderr)
        return 1
    if not capture.exists() and not rotated_captures(capture):
        print(f"no capture file at {capture}", file=sys.stderr)
        print(
            "The usage-observer plugin writes logs/agent-telemetry.mod.jsonl in the project root during a session. "
            "Run from the project root, or pass --source <file> (or set CLAUDE_OBSERVER_CAPTURE).",
            file=sys.stderr,
        )
        return 1
    if project:
        project_filter = ProjectFilter(project, project_dir)
        # The views read transcripts from the same project root as the filter; None
        # means no root owns the label, so they read no transcripts at all.
        project_dir = project_filter.project_dir
    else:
        print(ALL_PROJECTS_WARNING, file=sys.stderr if args.json else sys.stdout)
    if args.digest:
        return _report_digest(args, capture, project_filter)
    if args.tickets:
        return _report_tickets(args, capture, project_filter, project_dir)
    if args.sessions:
        return _report_sessions(args, capture, project_filter)
    if args.prompts:
        return _report_prompts(args, capture, project_filter)
    if args.iterations:
        return _report_iterations(args, capture, project_filter, project_dir)
    if args.subagents:
        return _report_subagents(args, capture, project_filter, project_dir)
    if args.reasoning:
        return _report_reasoning(args, capture, project_filter, project_dir)
    if args.context:
        return _report_context(args, capture, project_filter)
    if args.models:
        return _report_models(args, capture, project_filter)
    if args.fixed_context:
        return _report_fixed_context(args, capture, project_filter)
    if args.skills:  # takes precedence over --tools / --failures
        return _report_skills(args, capture, project_filter, project_dir)
    if args.plugins:
        return _report_plugins(capture, project_filter, project_dir)
    if args.friction:
        return _report_friction(args, capture, project_filter, project_dir)
    if args.tools or args.failures or args.by_session:
        return _report_tools(args, capture, project_filter, project_dir)
    totals = aggregate(capture, project_filter)
    from_requests = False
    if not totals:
        # No token metric (the plugin capture): sum the model requests instead.
        # Imported here so this file keeps depending on the standard library alone.
        import agent_token_report as token_report

        if project_filter is not None:
            project_filter.reset_counts()
        totals = token_report.aggregate_requests(capture, project_filter)
        from_requests = bool(totals)
    if project_filter is not None:
        print(project_filter.summary())
    print(format_report(totals))
    if from_requests:
        print(REQUESTS_NOTE)
    return 0


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text!r}")
    return value


MAX_DIGEST_DAYS = 36500


def _days_int(text: str) -> int:
    value = _positive_int(text)
    if value > MAX_DIGEST_DAYS:
        raise argparse.ArgumentTypeError(f"must be at most {MAX_DIGEST_DAYS} days, got {text!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture and summarize Claude Code's OTel token telemetry.")
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="summarize a capture file")
    report.add_argument(
        "--source",
        default=None,
        help="capture file (default: the main checkout's logs/agent-telemetry.mod.jsonl, the plugin capture; "
        "env CLAUDE_OBSERVER_CAPTURE overrides the default)",
    )
    scope = report.add_mutually_exclusive_group()
    scope.add_argument(
        "--project",
        help="only data points labelled project=NAME, or unlabelled ones whose "
        "session has a transcript in --project-dir (default: the project= label in "
        ".claude/settings.json at or above the cwd; an error if there is none)",
    )
    scope.add_argument(
        "--all-projects",
        action="store_true",
        help="read every project's data in the capture, with a warning line",
    )
    scope.add_argument(
        "--all-projects-if-unlabelled",
        action="store_true",
        help="use the project= label from .claude/settings.json when there is one, and read the whole "
        "capture (with the --all-projects warning line) instead of failing when there is none; for a "
        "caller that already points --source at one project's capture. An invalid or empty nearest label still "
        "exits 2 with the message on stderr",
    )
    report.add_argument(
        "--project-dir",
        help="Claude Code transcript dir for the session fallback (default: the transcript dir of the project root "
        "that owns the label, under ~/.claude/projects)",
    )
    report.add_argument(
        "--tools",
        action="store_true",
        help="per-tool calls, failures, durations, result bytes, prompt tokens "
        "and permission decisions instead of the token table",
    )
    report.add_argument(
        "--failures",
        action="store_true",
        help="recent failed tool calls with input and error text joined from the project's transcripts",
    )
    report.add_argument(
        "--by-session",
        action="store_true",
        help="add per-session tool calls to the --tools view (implies --tools)",
    )
    report.add_argument(
        "--iterations",
        action="store_true",
        help="model requests (iterations) per agent run by agent type, model, "
        "task and session, busiest runs, outliers and repeated failing calls, "
        "instead of the token table",
    )
    report.add_argument(
        "--subagents",
        "--personas",  # the old name, kept as an alias
        dest="subagents",
        action="store_true",
        help="tokens, cost, iterations and tool usage per subagent (agent type) by model and session, "
        "instead of the token table",
    )
    report.add_argument(
        "--reasoning",
        action="store_true",
        help="reasoning (thinking) tokens against output tokens per model, subagent and session with "
        "estimated cost, and per effort level, from local transcripts, instead of the token table",
    )
    report.add_argument(
        "--context",
        action="store_true",
        help="peak context fill and fill over time per session, and each compaction with the cache "
        "re-created after it, instead of the token table",
    )
    report.add_argument(
        "--models",
        action="store_true",
        help="iterations, output tokens, cost and stop reasons per model x effort, over the capture "
        "and per session, instead of the token table",
    )
    report.add_argument(
        "--fixed-context",
        action="store_true",
        help="the fixed context cost per session (system prompt sections, context blocks, listed tool "
        "descriptions, injected attachments, subagent attachments) against the first request's input "
        "tokens, instead of the token table; plugin capture only",
    )
    report.add_argument(
        "--skills",
        action="store_true",
        help="skill activations per skill, source and trigger, prompt tokens, "
        "load overhead and sessions, with redacted names recovered locally (overrides --tools)",
    )
    report.add_argument(
        "--plugins",
        action="store_true",
        help="plugins loaded per session, version drift, installs, session cost with and without each "
        "plugin, hook registrations and per-plugin hook overhead from local transcripts (overrides --tools)",
    )
    report.add_argument(
        "--friction",
        action="store_true",
        help="failing hook executions per hook and recurring tool error signatures (tool name and error kind, "
        "never error text), with a scope line on every run",
    )
    report.add_argument(
        "--json",
        action="store_true",
        help="with --friction or --prompts: print machine-readable output (--prompts: one JSON document with full session and prompt ids)",
    )
    report.add_argument(
        "--sessions",
        action="store_true",
        help="one row per session: cost, tokens, cache share, models and effort, iterations, "
        "side-request share, peak context and compactions (overrides the other views)",
    )
    report.add_argument(
        "--prompts",
        action="store_true",
        help="the costliest prompts (top 10, --limit N), each with the cost of the subagents it dispatched rolled "
        "up through the spawn chain; subagent cost that cannot be tied stays in an untied row; prints coverage, "
        "a per-version table and two checks; takes --since and --until (overridden by --sessions)",
    )
    report.add_argument(
        "--tickets",
        action="store_true",
        help="one row per kanban ticket: cost, requests, iterations, models and effort, wall time and "
        "outcome status, attributing sdk executor sessions from their local transcripts (overrides the other views)",
    )
    report.add_argument(
        "--digest",
        action="store_true",
        help="write a Markdown summary (totals, costliest sessions, sessions worth a look) of the last --days days "
        "(default 7), a --since/--until window or --last-session to --out, and print its path; asks nothing, so a "
        "timer can run it (overrides the other views)",
    )
    report.add_argument("--days", type=_days_int, help="with --digest: the last N days (default 7, at most 36500)")
    report.add_argument("--last-session", action="store_true", help="with --digest: only the latest session")
    report.add_argument(
        "--out",
        help="with --digest: the file to write (default agent-digest.md beside the capture; owner-only, replaced each run); - prints the digest to stdout and writes nothing",
    )
    report.add_argument(
        "--since",
        help="with --tickets, --digest, --iterations, --subagents or --prompts: only requests stamped at or after this ISO-8601 date/timestamp "
        "(a naive value is local time; same parsing as agent_usage_report --since)",
    )
    report.add_argument(
        "--until",
        help="with --tickets, --digest, --iterations, --subagents or --prompts: only requests stamped at or before this ISO-8601 date/timestamp "
        "(inclusive; a date alone means 00:00, so it leaves out the rest of that day)",
    )
    report.add_argument(
        "--board",
        help="kanban directory whose tasks/*.md front matter gives each ticket's status for --tickets "
        "(without it the status is '?')",
    )
    report.add_argument(
        "--limit",
        type=_positive_int,
        help="rows per list: sessions for --by-session and --skills (default 20), "
        "failures per group for --failures (default 5); sessions (default 20) and "
        "runs (default 10) for --iterations; sessions (default 20) and tools per subagent (default 5) for "
        "--subagents; tickets (default 20, the newest) for --tickets; costliest sessions (default 5) for --digest; prompts (default 10) for --prompts; recurring tool error signatures (default 10) for --friction; sessions (default 20) for --sessions, --reasoning, --context, --models and --fixed-context",
    )
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    result = args.func(args)
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
