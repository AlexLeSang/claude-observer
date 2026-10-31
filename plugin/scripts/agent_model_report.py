#!/usr/bin/env python3
"""Model and effort comparison for `agent_telemetry.py report --models`.

The operator chooses a model and an effort level per session. This view puts
them side by side: for every model x effort pair, the loop iterations, output
tokens, cost and stop reasons, first over the whole capture and then per
session (the unit of comparison). Outcome is not measured, so it shows what a
setting spends, not whether its answers were better.

Sources, the same as `report --sessions` (it reuses that loader):

- `claude_code.api_request` events: model, `effort`, output tokens and cost,
  for loop requests only (`repl_main_thread`, `sdk`, `agent:*`). Side requests
  (titles, hook prompts, summaries) are left out, so a cheap model that titles
  a session is not compared with the one that ran it; `--sessions` shows their
  share.
- the usage-observer plugin's native `step` records, for prompts with no loop
  event (no cost), and for stop reasons.

Stop reasons exist only on native steps: the OTLP event has none. They are
counted from every native step, including those of prompts that also have
events, so the stop column can cover requests the other columns count from
events. Without the plugin capture it shows `-`.

Nothing is read from transcripts, and no prompt text, tool text, identity
attribute (`user.*`, `organization.id`, `vcs.*`) or project label is printed.
"""

from __future__ import annotations

import dataclasses
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_session_report as sr
import agent_tool_report as tr
from agent_iteration_report import as_int, format_table, plural, unreadable_note, with_unreadable

if TYPE_CHECKING:
    from agent_telemetry import ProjectFilter

NONE = sr.NONE
UNSET = "unset"
MAX_STOPS = 3  # stop reasons listed per row, most common first

Key = tuple[str, str]  # model, effort


# --- aggregation -----------------------------------------------------------------


@dataclasses.dataclass
class Group:
    """Loop requests of one model x effort pair."""

    iterations: int = 0
    output: float = 0.0
    cost: float = 0.0
    uncosted: int = 0
    stops: Counter[str] = dataclasses.field(default_factory=Counter)

    @property
    def costed(self) -> int:
        return self.iterations - self.uncosted

    def add(self, request: sr.Request) -> None:
        self.iterations += 1
        self.output += request.tokens[1]
        if request.cost is None:
            self.uncosted += 1
        else:
            self.cost += request.cost

    def merge(self, other: Group) -> None:
        self.iterations += other.iterations
        self.output += other.output
        self.cost += other.cost
        self.uncosted += other.uncosted
        self.stops.update(other.stops)


@dataclasses.dataclass
class SessionGroups:
    session: str
    first: str = ""
    last: str = ""
    groups: dict[Key, Group] = dataclasses.field(default_factory=dict)

    def group(self, request: sr.Request) -> Group:
        key = (request.model, request.effort or UNSET)
        return self.groups.setdefault(key, Group())


@dataclasses.dataclass
class ModelReport:
    sessions: list[SessionGroups]
    unreadable: int = 0

    @property
    def totals(self) -> dict[Key, Group]:
        out: dict[Key, Group] = {}
        for session in self.sessions:
            for key, group in session.groups.items():
                out.setdefault(key, Group()).merge(group)
        return out

    @property
    def uncosted(self) -> int:
        return sum(g.uncosted for s in self.sessions for g in s.groups.values())

    @property
    def stops(self) -> int:
        return sum(sum(g.stops.values()) for s in self.sessions for g in s.groups.values())


def build_report(capture: sr.Capture) -> ModelReport:
    event_prompts = {(r.session, r.prompt) for r in capture.requests if r.loop}
    sessions: dict[str, SessionGroups] = {}

    def entry(request: sr.Request) -> SessionGroups:
        return sessions.setdefault(request.session, SessionGroups(request.session))

    for request in capture.requests:
        if request.loop:
            _count(entry(request), request)
    for step in capture.steps:
        # A prompt that has loop events is counted from them; its steps only add stop reasons.
        group = entry(step)
        if (step.session, step.prompt) not in event_prompts:
            _count(group, step)
        elif step.timestamp:
            _stamp(group, step)
        if step.stop:
            group.group(step).stops[step.stop] += 1
    ordered = sorted(sessions.values(), key=lambda s: (s.last, s.session), reverse=True)
    return ModelReport(ordered, capture.unreadable)


