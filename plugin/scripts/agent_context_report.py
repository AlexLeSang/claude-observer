#!/usr/bin/env python3
"""Context fill and compaction view for `agent_telemetry.py report --context`.

Two blocks, from the capture alone:

- Per session: peak context fill, fill over time (one sparkline character per
  sample, or per bucket of samples in a long session) and the number of
  compactions. Fill comes from the usage-observer plugin's native `measure`
  records (`context_tokens` over `context_window`); sessions without them show
  no fill.
- Per compaction: its trigger, the context size before and after, and the cache
  re-creation that followed: the tokens, and the cost where known, of the next
  main-loop request after the compaction, which has to rebuild the cache for the
  shortened context.

Compactions come from the plugin's native `compaction` records when a session
has any (they carry the sizes and tell skipped attempts apart); otherwise from
`claude_code.compaction` events, whose `pre_tokens` and `post_tokens` are the
sizes. An agent (the main loop or one subagent) never counts a compaction from both.
Only main-loop compactions are counted and listed; subagent ones are noted. Skipped and failed
attempts install nothing and are left out and counted.

Nothing is read from transcripts, and no prompt text, tool text, identity
attribute (`user.*`, `organization.id`, `vcs.*`) or project label is printed.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tool_report as tr
from agent_context_fill import (
    COMPACTION,
    Compaction,
    Sample,
    count_compactions,
    event_compaction_of,
    native_compaction_of,
    peak_of,
    sample_of,
    select_compactions,
    text_of,
)
from agent_iteration_report import MAIN_SOURCES, as_int, format_table, plural, unreadable_note, with_unreadable

if TYPE_CHECKING:
    from agent_telemetry import ProjectFilter

DEFAULT_SESSION_LIMIT = 20
SPARK_WIDTH = 24
SPARK = "▁▂▃▄▅▆▇█"
NONE = "-"
HIGH_FILL = 50.0  # a session is flagged when its peak fill reaches this percent


# --- loading ---------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Follower:
    """A main-loop request: what the cache looked like after a compaction."""

    session: str
    prompt: str
    timestamp: str
    cache_creation: float
    cache_read: float
    cost: float | None


@dataclasses.dataclass
class Capture:
    samples: list[Sample] = dataclasses.field(default_factory=list)
    native: list[Compaction] = dataclasses.field(default_factory=list)
    events: list[Compaction] = dataclasses.field(default_factory=list)
    followers: list[Follower] = dataclasses.field(default_factory=list)
    event_prompts: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    left_out: int = 0  # skipped or failed compaction attempts
    unreadable: int = 0  # capture lines that were not JSON objects


def _event_follower(attrs: dict[str, Any]) -> Follower | None:
    source = text_of(attrs.get("query_source"), "")
    if source not in MAIN_SOURCES or attrs.get("agent.name"):
        return None
    return Follower(
        text_of(attrs.get("session.id")),
        text_of(attrs.get("prompt.id")),
        text_of(attrs.get("event.timestamp"), ""),
        tr.as_number(attrs.get("cache_creation_tokens")) or 0.0,
        tr.as_number(attrs.get("cache_read_tokens")) or 0.0,
        tr.as_cost(attrs.get("cost_usd")),
    )


def _native_follower(record: dict[str, Any]) -> Follower | None:
    if record.get("agent") is not None:
        return None
    usage = record.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return Follower(
        text_of(record.get("session")),
        text_of(record.get("prompt")),
        text_of(record.get("ts"), ""),
        tr.as_number(usage.get("cache_creation_input_tokens")) or 0.0,
        tr.as_number(usage.get("cache_read_input_tokens")) or 0.0,
        None,
    )


def load_capture(capture: Path, project_filter: ProjectFilter | None = None) -> Capture:
    """Fill samples, compactions and main-loop requests that pass `project_filter`;
    native records are filtered by their session."""
    out = Capture()
    steps: list[Follower] = []
    skipped = tr.Skipped()
    for record in tr.capture_records(capture, skipped):
        kind = record.get("kind")
        if kind in ("step", "measure", "compaction"):
            if project_filter is not None and not project_filter.keep({"session.id": record.get("session")}):
                continue
            if kind == "measure":
                if (sample := sample_of(record)) is not None:
                    out.samples.append(sample)
            elif kind == "compaction":
                if record.get("skip") is not None:
                    out.left_out += 1
                else:
                    out.native.append(native_compaction_of(record))
            elif (step := _native_follower(record)) is not None:
                steps.append(step)
            continue
        for name, attrs in tr.iter_log_events(record.get("payload")):
            if name not in (tr.API_REQUEST, COMPACTION):
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            if name == COMPACTION:
                if str(attrs.get("success")).lower() == "false":
                    out.left_out += 1
                else:
                    out.events.append(event_compaction_of(attrs))
            elif (follower := _event_follower(attrs)) is not None:
                out.followers.append(follower)
                out.event_prompts.add((follower.session, follower.prompt))
    # A prompt that has both events and steps is counted from its events only.
    out.followers += [s for s in steps if (s.session, s.prompt) not in out.event_prompts]
    out.unreadable = skipped.lines
    return out


# --- building --------------------------------------------------------------------


@dataclasses.dataclass
class SessionFill:
    session: str
    samples: list[Sample] = dataclasses.field(default_factory=list)
    compactions: int = 0  # main loop only
    subagent_compactions: int = 0
    last: str = ""

    @property
    def peak(self) -> Sample | None:
        return peak_of(self.samples)


@dataclasses.dataclass(frozen=True)
class Recreation:
    compaction: Compaction
    follower: Follower | None  # the next main-loop request, if any


@dataclasses.dataclass
class ContextReport:
    sessions: list[SessionFill]
    recreations: list[Recreation]
    left_out: int
    unreadable: int = 0


def _next_request(compaction: Compaction, followers: list[Follower]) -> Follower | None:
    later = [f for f in followers if f.session == compaction.session and f.timestamp >= compaction.timestamp]
    return min(later, key=lambda f: f.timestamp, default=None)


def build_report(capture: Capture) -> ContextReport:
    sessions: dict[str, SessionFill] = {}
    for sample in sorted(capture.samples, key=lambda s: s.timestamp):
        fill = sessions.setdefault(sample.session, SessionFill(sample.session))
        fill.samples.append(sample)
        fill.last = max(fill.last, sample.timestamp)
    compactions = select_compactions(capture.native, capture.events)
    for compaction in compactions:
        fill = sessions.setdefault(compaction.session, SessionFill(compaction.session))
        fill.last = max(fill.last, compaction.timestamp)
    main, sub = count_compactions(compactions)
    for fill in sessions.values():
        fill.compactions = main.get(fill.session, 0)
        fill.subagent_compactions = sub.get(fill.session, 0)
    ordered = sorted(sessions.values(), key=lambda s: (s.last, s.session), reverse=True)
    # The next main-loop request only rebuilds the cache after a main-loop compaction.
    recreations = [Recreation(c, _next_request(c, capture.followers)) for c in compactions if c.agent is None]
    recreations.sort(key=lambda r: (r.compaction.timestamp, r.compaction.session), reverse=True)
    return ContextReport(ordered, recreations, capture.left_out, capture.unreadable)


# --- formatting --------------------------------------------------------------------


def sparkline(percents: list[float], width: int = SPARK_WIDTH) -> str:
    """One character per sample, or the peak of each bucket when there are more
    samples than `width`; scaled 0 to 100% so sessions compare."""
    if not percents:
        return NONE
    count = len(percents)
    if count > width:
        buckets = [percents[i * count // width : (i + 1) * count // width] for i in range(width)]
        percents = [max(bucket) for bucket in buckets]
    return "".join(SPARK[min(len(SPARK) - 1, max(0, int(p / 100 * len(SPARK))))] for p in percents)


def _percent(value: float | None) -> str:
    return NONE if value is None else f"{value:.0f}%"


def _tokens(value: float | None) -> str:
    return NONE if value is None else as_int(value)


def _peak_label(fill: SessionFill) -> str:
    peak = fill.peak
    return NONE if peak is None else _percent(peak.percent) + ("!" if peak.percent >= HIGH_FILL else "")


FILL_HEADER = ["session", "started", "samples", "peak", "peak tokens", "window", "last", "compact", "fill over time"]
FILL_LEGEND = [
    f"  one character per sample (the peak of a bucket past {SPARK_WIDTH} samples), scaled 0-100% of the window;",
    f"  ! = peak at or above {HIGH_FILL:.0f}%; compact = compactions that installed a summary",
]


def _fill_row(fill: SessionFill) -> list[str]:
    peak, last = fill.peak, fill.samples[-1] if fill.samples else None
    return [
        tr.session_label(fill.session),
        tr.when_label(fill.samples[0].timestamp) if fill.samples else NONE,
        f"{len(fill.samples):,}",
        _peak_label(fill),
        _tokens(peak.tokens if peak else None),
        _tokens(peak.window if peak else None),
        _percent(last.percent if last else None),
        f"{fill.compactions:,}",
        sparkline([s.percent for s in fill.samples]),
    ]


RECREATION_HEADER = ["session", "when", "trigger", "before", "after", "freed", "created", "read", "cost"]
RECREATION_LEGEND = [
    "  before and after = context tokens around the compaction; created, read and cost = the next main-loop",
    "  request: cache tokens it had to write and could still read, and what it cost",
]


def _recreation_row(item: Recreation) -> list[str]:
    c, f = item.compaction, item.follower
    freed = None if c.before is None or c.after is None or not c.before else (1 - c.after / c.before) * 100
    return [
        tr.session_label(c.session),
        tr.when_label(c.timestamp),
        c.trigger,
        _tokens(c.before),
        _tokens(c.after),
        _percent(freed),
        _tokens(f.cache_creation if f else None),
        _tokens(f.cache_read if f else None),
        f"${f.cost:.2f}" if f and f.cost is not None else NONE,
    ]


def format_report(report: ContextReport, limit: int | None = None) -> str:
    if not report.sessions:
        return with_unreadable("No context measurements or compactions found.", report.unreadable)
    shown = report.sessions[: limit or DEFAULT_SESSION_LIMIT]
    shown_ids = {s.session for s in shown}
    hidden = len(report.sessions) - len(shown)
    older = f"; {plural(hidden, 'older session')} not shown, raise --limit" if hidden else ""
    fill_table = format_table(FILL_HEADER, [_fill_row(s) for s in shown], left=1, text=(1, 8))
    blocks = [[f"Context fill per session (most recent first{older})", *fill_table, *FILL_LEGEND]]
    recreations = [r for r in report.recreations if r.compaction.session in shown_ids]
    if recreations:
        table = format_table(RECREATION_HEADER, [_recreation_row(r) for r in recreations], left=1, text=(1, 2))
        blocks.append(["Compactions and the cache re-created after each", *table, *RECREATION_LEGEND])
    else:
        blocks.append(["Compactions and the cache re-created after each", "  none recorded"])
    notes = []
    without = sum(not s.samples for s in shown)
    if without:
        notes.append(f"  fill needs the plugin capture (measure records); {plural(without, 'session')} without it")
    unsized = sum(r.compaction.before is None or r.compaction.after is None for r in recreations)
    if unsized:
        notes.append(f"  {plural(unsized, 'compaction')} without recorded sizes")
    unfollowed = sum(r.follower is None for r in recreations)
    if unfollowed:
        notes.append(f"  {plural(unfollowed, 'compaction')} without a later main-loop request (nothing to read)")
    if any(r.follower is not None and r.follower.cost is None for r in recreations):
        notes.append("  cost is not known for requests seen only as native steps")
    if subagent := sum(s.subagent_compactions for s in shown):
        notes.append(f"  {plural(subagent, 'subagent compaction')} not listed: they do not rebuild the main-loop cache")
    if report.left_out:
        notes.append(f"  {plural(report.left_out, 'skipped or failed compaction attempt')} left out")
    if report.unreadable:
        notes.append(f"  {unreadable_note(report.unreadable)}")
    if notes:
        blocks.append(["Notes", *notes])
    return "\n\n".join("\n".join(block) for block in blocks)
