#!/usr/bin/env python3
"""Transcript-derived agent usage report.

Read-only CLI deriving per-ticket, per-role (`agentType`), per-model, and
per-dispatch-shape (`fork` vs `fresh` vs `main-session`) usage totals from
`~/.claude/projects/<project-slug>/` transcripts and its `<slug>--claude-worktrees-*` sibling folders (default: the cwd's project), so
this is the one authoritative "what did this ticket/role/model/dispatch
shape cost" answer instead of hand-kept notes.

Reuses `scripts/transcript_iterator.py`'s `iter_project_events` (already
message-id-deduplicated) as its only transcript walker --
this module parses no JSONL itself. It does read each subagent's own
`.meta.json` a second time (for its `description` field, which
`transcript_iterator.TranscriptContext` deliberately does not expose,
staying navigation-agnostic per its own module docstring), and its first
user message via `transcript_iterator.read_jsonl` (the dispatch prompt).

Ticket attribution: a reference is `#(\\d+)` or `ticket (#)?(\\d+)`, case-insensitive
(not fixed-width -- kanban IDs have no fixed width), searched in order in a
subagent's `.meta.json` `description`, then its dispatch prompt (the first user
message of its transcript); the first match wins, so the description beats the
prompt. Real descriptions say "Implement ticket 5 ..." while the `#N` sits in
the prompt. A dispatch with no match in either, or a main-session transcript
(no `.meta.json` at all), is attributed to a literal `unattributed` bucket --
never dropped, never folded into another ticket's numbers.

Dispatch shape: `fork` when `.meta.json`'s `agentType` is exactly `"fork"`,
`fresh` for any other subagent `agentType`, `main-session` for a
main-session transcript.

Weighting: each deduplicated assistant message contributes a
sonnet-input-equivalent figure,
`(input + W_write*cacheCreation + 5*output) * W_in + R*cacheRead`.
W_in is the model's input price relative to Sonnet 5.5's and R its cache-read
price over Sonnet 5.5's input price (shared table, model_prices.py: W_in Opus
5.5 2.0, Haiku 4.5 0.5, Fable 5.1 5.0; R Sonnet 5.5 0.05, Opus 5.5 0.1, Opus 5 0.25,
Fable 5.1 0.125). A row with a prompt-length rate card (Haiku 5.5: W_in 0.05 and R 0.005 for
a prompt of at most 100,000 tokens, 0.25 and 0.025 over) is weighed per message by that
message's prompt length: input + cache read + cache writes (model_prices.py). Cache writes are priced per TTL from the transcript's
`usage.cache_creation` split: 1.25 for 5-minute writes, 2 for 1-hour writes. A
write the split does not cover (no split in the usage block) is priced at 1.25
and counted under "Cache writes without a TTL split", so the totals are a lower
bound while that section is present. A fast-mode message (`usage.speed` "fast", Opus 5.5 / 5 / 4.8) weighs 2x and each
web search request adds 5000 (model_prices.py). A model the table lacks (an older
model, a future id, a missing model) is never guessed at: its messages are
counted apart as "unpriced models" (requests and raw tokens per model id) and
add nothing to any grouping or the grand total, so the totals say so rather
than silently under-count. Read from that message's own `model` field -- ticket/role/dispatch-shape are
transcript-level attributes, but a message's *model* is read per message,
since one transcript can carry messages from more than one model.

Usage:
    uv run --locked python scripts/agent_usage_report.py \\
        [--project-dir DIR | --project-root DIR] [--since 2026-09-19] [--until 2026-09-27] [--json]
        [--context --budget 150000]

`--context` swaps the usage totals for a per-session context profile: turns, first/peak context (`input + cache_read + cache_creation` of
one message), the first turn index over the budget, turns and eq above it,
with role/ticket roll-ups. Same transcripts, attribution and filters.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tool_report as tr
import transcript_iterator as ti
from model_prices import (
    OUTPUT_PRICE_FACTOR,
    cache_read_equivalent,
    cache_write_equivalent,
    input_weight,
    model_label,
    prompt_tokens,
    speed_factor,
    unpriced_footer,
    usage_tokens,
    web_search_equivalent,
)
from project_groups import slug_of

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


def default_project_dir(cwd: Path | None = None) -> Path:
    """The `~/.claude/projects/<slug>` dir for `cwd`; the slug is Claude Code's, see
    `project_groups.claude_slug` (non-alphanumerics to `-` per UTF-16 unit, long paths truncated and hashed)."""
    return Path.home() / ".claude" / "projects" / slug_of(cwd or Path.cwd())


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to aware UTC (naive = local), else None."""
    if not value:
        return None
    try:
        # Python 3.10's fromisoformat rejects a trailing Z (3.11 added it); the project supports 3.10.
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):  # OverflowError: year 1 or 9999 shifted out of range
        return None


