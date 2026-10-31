#!/usr/bin/env python3
"""Tool usage views for `agent_telemetry.py report --tools / --failures`.

Reads the OTLP *log* payloads in the capture (the token table reads only the
metric payloads) and reports, per tool:

- calls, sessions, failures, `error_type` breakdown and duration, from
  `claude_code.tool_result`;
- result bytes pushed back into context, and each prompt's API tokens and
  cost (`claude_code.api_request`, joined on `prompt.id`) split across the
  tools that prompt called;
- permission decisions by source, from `claude_code.tool_decision`.

`--failures` lists recent failed calls with the tool input and error text.
OTel only carries the error *class* (`ShellError`, `TelemetrySafeError`, ...),
so the text is joined in by `tool_use_id` from the project's own transcripts
at report time; it is never written to the capture, and neither
OTEL_LOG_TOOL_DETAILS nor OTEL_LOG_TOOL_CONTENT is needed.

MCP tools: Claude Code exports every `mcp__<server>__<tool>` name as the
literal `mcp_tool` (tool_source `mcp` or `sdk_host_builtin_mcp`) unless
OTEL_LOG_TOOL_DETAILS is set. The summary tables keep that redacted bucket;
`--failures` shows the transcript's real name for the calls it lists.

Identity attributes (`user.*`, `organization.id`, `vcs.*`) are never read,
and sessions are shown only as an 8-character `session.id` prefix.
"""

from __future__ import annotations

import dataclasses
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcript_iterator as ti

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agent_telemetry import ProjectFilter

TOOL_RESULT = "tool_result"
TOOL_DECISION = "tool_decision"
API_REQUEST = "api_request"
MCP_BUCKET = "mcp_tool"
NO_TOOL_CALLS = "(no tool calls)"

SESSION_PREFIX = 8
INPUT_WIDTH = 100
ERROR_WIDTH = 200
DEFAULT_SESSION_LIMIT = 20
DEFAULT_FAILURE_LIMIT = 5

# Column order for permission sources Claude Code is known to emit; any other
# value observed in the capture is appended after these, sorted, rather than
# folded into an "other" column.
KNOWN_SOURCES = ("config", "hook", "user_permanent", "user_temporary", "user_abort", "user_reject")

API_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def capture_lines(capture: Path) -> Iterator[str]:
    """Every raw line of the capture, rotated files included. The one place
    this module reads it."""
    from agent_telemetry import iter_capture_lines

    yield from iter_capture_lines(capture)


@dataclasses.dataclass
class Skipped:
    """How many non-blank capture lines `capture_records` could not use."""

    lines: int = 0


def capture_records(capture: Path, skipped: Skipped | None = None) -> Iterator[dict[str, Any]]:
    """Every JSON object in the capture, rotated files included; the one place reports parse a line.

    A blank line is nothing. Any other line that is not a JSON object is counted in `skipped.lines`
    and left out: a torn last line (a crash mid-write), a stray byte, a number past Python's
    integer-digit limit (`ValueError`), JSON nested past the recursion limit (`RecursionError`), a
    bare array or number."""
    for line in capture_lines(capture):
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except (ValueError, RecursionError):
            record = None
        if isinstance(record, dict):
            yield record
        elif skipped is not None:
            skipped.lines += 1


def _attr_value(value: object) -> Any:
    """Unwrap an OTLP AnyValue ({"stringValue": "x"} -> "x")."""
    if not isinstance(value, dict):
        return None
    return next(iter(value.values()), None)


