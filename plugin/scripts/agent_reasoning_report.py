#!/usr/bin/env python3
"""Reasoning (extended thinking) tokens for `agent_telemetry.py report --reasoning`.

Reasoning is billed as output tokens, and OTel does not split it out:
`claude_code.api_request` and `claude_code.token.usage` carry input, output,
cache-read and cache-creation tokens only. Transcripts do: each assistant
message's `usage.output_tokens_details.thinking_tokens` is the part of its
`output_tokens` spent on reasoning. This view reads those counts (never the
thinking text, which is mostly stored empty anyway) from the project's
transcripts, per model, subagent (`main` or the subagent type) and session.

Effort comes from the capture: an assistant message's `requestId` equals the
`request_id` of its `claude_code.api_request` event, which carries `effort` and
`cost_usd`. The effort block says what a level spends; whether its answers were
better is not measured here.

The reasoning cost is an estimate: reasoning tokens at the model's output
price, five times its input price (checked against a captured `cost_usd`).
"""

from __future__ import annotations

import dataclasses
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tool_report as tr
import transcript_iterator as ti
from agent_iteration_report import MAIN, format_table
from model_prices import OUTPUT_PRICE_FACTOR, model_prices, prompt_tokens, unpriced_footer
from safe_name import subagent_label

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from agent_telemetry import ProjectFilter

DEFAULT_SESSION_LIMIT = 20
# A session is flagged when reasoning is at least this share of its output and at least this many tokens.
DOMINATES_SHARE = 0.5
DOMINATES_MIN_TOKENS = 1000
UNKNOWN = "unknown"
UNKNOWN_SUBAGENT = "?"
NO_VALUE = "-"
TOTAL_UNIT = "of reasoning tokens priced"


@dataclasses.dataclass(frozen=True)
class Message:
    """One assistant message's counts. Nothing but numbers and names."""

    session: str
    subagent: str
    model: str
    output: int
    thinking: int
    request_id: str | None
    timestamp: str
    prompt_tokens: float = 0.0  # input + cache read + cache write: picks the rate card


@dataclasses.dataclass(frozen=True)
class RequestInfo:
    effort: str | None
    cost: float | None


@dataclasses.dataclass
class ReasoningReport:
    messages: list[Message] = dataclasses.field(default_factory=list)
    requests: dict[str, RequestInfo] = dataclasses.field(default_factory=dict)  # request_id -> effort, cost
    without_counts: int = 0  # messages whose usage has no thinking_tokens


# --- loading ---------------------------------------------------------------------


def _count(value: object) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = tr.as_number(value)  # refuses negative, non-finite and huge counts, and counts the refusal
    return None if number is None else int(number)


def _thinking_tokens(usage: object) -> tuple[int, int] | None:
    """(output_tokens, thinking_tokens) of a message's usage, or None without a thinking count."""
    if not isinstance(usage, dict):
        return None
    details = usage.get("output_tokens_details")
    output = _count(usage.get("output_tokens"))
    thinking = _count(details.get("thinking_tokens")) if isinstance(details, dict) else None
    if output is None or thinking is None:
        return None
    return output, thinking


def _is_count(value: object) -> bool:
    """`_count(value) is not None` without `tr.as_number`'s side effect: ranking looks at every copy
    of a message, and a refusal counted per look would inflate the footer."""
    # NaN and infinity fail the range test; a huge int compares without converting to float
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= tr.MAX_NUMBER


def _has_thinking_counts(event: ti.AssistantMessageEvent) -> bool:
    """Whether `_thinking_tokens` would read this copy, decided without counting a refusal."""
    usage = event.usage
    if not isinstance(usage, dict):
        return False
    details = usage.get("output_tokens_details")
    return (
        isinstance(details, dict)
        and _is_count(usage.get("output_tokens"))
        and _is_count(details.get("thinking_tokens"))
    )


def _directories(project_dir: Path | None, all_projects: bool) -> list[Path]:
    """The project's transcript directories: its own and the siblings of sessions started in its
    `.claude/worktrees/*` (as the project filter does); every project's when `all_projects`."""
    if project_dir is None:
        return []  # a label no project root owns has no transcripts
    if all_projects:
        return (
            sorted(path for path in project_dir.parent.iterdir() if path.is_dir())
            if project_dir.parent.is_dir()
            else []
        )
    return [project_dir, *sorted(project_dir.parent.glob(f"{project_dir.name}--claude-worktrees-*"))]