UNATTRIBUTED: Final[str] = "unattributed"
UNKNOWN_MODEL_FAMILY: Final[str] = "unknown"
NO_MODEL: Final[str] = "(no model)"

# The closed set of model families, matched by substring against a real model
# string (e.g. "claude-opus-5[1m]"). None of the five names is a substring of
# another, so match order does not matter; `model_family` keys off this tuple.
# Every family with a row in `model_prices.MODEL_PRICES` belongs here, or its
# priced usage would group as `unknown`.
_MODEL_FAMILIES: Final[tuple[str, ...]] = ("opus", "sonnet", "haiku", "fable", "mythos")


def model_family(model: str | None) -> str:
    """The model family read by substring match against `_MODEL_FAMILIES`,
    or `UNKNOWN_MODEL_FAMILY` when `model` is absent or matches none."""
    if not model:
        return UNKNOWN_MODEL_FAMILY
    lowered = model.lower()
    for family in _MODEL_FAMILIES:
        if family in lowered:
            return family
    return UNKNOWN_MODEL_FAMILY


def is_priced(model: str | None) -> bool:
    """Whether the shared price table has a row for `model`."""
    return input_weight(model) is not None and cache_read_equivalent(model) is not None


def eq_weight(usage: Mapping[str, object] | None, model: str | None) -> float:
    """The sonnet-input-equivalent figure for one deduplicated assistant
    message: `(input + writes + 5*output) * W_in + R*cacheRead` with W_in the
    model's input price relative to Sonnet 5.5's, R its cache-read price over
    Sonnet 5.5's input price, and `writes` the cache-creation tokens priced per
    TTL (1.25x 5-minute, 2x 1-hour; the shared price table). A model with no price
    row is not guessed: it weighs 0.0 here, and callers count it apart via
    `is_priced`. `usage=None` (a message with no usage block) contributes
    0.0, never raises."""
    if usage is None:
        return 0.0
    length = prompt_tokens(usage)  # picks the rate card of a model with a prompt-length threshold
    input_multiplier = input_weight(model, length)
    read_equivalent = cache_read_equivalent(model, length)
    if input_multiplier is None or read_equivalent is None:
        return 0.0
    writes, _ = cache_write_equivalent(usage)
    input_part = (
        usage_tokens(usage, "input_tokens") + writes + OUTPUT_PRICE_FACTOR * usage_tokens(usage, "output_tokens")
    )
    tokens = input_part * input_multiplier + read_equivalent * usage_tokens(usage, "cache_read_input_tokens")
    return tokens * speed_factor(usage, model) + web_search_equivalent(usage)


def _raw_tokens(usage: Mapping[str, object]) -> int:
    """Every token of one message, unweighted: input + cache write + cache read + output."""
    return int(
        sum(
            usage_tokens(usage, key)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
        )
    )


def _count_unpriced(unpriced: dict[str, dict[str, int]], model: str | None, usage: Mapping[str, object]) -> None:
    tokens = _raw_tokens(usage)
    if tokens == 0:
        return  # Claude Code's `<synthetic>` local-error messages carry zero usage: not a request
    entry = unpriced.setdefault(model or NO_MODEL, {"requests": 0, "tokens": 0})
    entry["requests"] += 1
    entry["tokens"] += tokens