def dicts(value: object) -> list[dict[str, Any]]:
    """The objects in a list, and nothing for a value that is not a list: an OTLP level a hostile or torn
    capture line gave another shape reads as empty instead of raising."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def otlp_attributes(value: object) -> dict[str, Any]:
    """`{key: unwrapped value}` for an OTLP attribute list, skipping any entry that is not an object with a
    string key; a missing or odd value reads as None."""
    return {a["key"]: _attr_value(a.get("value")) for a in dicts(value) if isinstance(a.get("key"), str)}


def sub_dict(value: dict[str, Any], key: str) -> dict[str, Any]:
    """`value[key]` when it is an object, else an empty dict."""
    inner = value.get(key)
    return inner if isinstance(inner, dict) else {}


# Largest value any numeric attribute may carry. A count, size, duration or percent
# above it is a corrupt or hostile record, not a measurement; a cost has its own, lower cap.
MAX_NUMBER = 1e15
MAX_COST_USD = 1e6

# Numbers that parsed but were refused (negative, non-finite, over the cap) since the last
# `reset_rejected()`. The report command prints a footer from it, so a dropped value is not silent.
_rejected = 0
# Fields refused through an `owner` (the dict a value was read from): (id(owner), field) -> owner. A
# report reads the same message field more than once (its prompt length, then its weight), and each
# read refuses it again, so a field is counted once per owner. The owner is kept here so its id is
# never reused by another dict while the run lasts.
_rejected_fields: dict[tuple[int, str], object] = {}


def reset_rejected() -> None:
    global _rejected  # noqa: PLW0603
    _rejected = 0
    _rejected_fields.clear()


def _count_rejected(owner: object | None, field: str) -> None:
    global _rejected  # noqa: PLW0603
    if owner is not None:
        key = (id(owner), field)
        if key in _rejected_fields:
            return
        _rejected_fields[key] = owner
    _rejected += 1


def rejected_note(source: str = "capture") -> str | None:
    """A footer line for the report if `as_number` refused any value, else None. `source`
    is what the report read: `capture` (the default) or `transcripts`, so the note blames the
    right file."""
    if not _rejected:
        return None
    if source == "captures":
        # A mirrored event is read once per capture, so this counts readings, not distinct values.
        return (
            f"{_rejected:,} numeric reading(s) across the captures were negative, not finite or implausibly large "
            "and were left out of every total above (an event the plugin mirrored is read once per capture); "
            "a capture is corrupt or was posted to by something else."
        )
    if source == "transcripts":
        return (
            f"{_rejected:,} numeric value(s) in the transcripts were negative, not finite or implausibly large "
            "and were left out of every total above; a transcript is corrupt or was edited by something else."
        )
    return (
        f"{_rejected:,} numeric value(s) in the capture were negative, not finite or implausibly large "
        "and were left out of every total above; the capture is corrupt or was posted to by something else."
    )


def as_number(value: object, cap: float = MAX_NUMBER, *, owner: object | None = None, field: str = "") -> float | None:
    """A numeric attribute, whether exported as a string (tool events), an int
    (proto3 JSON may string-encode those too) or a double; None if unreadable.

    Every attribute read through here (tokens, bytes, durations, percents, cost) is
    a non-negative quantity, so a negative, non-finite or over-`cap` value is refused
    as None and counted for `rejected_note`, instead of poisoning the totals. With `owner` (the
    dict the value came from) and `field`, a refusal is counted once per (owner, field) however
    often it is read."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str) and (not value.isascii() or "_" in value):
        return None  # float() would read "٣" or "1_0" as a number: an exporter writes neither
    try:
        number = float(value)  # type: ignore[arg-type]
    except OverflowError:  # an int too large for a float
        _count_rejected(owner, field)
        return None
    except (TypeError, ValueError):
        return None
    if math.isfinite(number) and 0 <= number <= cap:
        return number + 0.0  # -0.0 becomes 0.0
    _count_rejected(owner, field)
    return None


def as_cost(value: object) -> float | None:
    """`as_number` for a `cost_usd`, with the lower `MAX_COST_USD` cap."""
    return as_number(value, MAX_COST_USD)


def _event_name(record: Mapping[str, Any], attrs: Mapping[str, Any]) -> str | None:
    name = attrs.get("event.name")
    if isinstance(name, str):
        return name
    body = _attr_value(record.get("body"))
    if isinstance(body, str):
        return body.removeprefix("claude_code.")
    return None