def read_messages(directories: Iterable[Path], report: ReasoningReport) -> None:
    """Every assistant message of the directories' transcripts. A response repeated across files
    (same message id and requestId) is counted once, from the file holding its best copy, and a
    copy carrying thinking counts outranks one without them. Files are read one at a time, twice:
    only the key -> owner table is held across files."""
    contexts = [context for directory in directories for context in ti.discover_transcripts(directory)]
    for context, events in ti.stream_owned_across_files(contexts, usable=_has_thinking_counts):
        subagent = MAIN if context.kind == "main" else subagent_label(context.agent_type, UNKNOWN_SUBAGENT)
        for event in events:
            counts = _thinking_tokens(event.usage)
            if counts is None:
                report.without_counts += 1
                continue
            report.messages.append(
                Message(
                    session=context.session_id,
                    subagent=subagent,
                    model=event.model or "?",
                    output=counts[0],
                    thinking=counts[1],
                    request_id=event.request_id,
                    timestamp=event.timestamp or "",
                    prompt_tokens=prompt_tokens(event.usage) if isinstance(event.usage, dict) else 0.0,
                )
            )


def _records(capture: Path) -> Iterable[dict[str, Any]]:
    yield from tr.capture_records(capture)


def read_requests(capture: Path, project_filter: ProjectFilter | None, report: ReasoningReport) -> None:
    for record in _records(capture):
        for name, attrs in tr.iter_log_events(record.get("payload")):
            if name != tr.API_REQUEST:
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            request_id = tr.as_str(attrs.get("request_id"))
            if request_id:
                report.requests[request_id] = RequestInfo(
                    tr.as_str(attrs.get("effort")), tr.as_cost(attrs.get("cost_usd"))
                )


def build_report(
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
    all_projects: bool = False,
) -> ReasoningReport:
    report = ReasoningReport()
    read_requests(capture, project_filter, report)
    read_messages(_directories(project_dir, all_projects), report)
    return report


# --- formatting ------------------------------------------------------------------


@dataclasses.dataclass
class Totals:
    messages: int = 0
    output: int = 0
    thinking: int = 0
    cost: float = 0.0
    priced_messages: int = 0
    priced_thinking: int = 0
    request_cost: float = 0.0
    request_costed: int = 0
    unpriced_models: dict[str, int] = dataclasses.field(default_factory=dict)  # model -> requests

    def add(self, message: Message, request: RequestInfo | None = None) -> None:
        self.messages += 1
        self.output += message.output
        self.thinking += message.thinking
        prices = model_prices(message.model, message.prompt_tokens)
        if prices is None:
            self.unpriced_models[message.model] = self.unpriced_models.get(message.model, 0) + 1
        else:
            self.priced_messages += 1
            self.priced_thinking += message.thinking
            self.cost += message.thinking * prices[0] * OUTPUT_PRICE_FACTOR / 1_000_000
        if request is not None and request.cost is not None:
            self.request_cost += request.cost
            self.request_costed += 1


def _int(value: float) -> str:
    return f"{round(value):,}"


def _share(thinking: int, output: int) -> str:
    return f"{thinking / output:.0%}" if output else NO_VALUE


def _priced_share(totals: Totals) -> str | None:
    """Share of reasoning tokens with a known price, or None when all of them have one."""
    if totals.priced_thinking == totals.thinking:
        return None
    share = totals.priced_thinking / totals.thinking
    if share < 0.01:
        return "<1%"
    return ">99%" if share >= 0.995 else f"{share:.0%}"


def _cost(totals: Totals, unit: str = "priced") -> str:
    """The priced part of the reasoning cost, with its share of the tokens when some are unpriced."""
    if not totals.priced_messages:
        return NO_VALUE
    share = _priced_share(totals)
    return f"${totals.cost:.2f}" if share is None else f"${totals.cost:.2f} ({share} {unit})"


def _average_cost(value: float, count: int) -> str:
    return f"${value / count:.4f}" if count else NO_VALUE


def _effort_average_cost(totals: Totals) -> str:
    """Average reasoning cost over the priced messages, noting how many of the messages that was."""
    average = _average_cost(totals.cost, totals.priced_messages)
    if totals.priced_messages in (0, totals.messages):
        return average
    return f"{average} ({totals.priced_messages:,} of {totals.messages:,} priced)"


