#!/usr/bin/env python3
"""One row per session for `agent_telemetry.py report --sessions`.

The session is the unit the operator compares: what it cost, how many tokens
it moved, how much of the input came from cache, which models and effort it
ran, how many loop iterations it took, how much of it was side requests, how
full the context got and how often it was compacted.

Sources, per session:

- `claude_code.api_request` events: cost, the four token counts, model,
  `effort` and `query_source`. `repl_main_thread`, `sdk` and `agent:*` are loop
  requests (iterations); every other source (session titles, hook prompts,
  agent summaries, ...) is a side request. Models and effort are taken from
  loop requests only, so a cheap model that titles the session does not show
  up as the model the session ran.
- the usage-observer plugin's native `step` records, for prompts that have no
  loop `api_request` event (the mirror was off); they carry no cost. A prompt
  that has both is counted from its events only, never twice.
- native `measure` records: peak context fill (plugin capture only).
- native `compaction` records, else `claude_code.compaction` events whose `success` is not
  `false`; main-loop compactions only. Fill and compaction rules are shared with `--context`
  (`agent_context_fill.py`).

Nothing is read from transcripts, and no prompt text, tool text, identity
attribute (`user.*`, `organization.id`, `vcs.*`) or project label is printed.
"""

from __future__ import annotations

import dataclasses
import itertools
import sys
from collections import Counter, defaultdict, deque
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
from agent_iteration_report import (
    AGENT_SOURCE_PREFIX,
    HOOK_PROMPT,
    HOOK_PROMPT_NOTE,
    NATIVE_TOKEN_KEYS,
    PLUGIN_REGISTER,
    PLUGIN_SET,
    as_int,
    format_table,
    hook_prompt_label,
    loop_type,
    names_you_should_know,
    plural,
    unreadable_note,
    with_unreadable,
)
from agent_usage_report import parse_timestamp
from safe_name import clean_name

if TYPE_CHECKING:
    from datetime import datetime

    from agent_telemetry import ProjectFilter

DEFAULT_SESSION_LIMIT = 20
MODEL_PREFIX = "claude-"
NONE = "-"


# --- loading ---------------------------------------------------------------------


@dataclasses.dataclass
class Request:
    """One model request: an `api_request` event, or a native `step`."""

    session: str
    prompt: str
    model: str
    effort: str | None
    tokens: tuple[float, float, float, float]  # fresh input, output, cache read, cache creation
    cost: float | None
    timestamp: str
    loop: bool
    stop: str | None = None  # native steps only; OTLP api_request carries none
    source: str = ""  # query_source of an api_request event; native steps carry none
    sub: bool = False  # a loop request of a subagent the session launched, not of the main loop
    request_id: str | None = None  # api_request `request_id`: joins a request to a subagent transcript
    agent: str | None = None  # native step's agent id
    subagent: str | None = (
        None  # subagent type (`agent.name`, else the last `query_source` part); None for the main loop
    )


MAX_TICKET_DIGITS = 5
MAX_TYPE_CHARS = 64


@dataclasses.dataclass(frozen=True)
class Spawn:
    """What an `agent_spawn` record says about one subagent: the dispatch description's
    ticket id and the agent type, nothing else. Both come from an untrusted capture line."""

    ticket: str | None  # plain digit string, 1 to 5 digits, not 0
    agent_type: str | None  # printable, at most MAX_TYPE_CHARS