def _count_unsplit(unsplit: dict[str, int], usage: Mapping[str, object]) -> None:
    """Counts a priced message whose cache writes had to be assumed 5-minute."""
    _, assumed = cache_write_equivalent(usage)
    if assumed > 0:
        unsplit["requests"] += 1
        unsplit["tokens"] += int(assumed)


def dispatch_shape(context: ti.TranscriptContext) -> str:
    """`main-session` for a main-session transcript (no `.meta.json`
    possible), `fork` when `.meta.json`'s `agentType` is exactly `"fork"`,
    `fresh` for any other subagent `agentType` (including a missing/
    unreadable one)."""
    if context.kind == "main":
        return "main-session"
    return "fork" if context.agent_type == "fork" else "fresh"


def role_of(context: ti.TranscriptContext) -> str:
    """`main-session` for a main-session transcript, else its `.meta.json`
    `agentType` verbatim, or `unknown-subagent` when that field is missing."""
    if context.kind == "main":
        return "main-session"
    return context.agent_type or "unknown-subagent"


def meta_description(context: ti.TranscriptContext) -> str | None:
    """The `.meta.json` `description` field paired with a subagent
    transcript, or `None` for a main-session transcript (no `.meta.json`
    exists) or when the field is missing/unreadable. `TranscriptContext`
    deliberately does not carry this field (see the module docstring), so
    this re-derives the meta path the same way
    `transcript_iterator.discover_transcripts` does."""
    if context.kind != "subagent":
        return None
    meta_path = context.transcript_path.with_suffix("").with_suffix(".meta.json")
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):  # unreadable, invalid UTF-8, invalid or too deeply nested JSON
        return None
    description = meta.get("description") if isinstance(meta, dict) else None
    return description if isinstance(description, str) else None


def _dispatch_prompt(context: ti.TranscriptContext) -> str | None:
    """The text of a subagent transcript's first user message (a bare
    string, or the `text` blocks of a content list), or `None` for a
    main-session transcript or when there is no such message. A main
    session's first prompt is the user's own, never a ticket dispatch."""
    if context.kind != "subagent":
        return None
    from agent_ticket_report import real_prompt_text  # noqa: PLC0415 -- cycle, see ticket_of

    for record in ti.read_jsonl(context.transcript_path):
        if record.get("type") != "user":
            continue
        message = record.get("message")
        return real_prompt_text(message.get("content") if isinstance(message, dict) else None)
    return None


def ticket_of(description: str | None, prompt: str | None = None) -> str:
    """The ticket a dispatch names, as a plain digit string, or `UNATTRIBUTED`.

    The description is tried first, then the dispatch prompt (only where it has no
    ticket of its own). Both go through `agent_ticket_report.explicit_ticket`, the one
    rule of every report: a text names a ticket only by `task/N-`, `ticket N`,
    `kanban N` or a bare `#N` (not `C#1`, `&#39;`, a hex colour or more than five
    digits), exactly one ticket per text, and a prompt only at its start. A text that
    lists several tickets is unattributed. The import is local because
    `agent_ticket_report` imports this module through `agent_iteration_report`."""
    from agent_ticket_report import explicit_ticket  # noqa: PLC0415 -- cycle

    return explicit_ticket(description, anchored=False) or explicit_ticket(prompt, anchored=True) or UNATTRIBUTED


def ticket_of_dispatch(context: ti.TranscriptContext) -> str:
    """Description first; the prompt is read only when it has no reference.
    Shared by the usage and the iteration report."""
    ticket = ticket_of(meta_description(context))
    if ticket != UNATTRIBUTED:
        return ticket
    return ticket_of(None, _dispatch_prompt(context))


def _first_assistant_timestamp(
    events: tuple[ti.AssistantMessageEvent, ...],
) -> datetime | None:
    for event in events:
        parsed = parse_timestamp(event.timestamp)
        if parsed is not None:
            return parsed
    return None