def _group(report: ReasoningReport, key: Callable[[Message], str]) -> dict[str, Totals]:
    out: dict[str, Totals] = defaultdict(Totals)
    for message in report.messages:
        out[key(message)].add(message, report.requests.get(message.request_id or ""))
    return out


def _by_output(totals: dict[str, Totals]) -> list[str]:
    return sorted(totals, key=lambda k: (-totals[k].thinking, -totals[k].output, k))


def _block(
    title: str,
    label: str,
    report: ReasoningReport,
    key: Callable[[Message], str],
    flag: bool = False,
    limit: int | None = None,
) -> list[str]:
    totals = _group(report, key)
    keys = _by_output(totals)
    note = ""
    if limit is not None:
        latest: dict[str, str] = {}
        for message in report.messages:
            latest[key(message)] = max(latest.get(key(message), ""), message.timestamp)
        keys = sorted(totals, key=lambda k: (latest[k], k), reverse=True)
        if len(keys) > limit:
            note = f"; {len(keys) - limit:,} older not shown, raise --limit"
            keys = keys[:limit]
    rows = []
    for name in keys:
        t = totals[name]
        row = [name, f"{t.messages:,}", _int(t.output), _int(t.thinking), _share(t.thinking, t.output), _cost(t)]
        if flag:
            dominates = t.output and t.thinking / t.output >= DOMINATES_SHARE and t.thinking >= DOMINATES_MIN_TOKENS
            row.append("!" if dominates else "")
        rows.append(row)
    header = [label, "msgs", "output", "reasoning", "share", "reasoning$"] + (["flag"] if flag else [])
    return [title + note, *format_table(header, rows, left=1)]


def _effort_block(report: ReasoningReport) -> list[str]:
    totals: dict[str, Totals] = defaultdict(Totals)
    for message in report.messages:
        request = report.requests.get(message.request_id or "")
        totals[(request.effort if request else None) or UNKNOWN].add(message, request)
    rows = []
    for effort in sorted(totals, key=lambda e: (e == UNKNOWN, -totals[e].messages, e)):
        t = totals[effort]
        rows.append(
            [
                effort,
                f"{t.messages:,}",
                _int(t.output / t.messages),
                _int(t.thinking / t.messages),
                _share(t.thinking, t.output),
                _effort_average_cost(t),
                _average_cost(t.request_cost, t.request_costed),
            ]
        )
    header = ["effort", "msgs", "avg output", "avg reasoning", "share", "avg reasoning$", "avg cost"]
    return [
        "By effort (per message; effort and cost from the matching api_request, unknown without one)",
        *format_table(header, rows, left=1),
        "  This shows what a level spends, not whether its answers were better.",
    ]


def format_report(report: ReasoningReport, limit: int | None = None) -> str:
    if not report.messages:
        if report.without_counts:
            return f"No assistant messages with thinking counts found ({report.without_counts:,} messages without)."
        return "No assistant messages found."
    total = Totals()
    for message in report.messages:
        total.add(message)
    blocks = [
        [
            (
                "Reasoning tokens (thinking, billed inside output tokens) from transcripts; reasoning$ estimates "
                "reasoning at the model's output price"
            ),
            (
                f"  messages {total.messages:,}, output {total.output:,}, reasoning {total.thinking:,} "
                f"({_share(total.thinking, total.output)}), est. reasoning cost {_cost(total, TOTAL_UNIT)}"
            ),
        ],
        _block("By model", "model", report, lambda m: m.model),
        _block("By subagent (agent type: main session or subagent type)", "subagent", report, lambda m: m.subagent),
        _block(
            f"By session (most recent first; ! = reasoning >= {DOMINATES_SHARE:.0%} of output and >= "
            f"{DOMINATES_MIN_TOKENS:,} tokens)",
            "session",
            report,
            lambda m: tr.session_label(m.session),
            flag=True,
            limit=limit or DEFAULT_SESSION_LIMIT,
        ),
        _effort_block(report),
    ]
    notes = []
    if report.without_counts:
        notes.append(f"  {report.without_counts:,} messages without thinking counts (left out)")
    if notes:
        blocks.append(["Notes", *notes])
    footer = unpriced_footer(total.unpriced_models)
    if footer is not None:
        blocks.append([footer])
    return "\n\n".join("\n".join(block) for block in blocks)