def iter_log_events(payload: object) -> list[tuple[str | None, dict[str, Any]]]:
    """(event name, attributes) for every OTLP log record in a payload, with
    resource attributes merged underneath the record's own (as for metrics)."""
    out: list[tuple[str | None, dict[str, Any]]] = []
    if not isinstance(payload, dict):
        return out
    for resource in dicts(payload.get("resourceLogs")):
        resource_attrs = otlp_attributes(sub_dict(resource, "resource").get("attributes"))
        for scope in dicts(resource.get("scopeLogs")):
            for record in dicts(scope.get("logRecords")):
                attrs = {**resource_attrs, **otlp_attributes(record.get("attributes"))}
                out.append((_event_name(record, attrs), attrs))
    return out


@dataclasses.dataclass(frozen=True)
class ToolCall:
    """One `tool_result` event."""

    tool: str
    tool_use_id: str | None
    session: str | None
    prompt: str | None
    success: bool
    error_type: str | None
    duration_ms: float | None
    result_bytes: float | None
    timestamp: str | None


@dataclasses.dataclass(frozen=True)
class Decision:
    """One `tool_decision` event."""

    tool: str
    decision: str
    source: str
    tool_source: str


@dataclasses.dataclass
class ToolEvents:
    calls: list[ToolCall] = dataclasses.field(default_factory=list)
    decisions: list[Decision] = dataclasses.field(default_factory=list)
    # prompt.id -> [tokens, cost_usd], summed over that prompt's api_request events.
    prompt_usage: dict[str, list[float]] = dataclasses.field(default_factory=lambda: defaultdict(lambda: [0.0, 0.0]))