def _within_range(timestamp: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    """Inclusive `--since`/`--until` filtering against a transcript's first
    event timestamp. A transcript with no parseable timestamp is always
    included -- a filter can only ever exclude what it can actually
    compare."""
    if timestamp is None:
        return True
    if since is not None and timestamp < since:
        return False
    return not (until is not None and timestamp > until)


@dataclasses.dataclass(frozen=True)
class UsageReport:
    """The full grouped rollup: one float total per label in each of the
    four groupings, plus the grand total across all dispatches. Every
    grouping is exhaustive over the transcripts considered -- a ticket
    grouping always includes `UNATTRIBUTED` when at least one dispatch
    could not be attributed."""

    by_ticket: Mapping[str, float]
    by_role: Mapping[str, float]
    by_model: Mapping[str, float]
    by_dispatch_shape: Mapping[str, float]
    grand_total: float
    # model id (or NO_MODEL) -> {"requests", "tokens"} for models with no price
    # row; their weight is in none of the groupings or the grand total.
    unpriced: Mapping[str, Mapping[str, int]] = dataclasses.field(default_factory=dict)
    # Priced messages whose cache writes carried no TTL split: {"requests", "tokens"}
    # of writes priced at the 5-minute rate, so the totals are a lower bound.
    cache_write_unsplit: Mapping[str, int] = dataclasses.field(default_factory=lambda: {"requests": 0, "tokens": 0})

    def as_dict(self) -> dict[str, object]:
        return {
            "by_ticket": dict(self.by_ticket),
            "by_role": dict(self.by_role),
            "by_model": dict(self.by_model),
            "by_dispatch_shape": dict(self.by_dispatch_shape),
            "grand_total": self.grand_total,
            "unpriced": {model: dict(counts) for model, counts in self.unpriced.items()},
            "cache_write_unsplit": dict(self.cache_write_unsplit),
        }


@dataclasses.dataclass(frozen=True)
class TranscriptRef:
    """One transcript's attribution, fixed for every message in it."""

    path: str
    ticket: str
    role: str
    shape: str
    meta_model: str | None


def project_transcript_dirs(project_dir: Path) -> list[Path]:
    """`project_dir` and its `.claude/worktrees/*` sibling folders, the set the
    other reports read (`agent_tool_report.transcript_dirs`)."""
    return tr.transcript_dirs(project_dir)


def discover_project_transcripts(project_dir: Path) -> tuple[ti.TranscriptContext, ...]:
    """Every transcript of the project and of its worktree sibling folders."""
    return tuple(
        context for directory in project_transcript_dirs(project_dir) for context in ti.discover_transcripts(directory)
    )


def load_transcripts(
    project_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Mapping[TranscriptRef, tuple[ti.AssistantMessageEvent, ...]]:
    """Every transcript under `project_dir` with at least one deduplicated
    assistant message, filtered whole by `--since`/`--until` against its
    first event timestamp, keyed by its attribution. A response repeated in
    several files (same message id and requestId) is kept only in the file
    holding its best copy among the files the window keeps. The one transcript
    source shared by the usage report and the `--context` profile."""
    candidates: list[tuple[ti.TranscriptContext, tuple[ti.AssistantMessageEvent, ...]]] = []
    for context in discover_project_transcripts(project_dir):
        assistant_events = tuple(
            event for event in ti.iter_events(context) if isinstance(event, ti.AssistantMessageEvent)
        )
        if not assistant_events:
            continue
        if not _within_range(_first_assistant_timestamp(assistant_events), since, until):
            continue
        candidates.append((context, assistant_events))
    # Only files the window keeps take part, so a filtered-out file never claims a message; a
    # response repeated across files is then owned by its best copy among these.
    loaded: dict[TranscriptRef, tuple[ti.AssistantMessageEvent, ...]] = {}
    for context, owned in ti.dedupe_across_files(candidates):
        if not owned:
            continue
        ref = TranscriptRef(
            path=str(context.transcript_path),
            ticket=ticket_of_dispatch(context),
            role=role_of(context),
            shape=dispatch_shape(context),
            meta_model=context.meta_model,
        )
        loaded[ref] = owned
    return loaded


def build_report(
    project_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> UsageReport:
    """Sums the eq-weighted usage of every deduplicated assistant message
    of `load_transcripts` into the four groupings. Ticket/role/dispatch-shape
    are transcript-level (fixed for every message in one transcript);
    model is read per message, since one transcript can carry messages
    from more than one model."""
    by_ticket: defaultdict[str, float] = defaultdict(float)
    by_role: defaultdict[str, float] = defaultdict(float)
    by_model: defaultdict[str, float] = defaultdict(float)
    by_shape: defaultdict[str, float] = defaultdict(float)
    grand_total = 0.0
    unpriced: dict[str, dict[str, int]] = {}
    unsplit = {"requests": 0, "tokens": 0}

    for ref, assistant_events in load_transcripts(project_dir, since=since, until=until).items():
        for event in assistant_events:
            model = event.model or ref.meta_model
            if not is_priced(model):
                if event.usage is not None:
                    _count_unpriced(unpriced, model, event.usage)
                continue
            weight = eq_weight(event.usage, model)
            if event.usage is not None:
                _count_unsplit(unsplit, event.usage)
            by_ticket[ref.ticket] += weight
            by_role[ref.role] += weight
            by_model[model_family(model)] += weight
            by_shape[ref.shape] += weight
            grand_total += weight

    return UsageReport(
        by_ticket=dict(by_ticket),
        by_role=dict(by_role),
        by_model=dict(by_model),
        by_dispatch_shape=dict(by_shape),
        grand_total=grand_total,
        unpriced=unpriced,
        cache_write_unsplit=unsplit,
    )


def message_context(usage: Mapping[str, object] | None) -> int:
    """The prompt size one message saw: input + cache_read + cache_creation."""
    if usage is None:
        return 0
    return int(
        usage_tokens(usage, "input_tokens")
        + usage_tokens(usage, "cache_read_input_tokens")
        + usage_tokens(usage, "cache_creation_input_tokens")
    )


@dataclasses.dataclass(frozen=True)
class ContextRow:
    """One transcript's context profile against a budget."""

    path: str
    ticket: str
    role: str
    turns: int
    first_context: int
    peak_context: int
    cross_turn: int | None
    turns_over: int
    eq: float
    eq_over: float


@dataclasses.dataclass(frozen=True)
class ContextRollup:
    """A group of `ContextRow`s (one role, one ticket, or everything)."""

    sessions: int
    sessions_over: int
    median_peak: float
    max_peak: int
    share_eq_over: float
    # total eq of the group; 0 means nothing priced was spent, so the share is not available
    eq: float = 0.0


@dataclasses.dataclass(frozen=True)
class ContextProfile:
    budget: int
    rows: tuple[ContextRow, ...]
    overall: ContextRollup
    by_role: Mapping[str, ContextRollup]
    by_ticket: Mapping[str, ContextRollup]
    unpriced: Mapping[str, Mapping[str, int]] = dataclasses.field(default_factory=dict)
    cache_write_unsplit: Mapping[str, int] = dataclasses.field(default_factory=lambda: {"requests": 0, "tokens": 0})


def _rollup(rows: Sequence[ContextRow]) -> ContextRollup:
    if not rows:
        return ContextRollup(0, 0, 0.0, 0, 0.0)
    total_eq = sum(row.eq for row in rows)
    return ContextRollup(
        sessions=len(rows),
        sessions_over=sum(1 for row in rows if row.cross_turn is not None),
        median_peak=statistics.median(row.peak_context for row in rows),
        max_peak=max(row.peak_context for row in rows),
        share_eq_over=sum(row.eq_over for row in rows) / total_eq if total_eq else 0.0,
        eq=total_eq,
    )


def _rollups_by(rows: Sequence[ContextRow], key: Callable[[ContextRow], str]) -> Mapping[str, ContextRollup]:
    labels = sorted({key(row) for row in rows})
    return {label: _rollup([row for row in rows if key(row) == label]) for label in labels}


def build_context_profile(
    events_by_transcript: Mapping[TranscriptRef, tuple[ti.AssistantMessageEvent, ...]],
    budget: int,
) -> ContextProfile:
    """Pure: per-transcript context rows against `budget`. A turn is over
    budget when its context is strictly greater than `budget`."""
    rows: list[ContextRow] = []
    unpriced: dict[str, dict[str, int]] = {}
    unsplit = {"requests": 0, "tokens": 0}
    for ref, events in events_by_transcript.items():
        for event in events:
            if event.usage is None:
                continue
            if is_priced(event.model or ref.meta_model):
                _count_unsplit(unsplit, event.usage)
            else:
                _count_unpriced(unpriced, event.model or ref.meta_model, event.usage)
        contexts = tuple(message_context(event.usage) for event in events)
        weights = tuple(eq_weight(event.usage, event.model or ref.meta_model) for event in events)
        rows.append(
            ContextRow(
                path=ref.path,
                ticket=ref.ticket,
                role=ref.role,
                turns=len(events),
                first_context=contexts[0],
                peak_context=max(contexts),
                cross_turn=next((i for i, tokens in enumerate(contexts) if tokens > budget), None),
                turns_over=sum(1 for tokens in contexts if tokens > budget),
                eq=sum(weights),
                eq_over=sum(w for w, tokens in zip(weights, contexts, strict=True) if tokens > budget),
            )
        )
    return ContextProfile(
        budget=budget,
        rows=tuple(rows),
        overall=_rollup(rows),
        by_role=_rollups_by(rows, lambda row: row.role),
        by_ticket=_rollups_by(rows, lambda row: row.ticket),
        unpriced=unpriced,
        cache_write_unsplit=unsplit,
    )


def _format_grouping(title: str, totals: Mapping[str, float], *, all_unpriced: bool = False) -> list[str]:
    lines = [title]
    if not totals:
        lines.append("  (all usage is unpriced; see below)" if all_unpriced else "  (no dispatches in range)")
        return lines
    lines.extend(f"  {label:<24} {totals[label]:>14,.1f}" for label in sorted(totals, key=lambda k: (-totals[k], k)))
    return lines


def _format_unpriced(unpriced: Mapping[str, Mapping[str, int]], excluded_from: str) -> list[str]:
    if not unpriced:
        return []
    lines = [f"Unpriced models (no price row; excluded from {excluded_from}):"]
    lines.extend(
        f"  {model_label(model):<40} {counts['requests']:>8,} requests {counts['tokens']:>16,} tokens"
        for model, counts in sorted(unpriced.items(), key=lambda item: (-item[1]["tokens"], item[0]))
    )
    return lines


def _format_unpriced_warning(unpriced: Mapping[str, Mapping[str, int]]) -> list[str]:
    """The footer line the pricing reports share: unpriced model ids and their request counts."""
    line = unpriced_footer({model: counts["requests"] for model, counts in unpriced.items()})
    return [] if line is None else [line]


def _format_unsplit(unsplit: Mapping[str, int]) -> list[str]:
    if not unsplit["requests"]:
        return []
    return [
        "Cache writes without a TTL split (priced at the 5-minute rate; totals are a lower bound):",
        f"  {unsplit['requests']:>8,} requests {unsplit['tokens']:>16,} tokens",
    ]


def format_table(report: UsageReport) -> str:
    lines: list[str] = []
    only_unpriced = bool(report.unpriced)
    lines += _format_grouping(
        "By ticket (sonnet-input-equivalent tokens):", report.by_ticket, all_unpriced=only_unpriced
    )
    lines += _format_grouping("By role:", report.by_role, all_unpriced=only_unpriced)
    lines += _format_grouping("By model family:", report.by_model, all_unpriced=only_unpriced)
    lines += _format_grouping("By dispatch shape:", report.by_dispatch_shape, all_unpriced=only_unpriced)
    lines.append(f"Grand total: {report.grand_total:,.1f}")
    lines += _format_unpriced(report.unpriced, "every total above")
    lines += _format_unsplit(report.cache_write_unsplit)
    lines += _format_unpriced_warning(report.unpriced)
    return "\n".join(lines)


def format_json(report: UsageReport) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)


def _share(rollup: ContextRollup) -> str:
    return f"{rollup.share_eq_over:.0%}" if rollup.eq else "n/a"


def _format_rollups(title: str, rollups: Mapping[str, ContextRollup]) -> list[str]:
    lines = [
        title,
        f"  {'':<24} {'sessions':>8} {'over':>5} {'median peak':>12} {'max peak':>10} {'eq over':>8}",
    ]
    lines.extend(
        f"  {label:<24} {r.sessions:>8} {r.sessions_over:>5} {r.median_peak:>12,.0f} {r.max_peak:>10,} {_share(r):>8}"
        for label, r in sorted(rollups.items(), key=lambda item: (-item[1].share_eq_over, item[0]))
    )
    return lines


def format_context_table(profile: ContextProfile) -> str:
    overall = profile.overall
    lines = [
        (
            f"Context profile (budget {profile.budget:,} tokens): "
            f"{overall.sessions_over} of {overall.sessions} sessions crossed, "
            f"{_share(overall)} of eq spent above budget"
        )
    ]
    lines += _format_rollups("By role:", profile.by_role)
    lines += _format_rollups("By ticket:", profile.by_ticket)
    lines += _format_unpriced(profile.unpriced, "eq above")
    lines += _format_unsplit(profile.cache_write_unsplit)
    lines += _format_unpriced_warning(profile.unpriced)
    return "\n".join(lines)


def format_context_json(profile: ContextProfile) -> str:
    return json.dumps(dataclasses.asdict(profile), indent=2, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="the ~/.claude/projects/<slug> transcript folder (default: the cwd's); its "
        "<slug>--claude-worktrees-* sibling folders are read too",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="the project's checkout; its transcript folder is derived from it (instead of --project-dir)",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO-8601 date/timestamp: only include transcripts starting at or after it",
    )
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="ISO-8601 date/timestamp: only include transcripts starting at or before it",
    )
    parser.add_argument(
        "--context",
        action="store_true",
        help="report per-session context size against --budget instead of usage totals",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="with --context (required): context-token budget",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    return parser


def _parse_cli_timestamp(raw: str | None) -> tuple[datetime | None, bool]:
    """Parses an optional CLI ISO-8601 timestamp argument; the second
    return value is `True` when `raw` was given but failed to parse."""
    if raw is None:  # an empty string was given and is invalid
        return None, False
    parsed = parse_timestamp(raw)
    return parsed, parsed is None


def _display_path(path: Path) -> str:
    """The path with the home directory shown as `~`, so output pasted elsewhere carries no home path."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.budget is not None and not args.context:
        parser.error("--budget requires --context")
    if args.context and args.budget is None:
        parser.error("--context requires --budget")
    if args.project_dir is not None and args.project_root is not None:
        parser.error("--project-dir and --project-root are exclusive")
    if args.project_dir is None:
        args.project_dir = default_project_dir(args.project_root)
    since, since_invalid = _parse_cli_timestamp(args.since)
    until, until_invalid = _parse_cli_timestamp(args.until)
    if since_invalid or until_invalid:
        bad_flag = "--since" if since_invalid else "--until"
        bad_value = args.since if since_invalid else args.until
        print(
            f"agent_usage_report: {bad_flag} is not a valid ISO-8601 timestamp: {bad_value!r}",
            file=sys.stderr,
        )
        return 1

    if not discover_project_transcripts(args.project_dir):
        # No folder or no transcripts at all: a zero table would read as real zero usage.
        message = f"no transcripts found at {_display_path(args.project_dir)}"
        if args.json:
            print(message, file=sys.stderr)
            return 1
        print(message)
        return 0

    tr.reset_rejected()
    if args.context:
        profile = build_context_profile(
            load_transcripts(args.project_dir, since=since, until=until),
            args.budget,
        )
        print(format_context_json(profile) if args.json else format_context_table(profile))
    else:
        report = build_report(args.project_dir, since=since, until=until)
        print(format_json(report) if args.json else format_table(report))
    note = tr.rejected_note("transcripts")
    if note:
        # Kept off stdout in JSON mode so the output stays parseable.
        print(note, file=sys.stderr if args.json else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