def spawn_ticket(value: object) -> str | None:
    """A ticket id as the plugin records it: a real integer from 1 to 99999. A bool, float, str or
    anything else names no ticket."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return str(value) if 0 < value < 10**MAX_TICKET_DIGITS else None


def spawn_type(value: object) -> str | None:
    """An agent type name: str only, cleaned and capped as `safe_name.clean_name`, never empty."""
    return clean_name(value)


@dataclasses.dataclass
class Capture:
    requests: list[Request] = dataclasses.field(default_factory=list)
    spawns: dict[tuple[str, str], Spawn] = dataclasses.field(default_factory=dict)  # (session, agent id)
    steps: list[Request] = dataclasses.field(default_factory=list)
    samples: list[Sample] = dataclasses.field(default_factory=list)  # native `measure` fill
    native_compactions: list[Compaction] = dataclasses.field(default_factory=list)
    event_compactions: list[Compaction] = dataclasses.field(default_factory=list)
    unreadable: int = 0  # capture lines that were not JSON objects
    you_should_know: set[str] = dataclasses.field(default_factory=set)  # sessions whose plugin_set names the plugin


def api_request_of(attrs: dict[str, Any]) -> Request:
    tokens = tuple(tr.as_number(attrs.get(key)) or 0.0 for key in tr.API_TOKEN_KEYS)
    source = text_of(attrs.get("query_source"), "")
    kind = loop_type(source, attrs.get("agent.name"))
    return Request(
        session=text_of(attrs.get("session.id")),
        prompt=text_of(attrs.get("prompt.id")),
        model=text_of(attrs.get("model")),
        effort=tr.as_str(attrs.get("effort")),
        tokens=tokens,  # type: ignore[arg-type]
        cost=tr.as_cost(attrs.get("cost_usd")),
        timestamp=text_of(attrs.get("event.timestamp"), ""),
        loop=kind is not None,
        source=source,
        sub=source.startswith(AGENT_SOURCE_PREFIX),
        request_id=tr.as_str(attrs.get("request_id")),
        subagent=kind if source.startswith(AGENT_SOURCE_PREFIX) else None,
    )


def _native_step(record: dict[str, Any]) -> Request:
    usage = record.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens = tuple(tr.as_number(usage.get(key)) or 0.0 for key in NATIVE_TOKEN_KEYS)
    return Request(
        session=text_of(record.get("session")),
        prompt=text_of(record.get("prompt")),
        model=text_of(record.get("model")),
        effort=tr.as_str(record.get("effort")),
        tokens=tokens,  # type: ignore[arg-type]
        cost=None,
        timestamp=text_of(record.get("ts"), ""),
        loop=True,
        stop=tr.as_str(record.get("stop")),
        sub=record.get("agent") is not None,
        agent=None if record.get("agent") is None else str(record.get("agent")),
        subagent=clean_name(record.get("agent_type")) if record.get("agent") is not None else None,
    )


def load_capture(capture: Path, project_filter: ProjectFilter | None = None) -> Capture:
    """Requests, native steps, context peaks and compactions that pass
    `project_filter`; native records are filtered by their session."""
    out = Capture()
    skipped = tr.Skipped()
    for record in tr.capture_records(capture, skipped):
        kind = record.get("kind")
        if kind == "agent_spawn":
            session, agent = record.get("session"), record.get("spawned")
            if (
                isinstance(session, str)
                and isinstance(agent, str)
                and agent
                and (project_filter is None or project_filter.keep({"session.id": session}))
            ):
                # One agent id can be recorded twice: the first ticket and the first type win, so a later
                # record with none cannot erase them.
                first = out.spawns.get((session, agent))
                ticket, agent_type = spawn_ticket(record.get("ticket")), spawn_type(record.get("subagent_type"))
                if first is not None:
                    ticket, agent_type = first.ticket or ticket, first.agent_type or agent_type
                out.spawns[(session, agent)] = Spawn(ticket, agent_type)
            continue
        if kind in (PLUGIN_SET, PLUGIN_REGISTER):
            session = record.get("session")
            if (
                isinstance(session, str)
                and names_you_should_know(record)
                and (project_filter is None or project_filter.keep({"session.id": session}))
            ):
                out.you_should_know.add(text_of(session))
            continue
        if kind in ("step", "measure", "compaction"):
            if project_filter is not None and not project_filter.keep({"session.id": record.get("session")}):
                continue
            if kind == "step":
                out.steps.append(_native_step(record))
                continue
            if kind == "compaction":
                if record.get("skip") is None:
                    out.native_compactions.append(native_compaction_of(record))
            elif (sample := sample_of(record)) is not None:
                out.samples.append(sample)
            continue
        for name, attrs in tr.iter_log_events(record.get("payload")):
            if name not in (tr.API_REQUEST, COMPACTION):
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            if name == tr.API_REQUEST:
                out.requests.append(api_request_of(attrs))
            elif str(attrs.get("success")).lower() != "false":
                out.event_compactions.append(event_compaction_of(attrs))
    out.unreadable = skipped.lines
    return out


def _match_key(request: Request) -> tuple[object, ...]:
    return (request.session, request.prompt, request.model, request.tokens)


def filter_window(capture: Capture, since: datetime | None, until: datetime | None) -> tuple[Capture, int]:
    """The capture cut to requests and native steps stamped within `since`..`until`.
    Both bounds are inclusive, like `agent_usage_report --since/--until`, and
    naive timestamps count as local time. A request whose timestamp is missing or unparseable
    cannot be compared, so it is kept; the second return value counts those, so a report can
    say so (requests only: a native step with no usable stamp is kept but not counted, since it
    mirrors a request). Context peaks and compactions are per session and carry no time: they stay.

    A native step and the loop `api_request` that mirrors it (matched on session, prompt, model and
    the four token counts, first come first served; the request carries the cost) are one iteration
    and are windowed as a pair by the step's timestamp (as `--iterations` does). `build_report` counts a prompt's steps only when it has no loop request, so a
    step of a prompt that has loop requests but pairs with none is never counted and is dropped
    here too: the windows add up to the whole report."""
    if since is None and until is None:
        return capture, 0
    undated = 0

    def keep(item: Request, *, count: bool = False) -> bool:
        nonlocal undated
        stamp = parse_timestamp(item.timestamp)
        if stamp is None:
            undated += count
            return True
        return (since is None or stamp >= since) and (until is None or stamp <= until)

    event_prompts = {(r.session, r.prompt) for r in capture.requests if r.loop}
    mirrored: dict[tuple[object, ...], deque[Request]] = defaultdict(deque)
    for request in capture.requests:
        if request.loop:
            mirrored[_match_key(request)].append(request)
    kept_pairs: set[int] = set()
    paired: set[int] = set()
    steps: list[Request] = []
    for step in capture.steps:
        if (step.session, step.prompt) not in event_prompts:
            if keep(step):
                steps.append(step)
            continue
        candidates = mirrored.get(_match_key(step))
        if not candidates:
            continue  # the prompt has loop requests: the whole report never counts this step
        request = candidates.popleft()
        paired.add(id(request))
        if keep(step, count=True):
            kept_pairs.add(id(request))
            steps.append(step)
    requests = [r for r in capture.requests if (id(r) in kept_pairs if id(r) in paired else keep(r, count=True))]
    return dataclasses.replace(capture, requests=requests, steps=steps), undated


# --- sessions --------------------------------------------------------------------


def _share(part: float, whole: float) -> float | None:
    return part / whole * 100 if whole else None


@dataclasses.dataclass
class Session:
    session: str
    first: str = ""
    last: str = ""
    requests: int = 0
    iterations: int = 0
    sub_iterations: int = 0
    side_requests: int = 0
    cost: float = 0.0
    side_cost: float = 0.0
    hook_requests: int = 0  # side requests with query_source hook_prompt
    hook_cost: float = 0.0
    hook_label: str = ""
    uncosted: int = 0
    fresh: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_creation: float = 0.0
    models: Counter[str] = dataclasses.field(default_factory=Counter)
    efforts: Counter[str] = dataclasses.field(default_factory=Counter)
    peak_context: float | None = None
    compactions: int = 0  # main loop only (shared with --context)
    subagent_compactions: int = 0
    stamps: list[float] = dataclasses.field(default_factory=list, repr=False, compare=False)  # epoch seconds

    def active_seconds(self, max_gap: float) -> float:
        """The sum of the gaps between consecutive request stamps that are at most `max_gap` seconds: the
        time worked, with idle pauses left out. A request with no usable stamp is not in `stamps`."""
        ordered = sorted(self.stamps)
        return sum(gap for a, b in itertools.pairwise(ordered) if (gap := b - a) <= max_gap)

    @property
    def tokens(self) -> float:
        return self.fresh + self.output + self.cache_read + self.cache_creation

    @property
    def cache_percent(self) -> float | None:
        return _share(self.cache_read, self.fresh + self.cache_read + self.cache_creation)

    @property
    def side_request_share(self) -> float | None:
        return _share(self.side_requests, self.requests)

    @property
    def side_cost_share(self) -> float | None:
        return _share(self.side_cost, self.cost)

    @property
    def hook_cost_share(self) -> float | None:
        return _share(self.hook_cost, self.cost)

    def add(self, request: Request) -> None:
        self.requests += 1
        fresh, output, read, created = request.tokens
        self.fresh += fresh
        self.output += output
        self.cache_read += read
        self.cache_creation += created
        if request.cost is None:
            self.uncosted += 1
        else:
            self.cost += request.cost
        if request.loop:
            self.iterations += 1
            self.sub_iterations += request.sub
            self.models[request.model] += 1
            if request.effort:
                self.efforts[request.effort] += 1
        else:
            self.side_requests += 1
            self.side_cost += request.cost or 0.0
            if request.source == HOOK_PROMPT:
                self.hook_requests += 1
                self.hook_cost += request.cost or 0.0
        if (stamp := parse_timestamp(request.timestamp)) is not None:
            self.stamps.append(stamp.timestamp())
        if request.timestamp:
            self.first = min(self.first or request.timestamp, request.timestamp)
            self.last = max(self.last, request.timestamp)


@dataclasses.dataclass
class SessionReport:
    sessions: list[Session]
    unreadable: int = 0

    @property
    def uncosted(self) -> int:
        return sum(s.uncosted for s in self.sessions)


def build_report(capture: Capture) -> SessionReport:
    event_prompts = {(r.session, r.prompt) for r in capture.requests if r.loop}
    steps = [s for s in capture.steps if (s.session, s.prompt) not in event_prompts]
    sessions: dict[str, Session] = {}
    for request in [*capture.requests, *steps]:
        sessions.setdefault(request.session, Session(request.session)).add(request)
    main, sub = count_compactions(select_compactions(capture.native_compactions, capture.event_compactions))
    peaks: dict[str, list[Sample]] = {}
    for sample in capture.samples:
        peaks.setdefault(sample.session, []).append(sample)
    for session in sessions.values():
        session.hook_label = hook_prompt_label(session.session, capture.you_should_know)
        peak = peak_of(peaks.get(session.session, []))
        session.peak_context = None if peak is None else peak.percent
        session.compactions = main.get(session.session, 0)
        session.subagent_compactions = sub.get(session.session, 0)
    ordered = sorted(sessions.values(), key=lambda s: (s.last, s.session), reverse=True)
    return SessionReport(ordered, capture.unreadable)


# --- formatting --------------------------------------------------------------------


def _percent(value: float | None, digits: int = 0) -> str:
    return NONE if value is None else f"{value:.{digits}f}%"


def _cost(session: Session) -> str:
    return f"${session.cost:.2f}" if session.requests > session.uncosted else NONE


def _short(names: Counter[str]) -> str:
    """Names by request count, `claude-` dropped; `-` when there are none."""
    ordered = [name.removeprefix(MODEL_PREFIX) for name, _ in names.most_common()]
    return ",".join(ordered) or NONE


HEADER = [
    "session", "started", "reqs", "iters", "sub", "cost", "tokens", "cache%", "side%", "side$%", "hook$%", "peak", "compact", "models", "effort",
]  # fmt: skip
LEGEND = [
    "  reqs = model requests; iters = loop requests (main loop and subagents); sub = the part of iters made by",
    "  subagents the session launched (they share its session id, so they are in its row, not in a row of",
    "  their own); side% and side$% = share of requests and of cost spent on side requests (titles, hook",
    "  prompts, summaries, ...); hook$% = share of cost spent on hook_prompt requests (labelled below); cache% = cache read over all input tokens; peak = highest context fill seen;",
    "  compact = main-loop compactions that installed a summary; models and effort = of loop requests, most used first",
]


def _row(session: Session) -> list[str]:
    return [
        tr.session_label(session.session),
        tr.when_label(session.first),
        f"{session.requests:,}",
        f"{session.iterations:,}",
        f"{session.sub_iterations:,}",
        _cost(session),
        as_int(session.tokens),
        _percent(session.cache_percent, 1),
        _percent(session.side_request_share),
        _percent(session.side_cost_share),
        _percent(session.hook_cost_share, 1),
        _percent(session.peak_context),
        f"{session.compactions:,}",
        _short(session.models),
        _short(session.efforts),
    ]


def _total_row(sessions: list[Session]) -> list[str]:
    total = Session("")
    costed = 0
    for s in sessions:
        total.requests += s.requests
        total.iterations += s.iterations
        total.sub_iterations += s.sub_iterations
        total.side_requests += s.side_requests
        total.cost += s.cost
        total.side_cost += s.side_cost
        total.hook_requests += s.hook_requests
        total.hook_cost += s.hook_cost
        total.fresh += s.fresh
        total.output += s.output
        total.cache_read += s.cache_read
        total.cache_creation += s.cache_creation
        total.compactions += s.compactions
        total.subagent_compactions += s.subagent_compactions
        costed += s.requests - s.uncosted
    total.uncosted = total.requests - costed
    row = _row(total)
    row[0], row[1], row[11], row[13], row[14] = "total", plural(len(sessions), "session"), NONE, "", ""
    return row


def _hook_block(sessions: list[Session]) -> list[str]:
    """hook_prompt side requests per label, with their share of the cost of every session."""
    by_label: dict[str, list[float]] = {}
    for s in sessions:
        if s.hook_requests:
            row = by_label.setdefault(s.hook_label, [0.0, 0.0])
            row[0] += s.hook_requests
            row[1] += s.hook_cost
    if not by_label:
        return []
    total = sum(s.cost for s in sessions)
    rows = [
        [label, f"{n:,.0f}", f"${cost:.2f}", _percent(_share(cost, total), 1)]
        for label, (n, cost) in sorted(by_label.items(), key=lambda item: (-item[1][0], item[0]))
    ]
    return [
        "Hook-prompt side requests (query_source=hook_prompt)",
        *format_table(["label", "requests", "cost", "of cost"], rows, left=1),
        "  of cost = share of the cost of all sessions shown in the total row.",
        HOOK_PROMPT_NOTE,
    ]


def format_report(report: SessionReport, limit: int | None = None) -> str:
    sessions = report.sessions
    if not sessions:
        return with_unreadable("No model requests found.", report.unreadable)
    shown = sessions[: limit or DEFAULT_SESSION_LIMIT]
    rows = [*(_row(s) for s in shown), _total_row(sessions)]
    table = format_table(HEADER, rows, left=1, text=(1, 13, 14))
    # A rule above the total row, as wide as the table.
    table.insert(len(table) - 1, "  " + "-" * (len(table[0]) - 2))
    hidden = len(sessions) - len(shown)
    older = f"; {plural(hidden, 'older session')} not shown, raise --limit" if hidden else ""
    blocks = [
        [
            f"Per-session summary (one row per session, most recent first{older}; total covers every session)",
            *table,
            *LEGEND,
        ]
    ]
    hook = _hook_block(sessions)
    if hook:
        blocks.append(hook)
    notes = []
    if report.uncosted:
        notes.append(f"  {plural(report.uncosted, 'request')} without cost (left out of cost)")
    if any(s.peak_context is None for s in sessions):
        missing = sum(s.peak_context is None for s in sessions)
        notes.append(
            f"  peak context needs the plugin capture (measure records); {plural(missing, 'session')} without it"
        )
    if subagent := sum(s.subagent_compactions for s in sessions):
        notes.append(f"  {plural(subagent, 'subagent compaction')} not counted in compact (it counts the main loop)")
    if report.unreadable:
        notes.append(f"  {unreadable_note(report.unreadable)}")
    if notes:
        blocks.append(["Notes", *notes])
    return "\n\n".join("\n".join(block) for block in blocks)