def encodable(text: str) -> str:
    """`text` with each lone UTF-16 surrogate (a JSON `\\ud83d` with no partner) replaced by U+FFFD, so a
    report can always be written as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    return text


def as_str(value: object) -> str | None:
    return None if value is None else encodable(str(value))


def load_events(capture: Path, project_filter: ProjectFilter | None = None) -> ToolEvents:
    """Every tool_result, tool_decision and api_request event in the capture
    that passes `project_filter` (any object with `keep(attrs) -> bool`)."""
    events = ToolEvents()
    for record in capture_records(capture):
        for name, attrs in iter_log_events(record.get("payload")):
            if name not in (TOOL_RESULT, TOOL_DECISION, API_REQUEST):
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            if name == TOOL_RESULT:
                events.calls.append(
                    ToolCall(
                        tool=str(attrs.get("tool_name") or "?"),
                        tool_use_id=as_str(attrs.get("tool_use_id")),
                        session=as_str(attrs.get("session.id")),
                        prompt=as_str(attrs.get("prompt.id")),
                        success=str(attrs.get("success")).lower() == "true",
                        error_type=as_str(attrs.get("error_type")),
                        duration_ms=as_number(attrs.get("duration_ms")),
                        result_bytes=as_number(attrs.get("tool_result_size_bytes")),
                        timestamp=as_str(attrs.get("event.timestamp")),
                    )
                )
            elif name == TOOL_DECISION:
                events.decisions.append(
                    Decision(
                        tool=str(attrs.get("tool_name") or "?"),
                        decision=str(attrs.get("decision") or "?"),
                        source=str(attrs.get("source") or "?"),
                        tool_source=str(attrs.get("tool_source") or "?"),
                    )
                )
            else:
                prompt = attrs.get("prompt.id")
                usage = events.prompt_usage[str(prompt) if prompt is not None else "?"]
                usage[0] += sum(as_number(attrs.get(key)) or 0.0 for key in API_TOKEN_KEYS)
                usage[1] += as_cost(attrs.get("cost_usd")) or 0.0
    return events


@dataclasses.dataclass
class ToolStats:
    calls: int = 0
    failures: int = 0
    sessions: set[str] = dataclasses.field(default_factory=set)
    prompts: set[str] = dataclasses.field(default_factory=set)
    errors: Counter[str] = dataclasses.field(default_factory=Counter)
    durations: list[float] = dataclasses.field(default_factory=list)
    result_bytes: list[float] = dataclasses.field(default_factory=list)
    attributed_tokens: float = 0.0
    attributed_cost: float = 0.0


@dataclasses.dataclass
class DecisionStats:
    decisions: int = 0
    accepts: int = 0
    rejects: int = 0
    sources: Counter[str] = dataclasses.field(default_factory=Counter)
    tool_sources: Counter[str] = dataclasses.field(default_factory=Counter)


@dataclasses.dataclass
class ToolSummary:
    tools: dict[str, ToolStats]
    decisions: dict[str, DecisionStats]
    calls: list[ToolCall]
    api_tokens: float
    api_cost: float
    unattributed_tokens: float
    unattributed_cost: float


def summarize(events: ToolEvents) -> ToolSummary:
    tools: dict[str, ToolStats] = defaultdict(ToolStats)
    calls_per_prompt: dict[str, Counter[str]] = defaultdict(Counter)
    for call in events.calls:
        stats = tools[call.tool]
        stats.calls += 1
        if call.session is not None:
            stats.sessions.add(call.session)
        if call.prompt is not None:
            stats.prompts.add(call.prompt)
            calls_per_prompt[call.prompt][call.tool] += 1
        if not call.success:
            stats.failures += 1
            stats.errors[call.error_type or "?"] += 1
        if call.duration_ms is not None:
            stats.durations.append(call.duration_ms)
        if call.result_bytes is not None:
            stats.result_bytes.append(call.result_bytes)

    # Call-share attribution: a prompt's API usage is split across the tools it
    # called in proportion to call count. Approximate by design -- the cost of a
    # turn is mostly cache reads of the whole context, not of any one result.
    unattributed_tokens = unattributed_cost = 0.0
    for prompt, (tokens, cost) in events.prompt_usage.items():
        mix = calls_per_prompt.get(prompt)
        if not mix:
            unattributed_tokens += tokens
            unattributed_cost += cost
            continue
        total = sum(mix.values())
        for tool, count in mix.items():
            tools[tool].attributed_tokens += tokens * count / total
            tools[tool].attributed_cost += cost * count / total

    decisions: dict[str, DecisionStats] = defaultdict(DecisionStats)
    for decision in events.decisions:
        decision_stats = decisions[decision.tool]
        decision_stats.decisions += 1
        decision_stats.accepts += decision.decision == "accept"
        decision_stats.rejects += decision.decision == "reject"
        decision_stats.sources[decision.source] += 1
        decision_stats.tool_sources[decision.tool_source] += 1

    return ToolSummary(
        tools=dict(tools),
        decisions=dict(decisions),
        calls=events.calls,
        api_tokens=sum(usage[0] for usage in events.prompt_usage.values()),
        api_cost=sum(usage[1] for usage in events.prompt_usage.values()),
        unattributed_tokens=unattributed_tokens,
        unattributed_cost=unattributed_cost,
    )


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least pct% of the
    values at or below it. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _pct(part: float, whole: float) -> str:
    return f"{(part / whole * 100) if whole else 0.0:.1f}%"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def _by_calls(tools: Mapping[str, ToolStats]) -> list[tuple[str, ToolStats]]:
    return sorted(tools.items(), key=lambda item: (-item[1].calls, item[0]))


def _calls_table(summary: ToolSummary) -> list[str]:
    lines = [
        "Tool calls (claude_code.tool_result)",
        (
            f"{'tool':<24} {'calls':>8} {'sessions':>8} {'calls/sess':>10} {'fail':>6} {'fail%':>6}"
            f" {'p50ms':>8} {'p95ms':>8} {'total_s':>9}"
        ),
        "-" * 94,
    ]
    every_session: set[str] = set()
    all_durations: list[float] = []
    for tool, stats in _by_calls(summary.tools):
        every_session |= stats.sessions
        all_durations += stats.durations
        per_session = stats.calls / len(stats.sessions) if stats.sessions else 0.0
        lines.append(
            f"{tool:<24} {stats.calls:>8,} {len(stats.sessions):>8,} {per_session:>10.1f}"
            f" {stats.failures:>6,} {_pct(stats.failures, stats.calls):>6}"
            f" {_ms(percentile(stats.durations, 50)):>8} {_ms(percentile(stats.durations, 95)):>8}"
            f" {sum(stats.durations) / 1000:>9,.1f}"
        )
    calls = sum(stats.calls for stats in summary.tools.values())
    failures = sum(stats.failures for stats in summary.tools.values())
    lines.append("-" * 94)
    lines.append(
        f"{'TOTAL':<24} {calls:>8,} {len(every_session):>8,}"
        f" {(calls / len(every_session)) if every_session else 0.0:>10.1f}"
        f" {failures:>6,} {_pct(failures, calls):>6}"
        f" {_ms(percentile(all_durations, 50)):>8} {_ms(percentile(all_durations, 95)):>8}"
        f" {sum(all_durations) / 1000:>9,.1f}"
    )
    return lines


def _errors_block(summary: ToolSummary) -> list[str]:
    failing = [(tool, stats) for tool, stats in _by_calls(summary.tools) if stats.errors]
    if not failing:
        return []
    lines = ["Failures by error_type (details: report --failures)"]
    for tool, stats in sorted(failing, key=lambda item: (-item[1].failures, item[0])):
        breakdown = ", ".join(f"{error} {count:,}" for error, count in stats.errors.most_common())
        lines.append(f"  {tool:<24} {breakdown}")
    return lines


def _context_table(summary: ToolSummary) -> list[str]:
    all_bytes = sum(sum(stats.result_bytes) for stats in summary.tools.values())
    lines = [
        "Context cost (result bytes per tool; prompt API usage split by call share)",
        f"{'tool':<24} {'result_KB':>10} {'avg_B':>9} {'bytes%':>7} {'prompts':>8} {'tokens':>15} {'cost_usd':>10}",
        "-" * 89,
    ]
    rows = sorted(
        summary.tools.items(), key=lambda item: (-item[1].attributed_cost, -sum(item[1].result_bytes), item[0])
    )
    for tool, stats in rows:
        total = sum(stats.result_bytes)
        mean = total / len(stats.result_bytes) if stats.result_bytes else 0.0
        lines.append(
            f"{tool:<24} {total / 1024:>10,.1f} {mean:>9,.0f} {_pct(total, all_bytes):>7}"
            f" {len(stats.prompts):>8,} {stats.attributed_tokens:>15,.0f} {stats.attributed_cost:>10,.2f}"
        )
    lines.append(
        f"{NO_TOOL_CALLS:<24} {'':>10} {'':>9} {'':>7} {'':>8}"
        f" {summary.unattributed_tokens:>15,.0f} {summary.unattributed_cost:>10,.2f}"
    )
    lines.append("-" * 89)
    lines.append(
        f"{'TOTAL':<24} {all_bytes / 1024:>10,.1f} {'':>9} {'':>7} {'':>8}"
        f" {summary.api_tokens:>15,.0f} {summary.api_cost:>10,.2f}"
    )
    lines.append(
        "tokens = input + output + cacheRead + cacheCreation of claude_code.api_request; each prompt's usage is split"
    )
    lines.append("across its tool calls by call count -- an approximation, not a per-tool measurement.")
    return lines


def _source_columns(decisions: Mapping[str, DecisionStats]) -> list[str]:
    seen: set[str] = set()
    for stats in decisions.values():
        seen.update(stats.sources)
    return [s for s in KNOWN_SOURCES if s in seen] + sorted(seen - set(KNOWN_SOURCES))


def _user_prompted(stats: DecisionStats) -> int:
    return sum(count for source, count in stats.sources.items() if source.startswith("user_"))


def _decisions_table(summary: ToolSummary) -> list[str]:
    sources = _source_columns(summary.decisions)
    widths = [max(len(s), 6) for s in sources]
    header = f"{'tool':<24} {'decisions':>9} {'accept':>8} {'reject':>6}"
    header += "".join(f" {s:>{w}}" for s, w in zip(sources, widths, strict=False)) + f" {'user%':>6}"
    lines = ["Permission decisions (claude_code.tool_decision)", header, "-" * len(header)]
    total = DecisionStats()
    rows = sorted(summary.decisions.items(), key=lambda item: (-item[1].decisions, item[0]))
    for tool, stats in [*rows, ("TOTAL", total)]:
        if tool == "TOTAL":
            lines.append("-" * len(header))
        else:
            total.decisions += stats.decisions
            total.accepts += stats.accepts
            total.rejects += stats.rejects
            total.sources.update(stats.sources)
        lines.append(
            f"{tool:<24} {stats.decisions:>9,} {stats.accepts:>8,} {stats.rejects:>6,}"
            + "".join(f" {stats.sources.get(s, 0):>{w},}" for s, w in zip(sources, widths, strict=False))
            + f" {_pct(_user_prompted(stats), stats.decisions):>6}"
        )
    lines.append("user% = decisions whose source is user_* (the user was asked) -- candidates for the allow-list.")
    return lines


def _mcp_note(summary: ToolSummary) -> list[str]:
    if MCP_BUCKET not in summary.tools and MCP_BUCKET not in summary.decisions:
        return []
    note = (
        f"{MCP_BUCKET} is Claude Code's redacted bucket for every mcp__* tool "
        "(names need OTEL_LOG_TOOL_DETAILS; report --failures shows them from transcripts)."
    )
    stats = summary.decisions.get(MCP_BUCKET)
    if stats is not None:
        note += (
            " Decisions by "
            + ", ".join(f"tool_source {source} {count:,}" for source, count in stats.tool_sources.most_common())
            + "."
        )
    return [note]


def session_label(session: str | None) -> str:
    return (session or "?")[:SESSION_PREFIX]


def when_label(timestamp: str | None) -> str:
    """`2026-10-01T09:06:00.000Z` -> `2026-10-01 09:06`."""
    if not timestamp:
        return "?"
    return timestamp.replace("T", " ")[:16]


def _sessions_table(summary: ToolSummary, limit: int) -> list[str]:
    per_session: dict[str, list[ToolCall]] = defaultdict(list)
    for call in summary.calls:
        per_session[call.session or "?"].append(call)
    last_seen = {
        session: max((call.timestamp or "" for call in calls), default="") for session, calls in per_session.items()
    }
    lines = [
        "Tool calls per session (most recent first)",
        f"{'session':<8} {'last_event':<16} {'calls':>7} {'fail':>5}  top tools",
        "-" * 80,
    ]
    for session in sorted(per_session, key=lambda s: last_seen[s], reverse=True)[:limit]:
        calls = per_session[session]
        mix = Counter(call.tool for call in calls)
        top = ", ".join(
            f"{tool} {count:,}" for tool, count in sorted(mix.items(), key=lambda item: (-item[1], item[0]))[:3]
        )
        failures = sum(not call.success for call in calls)
        lines.append(
            f"{session_label(session):<8} {when_label(last_seen[session] or None):<16} {len(calls):>7,}"
            f" {failures:>5,}  {top}"
        )
    if len(per_session) > limit:
        lines.append(f"... {len(per_session) - limit:,} more sessions (raise --limit)")
    return lines


def format_tool_report(summary: ToolSummary, by_session: bool = False, limit: int | None = None) -> str:
    if not summary.tools and not summary.decisions:
        return "No claude_code.tool_result or tool_decision events found."
    blocks = [_calls_table(summary), _errors_block(summary)]
    if by_session:
        blocks.append(_sessions_table(summary, limit or DEFAULT_SESSION_LIMIT))
    blocks += [_context_table(summary), _decisions_table(summary), _mcp_note(summary)]
    return "\n\n".join("\n".join(block) for block in blocks if block)


# --- failure drill-down ----------------------------------------------------------

# Input keys worth showing for a failed call, most telling first; anything else
# falls back to the compact JSON of the whole input.
INPUT_KEYS = ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "skill", "description")


def transcript_dirs(project_dir: Path | None) -> list[Path]:
    """The transcript directories the drill-down may read: the project's own
    and its `.claude/worktrees/*` siblings, the set the project filter uses.
    None (a label no project root owns) reads nothing.
    The one place to narrow if per-project isolation needs it."""
    if project_dir is None:
        return []
    return [project_dir, *sorted(project_dir.parent.glob(f"{project_dir.name}--claude-worktrees-*"))]


def one_line(text: str, width: int) -> str:
    """Whitespace collapsed to single spaces, cut to `width` characters with a
    trailing ellipsis when longer."""
    flat = " ".join("".join(c for c in text if c.isspace() or c.isprintable()).split())  # no escape reaches a terminal
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def summarize_input(tool_input: Mapping[str, object]) -> str:
    for key in INPUT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return one_line(value, INPUT_WIDTH)
    return one_line(json.dumps(tool_input, sort_keys=True), INPUT_WIDTH)


def join_transcripts(
    calls: list[ToolCall], dirs: list[Path]
) -> dict[str, tuple[ti.ToolUseEvent | None, ti.ToolResultEvent | None]]:
    """tool_use_id -> (transcript tool_use, transcript tool_result) for `calls`,
    parsing only transcripts of sessions that appear in `calls`."""
    wanted = {call.tool_use_id for call in calls if call.tool_use_id}
    sessions = {call.session for call in calls if call.session}
    found: dict[str, list[Any]] = {}
    for directory in dirs:
        for context in ti.discover_transcripts(directory):
            if context.session_id not in sessions:
                continue
            for event in ti.iter_events(context):
                if isinstance(event, (ti.ToolUseEvent, ti.ToolResultEvent)) and event.tool_use_id in wanted:
                    slot = found.setdefault(event.tool_use_id, [None, None])
                    slot[0 if isinstance(event, ti.ToolUseEvent) else 1] = event
    return {use_id: (pair[0], pair[1]) for use_id, pair in found.items()}


def _failure_lines(
    call: ToolCall, joined: Mapping[str, tuple[ti.ToolUseEvent | None, ti.ToolResultEvent | None]]
) -> list[str]:
    prefix = f"  {when_label(call.timestamp):<16}  {session_label(call.session):<8}  "
    use, result = joined.get(call.tool_use_id or "", (None, None))
    if use is None and result is None:
        return [prefix + "(no transcript match)"]
    lines = []
    if use is not None:
        name = f"{use.tool_name}: " if use.tool_name != call.tool else ""
        lines.append(prefix + name + summarize_input(use.tool_input))
    else:
        lines.append(prefix + "(tool input not in transcript)")
    text = result.result_text if result is not None else None
    lines.append("      " + (one_line(text, ERROR_WIDTH) if text else "(no result text in transcript)"))
    return lines


def format_failures(events: ToolEvents, project_dir: Path | None, limit: int | None = None) -> str:
    """Failed calls grouped by (tool, error_type), most frequent group first,
    each with its `limit` most recent failures joined to the transcripts."""
    groups: dict[tuple[str, str], list[ToolCall]] = defaultdict(list)
    for call in events.calls:
        if not call.success:
            groups[(call.tool, call.error_type or "?")].append(call)
    if not groups:
        return "No failed claude_code.tool_result events found."

    limit = limit or DEFAULT_FAILURE_LIMIT
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    shown = {key: sorted(calls, key=lambda call: call.timestamp or "", reverse=True)[:limit] for key, calls in ordered}
    joined = join_transcripts([call for calls in shown.values() for call in calls], transcript_dirs(project_dir))

    lines = [
        "Recent tool failures (input and error text from this project's transcripts, truncated)",
    ]
    for (tool, error_type), calls in ordered:
        count = len(calls)
        lines.append("")
        lines.append(f"{tool} / {error_type} -- {count:,} failure{'s' if count != 1 else ''}")
        for call in shown[(tool, error_type)]:
            lines += _failure_lines(call, joined)
        if count > limit:
            lines.append(f"  ... {count - limit:,} older (raise --limit)")
    return "\n".join(lines)