def _stamp(session: SessionGroups, request: sr.Request) -> None:
    session.first = min(session.first or request.timestamp, request.timestamp)
    session.last = max(session.last, request.timestamp)


def _count(session: SessionGroups, request: sr.Request) -> None:
    session.group(request).add(request)
    if request.timestamp:
        _stamp(session, request)


# --- formatting ------------------------------------------------------------------


def _name(model: str) -> str:
    return model.removeprefix(sr.MODEL_PREFIX)


def _cost(group: Group) -> str:
    return f"${group.cost:.2f}" if group.costed else NONE


def _per_iteration(group: Group) -> str:
    return f"${group.cost / group.costed:.3f}" if group.costed else NONE


def _stops(stops: Counter[str]) -> str:
    """`tool_use 12, end_turn 3`, most common first; `-` when there are none."""
    ordered = sorted(stops.items(), key=lambda item: (-item[1], item[0]))
    shown = [f"{name} {count:,}" for name, count in ordered[:MAX_STOPS]]
    if len(ordered) > MAX_STOPS:
        shown.append(f"+{len(ordered) - MAX_STOPS} more")
    return ", ".join(shown) or NONE


def _metrics(group: Group) -> list[str]:
    return [
        f"{group.iterations:,}",
        as_int(group.output),
        as_int(group.output / group.iterations) if group.iterations else NONE,
        _cost(group),
        _per_iteration(group),
        _stops(group.stops),
    ]


def _order(keys: list[Key]) -> list[Key]:
    return sorted(keys, key=lambda k: (_name(k[0]), k[1]))


TOTAL_HEADER = ["model", "effort", "sessions", "iters", "output", "out/iter", "cost", "$/iter", "stops"]
SESSION_HEADER = ["session", "started", "model", "effort", "iters", "output", "out/iter", "cost", "$/iter", "stops"]
LEGEND = [
    "  iters = loop requests (main loop and subagents); out/iter = output tokens per iteration; $/iter = cost per",
    "  costed iteration; stops = stop reasons of native steps (plugin capture only), most common first",
]


def _total_block(report: ModelReport) -> list[str]:
    totals = report.totals
    seen: Counter[Key] = Counter()
    for session in report.sessions:
        seen.update(k for k, g in session.groups.items() if g.iterations or g.stops)
    rows = [
        [_name(model), effort, f"{seen[(model, effort)]:,}", *_metrics(totals[(model, effort)])]
        for model, effort in _order([k for k, g in totals.items() if g.iterations or g.stops])
    ]
    table = format_table(TOTAL_HEADER, rows, left=2, text=(8,))
    return ["By model and effort (every session in the capture, loop requests)", *table, *LEGEND]


def _session_block(report: ModelReport, limit: int | None) -> list[str]:
    shown = report.sessions[: limit or sr.DEFAULT_SESSION_LIMIT]
    rows = []
    for session in shown:
        keys = _order([k for k, g in session.groups.items() if g.iterations or g.stops])
        for model, effort in keys:
            rows.append(
                [
                    tr.session_label(session.session),
                    tr.when_label(session.first),
                    _name(model),
                    effort,
                    *_metrics(session.groups[(model, effort)]),
                ]
            )
    hidden = len(report.sessions) - len(shown)
    older = f"; {plural(hidden, 'older session')} not shown, raise --limit" if hidden else ""
    table = format_table(SESSION_HEADER, rows, left=4, text=(9,))
    return [f"Per session (most recent first{older})", *table]


def format_report(report: ModelReport, limit: int | None = None) -> str:
    unreadable = report.unreadable
    report = ModelReport(
        [s for s in report.sessions if any(g.iterations or g.stops for g in s.groups.values())], unreadable
    )
    if not report.sessions:
        return with_unreadable("No loop requests found.", unreadable)
    blocks = [_total_block(report), _session_block(report, limit)]
    notes = []
    if report.uncosted:
        notes.append(f"  {plural(report.uncosted, 'iteration')} without cost (left out of cost and $/iter)")
    if not report.stops:
        notes.append("  stop reasons need the plugin capture (step records); none found")
    if report.unreadable:
        notes.append(f"  {unreadable_note(report.unreadable)}")
    if notes:
        blocks.append(["Notes", *notes])
    return "\n\n".join("\n".join(block) for block in blocks)


def load_capture(capture: Path, project_filter: ProjectFilter | None = None) -> sr.Capture:
    return sr.load_capture(capture, project_filter)
