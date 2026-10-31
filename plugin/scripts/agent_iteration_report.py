#!/usr/bin/env python3
"""Iterations per agent run for `agent_telemetry.py report --iterations`.

An *iteration* is one model request in an agent's loop together with the tool
calls it asked for (its tool round). A *run* is the main loop answering one
prompt (`session.id`, `prompt.id`) or one subagent from start to finish (its
agent id). Every request re-reads the whole context, so a run that takes many
iterations, or keeps repeating the same failing call, pays for it every turn.

Sources, per prompt:

- the usage-observer plugin's native `step` records (one per loop request,
  with the agent id) and `tool` records, when the prompt has any `step`; the
  mirrored OTel events of that prompt are then used only to look up each
  step's `cost_usd` (exact match on session, prompt, model and the four token
  counts) and for side requests, never counted again;
- otherwise `claude_code.api_request` events, classified by `query_source`:
  `repl_main_thread` and `sdk` are the main loop, `agent:*` a subagent (type
  from `agent.name`), anything else a side request (session titles, hook
  prompts, agent summaries, ...) listed apart. OTel carries no agent id, so a
  subagent request is joined by `request_id` to the `requestId` of the
  project's subagent transcripts (`<session>/subagents/agent-<id>.jsonl`);
  without a match its requests form one run per session, prompt and type with
  agent id `?`. `tool_result` events go to the subagent whose transcript holds
  their `tool_use_id`, else to the prompt's main run.

Transcripts are read for ids, task labels (`#N` from a subagent's task
description) and, for repeated failing calls, an in-memory digest of each
call's input; no transcript, prompt or tool text is printed, and identity
attributes (`user.*`, `organization.id`, `vcs.*`) are never read.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tool_report as tr
import transcript_iterator as ti
from agent_usage_report import UNATTRIBUTED, parse_timestamp, ticket_of_dispatch
from safe_name import clean_name, subagent_label

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

    from agent_telemetry import ProjectFilter

MAIN = "main"
MAIN_SOURCES = ("repl_main_thread", "sdk")
AGENT_SOURCE_PREFIX = "agent:"
NO_AGENT_ID = "-"
UNKNOWN_AGENT_ID = "?"
NO_TASK = "-"
NO_SOURCE = "(none)"
SUBAGENT_COMPLETED = "subagent_completed"

NATIVE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# A run is flagged when it takes at least FLAG_MIN_ITERATIONS iterations and
# at least FLAG_MEDIAN_FACTOR times the median of runs of its agent type.
FLAG_MIN_ITERATIONS = 10
FLAG_MEDIAN_FACTOR = 3
# Failures of one call signature within one run that count as a retry loop.
REPEAT_MIN_FAILURES = 3

DEFAULT_RUN_LIMIT = 10
DEFAULT_SESSION_LIMIT = 20

# `query_source=hook_prompt` side requests: the built-in "You should know" plugin forking the session
# mid-turn (`$.model.fork`), not a prompt-type hook. The capture cannot say so itself: the usage-observer `plugin_set`
# record lists the `enabledPlugins` of the settings files (a built-in is not in them) and the hook inventory records
# events and matchers, never a hook type. So the label is inferred, and only where a session's `plugin_set` or
# `plugin_register` names the plugin; every other session keeps the narrower claim.
HOOK_PROMPT = "hook_prompt"
YOU_SHOULD_KNOW_LABEL = "you-should-know (inferred)"
PROMPT_HOOK_LABEL = "prompt hook (unattributed)"
YOU_SHOULD_KNOW_NAMES = frozenset({"you-should-know", "cc-plugin-you-should-know"})
PLUGIN_SET = "plugin_set"
PLUGIN_REGISTER = "plugin_register"
# A side-request key for a hook_prompt label: NUL cannot survive `_text`, so no capture field forges it.
HOOK_KEY = HOOK_PROMPT + "\0"
HOOK_PROMPT_NOTE = (
    f"  query_source={HOOK_PROMPT}: '{YOU_SHOULD_KNOW_LABEL}' = the session's plugin_set names the plugin that forks "
    f"the session mid-turn;\n  '{PROMPT_HOOK_LABEL}' = nothing in the capture names it (the hook inventory "
    "records no hook type, and a built-in is not in plugin_set)."
)
PROMPT_PREFIX = 8


# --- loading ---------------------------------------------------------------------


@dataclasses.dataclass
class Request:
    """One loop or side model request: a native `step` or an `api_request`."""

    session: str
    prompt: str
    model: str
    tokens: tuple[float, ...]
    cost: float | None
    timestamp: str
    source: str  # query_source, or "step" for a native record
    agent_type: str | None  # MAIN, a subagent type, or None for a side request
    agent: str | None = None  # native agent id
    request_id: str | None = None
    skill: str | None = None


@dataclasses.dataclass(frozen=True)
class Call:
    """One tool call: a native `tool` record or a `tool_result` event."""

    session: str
    prompt: str
    agent: str | None  # native agent id
    tool: str
    tool_use_id: str | None
    success: bool
    error_type: str | None
    input_size: float | None
    timestamp: str
    order: int


@dataclasses.dataclass
class Capture:
    subagent_sources: dict[str, str] = dataclasses.field(default_factory=dict)  # agent type -> where it is defined
    steps: list[Request] = dataclasses.field(default_factory=list)
    requests: list[Request] = dataclasses.field(default_factory=list)
    native_calls: list[Call] = dataclasses.field(default_factory=list)
    otel_calls: list[Call] = dataclasses.field(default_factory=list)
    you_should_know: set[str] = dataclasses.field(default_factory=set)  # sessions whose plugin_set names the plugin


def names_you_should_know(record: dict[str, Any]) -> bool:
    """Whether a `plugin_set` or `plugin_register` record names the You-should-know plugin: an exact, case-sensitive
    match of an id's name part (before `@`), so a look-alike name with an invisible character does not count."""
    if record.get("kind") == PLUGIN_SET:
        ids = record.get("enabled")
        return isinstance(ids, list) and any(
            isinstance(i, str) and i.split("@", 1)[0] in YOU_SHOULD_KNOW_NAMES for i in ids
        )
    name = record.get("plugin")
    return isinstance(name, str) and name.split("@", 1)[0] in YOU_SHOULD_KNOW_NAMES


def hook_prompt_label(session: str, you_should_know: set[str]) -> str:
    """The label of a session's `hook_prompt` requests."""
    return YOU_SHOULD_KNOW_LABEL if session in you_should_know else PROMPT_HOOK_LABEL


def _text(value: object, default: str = "?") -> str:
    """`value` as a label: each non-printable character becomes `?`, so no capture field can break a table row
    or send a terminal escape (the same rule as `agent_context_fill.text_of`)."""
    if value is None:
        return default
    text = tr.encodable(str(value))
    return text if text.isprintable() else "".join(c if c.isprintable() else "?" for c in text)


def subagent_type(value: object, default: str) -> str:
    """The label of a subagent: see `safe_name.subagent_label`."""
    return subagent_label(value, default)


def loop_type(source: str, agent_name: object) -> str | None:
    if source in MAIN_SOURCES:
        return MAIN
    if source.startswith(AGENT_SOURCE_PREFIX):
        return subagent_type(agent_name, source.rsplit(":", 1)[-1] or "?")
    return None


def _native_step(record: dict[str, Any]) -> Request:
    usage = record.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    agent = record.get("agent")
    return Request(
        session=_text(record.get("session")),
        prompt=_text(record.get("prompt")),
        model=_text(record.get("model")),
        tokens=tuple(tr.as_number(usage.get(key)) or 0.0 for key in NATIVE_TOKEN_KEYS),
        cost=None,
        timestamp=_text(record.get("ts"), ""),
        source="step",
        agent_type=MAIN if agent is None else subagent_type(record.get("agent_type"), "?"),
        agent=None if agent is None else _text(agent),
    )


def _native_call(record: dict[str, Any], order: int) -> Call:
    denied = record.get("denied") is True
    error = record.get("error") is True
    agent = record.get("agent")
    return Call(
        session=_text(record.get("session")),
        prompt=_text(record.get("prompt")),
        agent=None if agent is None else _text(agent),
        tool=_text(record.get("tool")),
        tool_use_id=tr.as_str(record.get("tool_use_id")),
        success=not (error or denied),
        error_type="denied" if denied else ("error" if error else None),
        input_size=tr.as_number(record.get("input_chars")),
        timestamp=_text(record.get("ts"), ""),
        order=order,
    )


def _api_request(attrs: dict[str, Any]) -> Request:
    source = _text(attrs.get("query_source"), NO_SOURCE)
    cost = tr.as_cost(attrs.get("cost_usd"))
    return Request(
        session=_text(attrs.get("session.id")),
        prompt=_text(attrs.get("prompt.id")),
        model=_text(attrs.get("model")),
        tokens=tuple(tr.as_number(attrs.get(key)) or 0.0 for key in tr.API_TOKEN_KEYS),
        cost=cost,
        timestamp=_text(attrs.get("event.timestamp"), ""),
        source=source,
        agent_type=loop_type(source, attrs.get("agent.name")),
        request_id=tr.as_str(attrs.get("request_id")),
        skill=clean_name(attrs.get("skill.name")),
    )


def _tool_result(attrs: dict[str, Any], order: int) -> Call:
    return Call(
        session=_text(attrs.get("session.id")),
        prompt=_text(attrs.get("prompt.id")),
        agent=None,
        tool=_text(attrs.get("tool_name")),
        tool_use_id=tr.as_str(attrs.get("tool_use_id")),
        success=str(attrs.get("success")).lower() == "true",
        error_type=tr.as_str(attrs.get("error_type")),
        input_size=tr.as_number(attrs.get("tool_input_size_bytes")),
        timestamp=_text(attrs.get("event.timestamp"), ""),
        order=order,
    )


def _note_source(capture: Capture, agent_type: str | None, source: str | None) -> None:
    """Remember where an agent type is defined (`built-in`, `custom`, ...)."""
    if agent_type and source:
        capture.subagent_sources[agent_type] = "built-in" if source == "builtin" else source


def _untyped_source(request: Request) -> bool:
    """`agent:custom`: OTel names no agent type (`agent.name` is `custom`)."""
    return len(request.source.split(":")) < 3


def _note_request_source(capture: Capture, agent_type: str | None, request: Request) -> None:
    """A subagent named by its transcript was defined where `query_source` says
    (`agent:<source>`); `subagent_completed` events keep priority."""
    parts = request.source.split(":")
    if agent_type and len(parts) >= 2 and agent_type not in capture.subagent_sources:
        _note_source(capture, agent_type, parts[1])


def load_capture(capture: Path, project_filter: ProjectFilter | None = None) -> Capture:
    """Native `step`/`tool` records and `api_request`/`tool_result` events that
    pass `project_filter`; native records are filtered by their session."""
    out = Capture()
    order = 0
    for record in tr.capture_records(capture):
        kind = record.get("kind")
        if kind in (PLUGIN_SET, PLUGIN_REGISTER):
            session = record.get("session")
            if (
                isinstance(session, str)
                and names_you_should_know(record)
                and (project_filter is None or project_filter.keep({"session.id": session}))
            ):
                out.you_should_know.add(_text(session))
            continue
        if kind in ("step", "tool"):
            if project_filter is not None and not project_filter.keep({"session.id": record.get("session")}):
                continue
            order += 1
            if kind == "step":
                out.steps.append(_native_step(record))
            else:
                out.native_calls.append(_native_call(record, order))
            continue
        for name, attrs in tr.iter_log_events(record.get("payload")):
            if name not in (tr.API_REQUEST, tr.TOOL_RESULT, SUBAGENT_COMPLETED):
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            if name == SUBAGENT_COMPLETED:
                _note_source(out, clean_name(attrs.get("agent_type")), clean_name(attrs.get("agent.source")))
                continue
            order += 1
            if name == tr.API_REQUEST:
                request = _api_request(attrs)
                parts = request.source.split(":")
                if parts[0] == AGENT_SOURCE_PREFIX.rstrip(":") and len(parts) >= 3:
                    _note_source(out, request.agent_type, parts[1])
                elif request.source == "agent:custom":  # type unknown unless a transcript joins
                    _note_source(out, request.agent_type, "custom")
                out.requests.append(request)
            else:
                out.otel_calls.append(_tool_result(attrs, order))
    return out


def _match_key(request: Request) -> tuple[object, ...]:
    return (request.session, request.prompt, request.model, request.tokens)


def _pair_steps(capture: Capture) -> dict[int, Request]:
    """Each native step's mirrored `api_request` (matched on session, prompt, model and the four
    token counts, first come first served), keyed by `id(step)`. `build_report` and
    `filter_window` both pair through this, so they cannot disagree."""
    native_prompts = {(step.session, step.prompt) for step in capture.steps}
    mirrored: dict[tuple[object, ...], deque[Request]] = defaultdict(deque)
    for request in capture.requests:
        if request.agent_type is not None and (request.session, request.prompt) in native_prompts:
            mirrored[_match_key(request)].append(request)
    pairs: dict[int, Request] = {}
    for step in capture.steps:
        candidates = mirrored.get(_match_key(step))
        if candidates:
            pairs[id(step)] = candidates.popleft()
    return pairs


def filter_window(capture: Capture, since: datetime | None, until: datetime | None) -> tuple[Capture, int]:
    """The capture cut to records stamped within `since`..`until`, with the
    same rules as `agent_session_report.filter_window`: both bounds inclusive, a naive
    timestamp is local time, and a record whose timestamp is missing or unparseable cannot be
    compared, so it is kept and counted in the second return value. Steps, requests and tool
    calls are all cut; subagent sources carry no time and stay.

    A native step and the `api_request` that mirrors it (which carries its cost) are one
    iteration and are windowed as a pair by the step's timestamp; the pair is
    counted once when that timestamp is unusable. A request with no step is cut by its own,
    unless its prompt has native steps in the whole capture: `build_report` drops such a request
    as mirrored, so it is dropped here too and the windows add up to the whole report."""
    if since is None and until is None:
        return capture, 0
    undated = 0

    def keep(item: Request | Call) -> bool:
        nonlocal undated
        stamp = parse_timestamp(item.timestamp)
        if stamp is None:
            undated += 1
            return True
        return (since is None or stamp >= since) and (until is None or stamp <= until)

    pairs = _pair_steps(capture)
    steps = [r for r in capture.steps if keep(r)]
    kept_pairs = {id(pairs[id(s)]) for s in steps if id(s) in pairs}
    paired = {id(r) for r in pairs.values()}
    mirrored = {(step.session, step.prompt) for step in capture.steps}

    def keep_request(request: Request) -> bool:
        if id(request) in paired:
            return id(request) in kept_pairs
        if request.agent_type is not None and (request.session, request.prompt) in mirrored:
            return False  # unpaired but mirrored by its prompt's steps: never counted
        return keep(request)

    requests = [r for r in capture.requests if keep_request(r)]
    return (
        dataclasses.replace(
            capture,
            steps=steps,
            requests=requests,
            native_calls=[c for c in capture.native_calls if keep(c)],
            otel_calls=[c for c in capture.otel_calls if keep(c)],
        ),
        undated,
    )


# --- transcripts -------------------------------------------------------------------


@dataclasses.dataclass
class TranscriptIndex:
    """What the report takes from transcripts: ids, ticket labels and input
    digests. Never any text."""

    agent_of_request: dict[str, str] = dataclasses.field(default_factory=dict)
    agent_of_use: dict[str, str] = dataclasses.field(default_factory=dict)
    input_of_use: dict[str, str] = dataclasses.field(default_factory=dict)
    ticket_of_agent: dict[str, str] = dataclasses.field(default_factory=dict)
    type_of_agent: dict[str, str] = dataclasses.field(default_factory=dict)


def _digest(tool_input: object) -> str:
    text = json.dumps(tool_input, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_transcripts(dirs: Iterable[Path], subagent_sessions: set[str], failure_sessions: set[str]) -> TranscriptIndex:
    """Subagent transcripts of `subagent_sessions` (request and tool-use ids,
    ticket labels) and every transcript of `failure_sessions` (input digests)."""
    index = TranscriptIndex()
    for directory in dirs:
        for context in ti.discover_transcripts(directory):
            session = context.session_id
            agent_id = None
            if context.kind == "subagent":
                if session not in subagent_sessions and session not in failure_sessions:
                    continue
                agent_id = context.transcript_path.stem.removeprefix("agent-")
                ticket = ticket_of_dispatch(context)
                if ticket != UNATTRIBUTED:
                    index.ticket_of_agent[agent_id] = f"#{ticket}"
                agent_type = context.agent_type  # `discover_transcripts` cleaned it
                if agent_type:
                    index.type_of_agent[agent_id] = subagent_type(agent_type, "?")
                for record in ti.read_jsonl(context.transcript_path):
                    request_id = record.get("requestId")
                    if record.get("type") == "assistant" and isinstance(request_id, str):
                        index.agent_of_request[request_id] = agent_id
            elif session not in failure_sessions:
                continue
            for event in ti.iter_events(context):
                if not isinstance(event, ti.ToolUseEvent):
                    continue
                if agent_id is not None:
                    index.agent_of_use[event.tool_use_id] = agent_id
                if session in failure_sessions:
                    index.input_of_use[event.tool_use_id] = _digest(event.tool_input)
    return index


# --- runs --------------------------------------------------------------------------


@dataclasses.dataclass
class ModelUse:
    iterations: int = 0
    tokens: float = 0.0
    cost: float = 0.0
    costed: int = 0


@dataclasses.dataclass
class Run:
    session: str
    prompt: str
    agent_type: str
    agent_id: str
    iterations: int = 0
    tokens: float = 0.0
    cost: float = 0.0
    costed: int = 0
    tool_calls: int = 0
    failures: int = 0
    last_seen: str = ""
    task: str = NO_TASK
    flagged: bool = False
    models: Counter[str] = dataclasses.field(default_factory=Counter)
    model_use: dict[str, ModelUse] = dataclasses.field(default_factory=lambda: defaultdict(ModelUse))
    skills: Counter[str] = dataclasses.field(default_factory=Counter)
    calls: list[Call] = dataclasses.field(default_factory=list)

    @property
    def model(self) -> str:
        return self.models.most_common(1)[0][0] if self.models else "?"

    def add_request(self, request: Request) -> None:
        self.iterations += 1
        self.tokens += sum(request.tokens)
        if request.cost is not None:
            self.cost += request.cost
            self.costed += 1
        self.models[request.model] += 1
        use = self.model_use[request.model]
        use.iterations += 1
        use.tokens += sum(request.tokens)
        if request.cost is not None:
            use.cost += request.cost
            use.costed += 1
        if request.skill:
            self.skills[request.skill] += 1
        self.last_seen = max(self.last_seen, request.timestamp)

    def add_call(self, call: Call) -> None:
        self.tool_calls += 1
        self.failures += not call.success
        self.calls.append(call)


@dataclasses.dataclass
class SideStats:
    requests: int = 0
    tokens: float = 0.0
    cost: float = 0.0


@dataclasses.dataclass(frozen=True)
class Repeat:
    run: Run
    tool: str
    failures: int
    error: str
    later_success: bool
    basis: str  # "input" (transcript input digest) or "size" (input size)


@dataclasses.dataclass
class IterationReport:
    runs: list[Run]
    side: dict[str, SideStats]
    repeats: list[Repeat]
    has_requests: bool
    native_iterations: int = 0
    otel_iterations: int = 0
    uncosted: int = 0
    unmatched_requests: int = 0
    unattributed_calls: int = 0
    subagent_sources: dict[str, str] = dataclasses.field(default_factory=dict)


def _signature(call: Call, index: TranscriptIndex) -> tuple[str, str, object]:
    digest = index.input_of_use.get(call.tool_use_id or "")
    if digest is not None:
        return ("input", call.tool, digest)
    return ("size", call.tool, call.input_size)


def _repeats(run: Run, index: TranscriptIndex) -> list[Repeat]:
    by_signature: dict[tuple[str, str, object], list[Call]] = defaultdict(list)
    for call in sorted(run.calls, key=lambda c: (c.timestamp, c.order)):
        by_signature[_signature(call, index)].append(call)
    out = []
    for (basis, tool, _), calls in by_signature.items():
        failed = [c for c in calls if not c.success]
        if len(failed) < REPEAT_MIN_FAILURES:
            continue
        first = calls.index(failed[0])
        errors = Counter(c.error_type or "?" for c in failed)
        out.append(
            Repeat(
                run=run,
                tool=tool,
                failures=len(failed),
                error=errors.most_common(1)[0][0],
                later_success=any(c.success for c in calls[first + 1 :]),
                basis=basis,
            )
        )
    return out


def _flag(runs: list[Run]) -> None:
    by_type: dict[str, list[int]] = defaultdict(list)
    for run in runs:
        by_type[run.agent_type].append(run.iterations)
    medians = {kind: statistics.median(values) for kind, values in by_type.items()}
    for run in runs:
        run.flagged = (
            run.iterations >= FLAG_MIN_ITERATIONS and run.iterations >= FLAG_MEDIAN_FACTOR * medians[run.agent_type]
        )


def build_report(capture: Capture, project_dir: Path | None) -> IterationReport:
    native_prompts = {(step.session, step.prompt) for step in capture.steps}
    side: dict[str, SideStats] = defaultdict(SideStats)
    pairs = _pair_steps(capture)
    otel_loop: list[Request] = []
    for request in capture.requests:
        if request.agent_type is None:
            side_key = request.source
            if side_key == HOOK_PROMPT:
                side_key = HOOK_KEY + hook_prompt_label(request.session, capture.you_should_know)
            stats = side[side_key]
            stats.requests += 1
            stats.tokens += sum(request.tokens)
            stats.cost += request.cost or 0.0
        elif (request.session, request.prompt) not in native_prompts:
            otel_loop.append(request)  # else mirrored by a native step: counted through that step
    otel_calls = [c for c in capture.otel_calls if (c.session, c.prompt) not in native_prompts]

    subagent_sessions = {r.session for r in otel_loop if r.agent_type != MAIN}
    subagent_sessions |= {s.session for s in capture.steps if s.agent is not None}
    failure_sessions = {c.session for c in [*capture.native_calls, *otel_calls] if not c.success}
    index = read_transcripts(tr.transcript_dirs(project_dir), subagent_sessions, failure_sessions)

    runs: dict[tuple[str, ...], Run] = {}

    def run_for(key: tuple[str, ...], request: Request, agent_id: str, agent_type: str | None = None) -> Run:
        if key not in runs:
            runs[key] = Run(request.session, request.prompt, str(agent_type or request.agent_type), agent_id)
        return runs[key]

    for step in capture.steps:
        match = pairs.get(id(step))
        if match is not None:
            step.cost, step.skill = match.cost, match.skill
        if step.agent is None:
            run_for((MAIN, step.session, step.prompt), step, NO_AGENT_ID).add_request(step)
        else:
            run_for(("agent", step.session, step.agent), step, step.agent).add_request(step)

    unmatched = 0
    unknown_prompts: set[tuple[str, str]] = set()
    for request in otel_loop:
        if request.agent_type == MAIN:
            run_for((MAIN, request.session, request.prompt), request, NO_AGENT_ID).add_request(request)
            continue
        agent_id = index.agent_of_request.get(request.request_id or "")
        if agent_id is not None:
            transcript_type = index.type_of_agent.get(agent_id) if _untyped_source(request) else None
            _note_request_source(capture, transcript_type, request)
            run_for(("agent", request.session, agent_id), request, agent_id, transcript_type).add_request(request)
            continue
        unmatched += 1
        unknown_prompts.add((request.session, request.prompt))
        key: tuple[str, ...] = (UNKNOWN_AGENT_ID, request.session, request.prompt, str(request.agent_type))
        run_for(key, request, UNKNOWN_AGENT_ID).add_request(request)

    unattributed = 0
    for call in capture.native_calls:
        key = (MAIN, call.session, call.prompt) if call.agent is None else ("agent", call.session, call.agent)
        if key in runs:
            runs[key].add_call(call)
        else:
            unattributed += 1
    for call in otel_calls:
        agent_id = index.agent_of_use.get(call.tool_use_id or "")
        if agent_id is not None:
            key = ("agent", call.session, agent_id)
        elif (call.session, call.prompt) in unknown_prompts:
            key = ()
        else:
            key = (MAIN, call.session, call.prompt)
        if key in runs:
            runs[key].add_call(call)
        else:
            unattributed += 1

    run_list = list(runs.values())
    for run in run_list:
        ticket = index.ticket_of_agent.get(run.agent_id) if run.agent_type != MAIN else None
        if ticket is not None:
            run.task = ticket
        elif run.skills:
            run.task = run.skills.most_common(1)[0][0]
    _flag(run_list)
    repeats = [repeat for run in run_list for repeat in _repeats(run, index)]
    iterations = sum(run.iterations for run in run_list)
    return IterationReport(
        runs=run_list,
        side=dict(side),
        repeats=repeats,
        has_requests=bool(capture.steps or capture.requests),
        native_iterations=len(capture.steps),
        otel_iterations=len(otel_loop),
        uncosted=iterations - sum(run.costed for run in run_list),
        unmatched_requests=unmatched,
        unattributed_calls=unattributed,
        subagent_sources=capture.subagent_sources,
    )


# --- formatting --------------------------------------------------------------------


def as_int(value: float) -> str:
    return f"{round(value):,}"


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def unreadable_note(count: int) -> str:
    """The report note for capture lines that were not JSON objects."""
    return f"{plural(count, 'unreadable line')} in the capture (a torn write or a corrupt line) left out"


def with_unreadable(text: str, count: int) -> str:
    """`text` (an empty-report message) followed by the unreadable-lines note, when there are any."""
    return f"{text}\n\nNotes\n  {unreadable_note(count)}" if count else text


def format_table(header: list[str], rows: list[list[str]], left: int = 1, text: tuple[int, ...] = ()) -> list[str]:
    """Columns padded to their widest cell; the first `left` columns and the
    columns in `text` are left-aligned, the rest right-aligned."""
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    out = []
    for row in [header, *rows]:
        cells = [
            cell.ljust(w) if i < left or i in text else cell.rjust(w)
            for i, (cell, w) in enumerate(zip(row, widths, strict=False))
        ]
        out.append(("  " + "  ".join(cells)).rstrip())
    return out


def _group_rows(runs: list[Run], label: Callable[[Run], str], order: list[str] | None = None) -> list[list[str]]:
    groups: dict[str, list[Run]] = defaultdict(list)
    for run in runs:
        groups[label(run)].append(run)
    keys = order if order is not None else sorted(groups, key=lambda k: (-sum(r.iterations for r in groups[k]), k))
    rows = []
    for key in keys:
        members = groups[key]
        counts = [r.iterations for r in members]
        iterations = sum(counts)
        costed = sum(r.costed for r in members)
        cost = sum(r.cost for r in members)
        rows.append(
            [
                key,
                f"{len(members):,}",
                f"{iterations:,}",
                f"{min(counts):,}",
                f"{statistics.median(counts):g}",
                f"{max(counts):,}",
                f"{sum(r.tool_calls for r in members) / iterations:.1f}",
                as_int(sum(r.tokens for r in members) / iterations),
                f"${cost / costed:.4f}" if costed else "-",
                f"{sum(r.flagged for r in members):,}",
            ]
        )
    return rows


GROUP_COLUMNS = ["runs", "iters", "min", "median", "max", "tools/iter", "tokens/iter", "cost/iter", "flagged"]


def session_lines(runs: list[Run], limit: int) -> tuple[list[str], int]:
    latest: dict[str, str] = {}
    for run in runs:
        key = tr.session_label(run.session)
        latest[key] = max(latest.get(key, ""), run.last_seen)
    ordered = sorted(latest, key=lambda s: (latest[s], s), reverse=True)
    return ordered[:limit], len(ordered)


def _busiest(runs: list[Run], limit: int) -> list[str]:
    ordered = sorted(runs, key=lambda r: (-r.iterations, -r.tokens, r.session, r.prompt))[:limit]
    rows = [
        [
            tr.session_label(run.session),
            run.prompt[:PROMPT_PREFIX],
            run.agent_type,
            run.agent_id,
            run.model,
            f"{run.iterations:,}",
            f"{run.tool_calls:,}",
            f"{run.failures:,}",
            as_int(run.tokens),
            f"${run.cost:.2f}" if run.costed else "-",
            run.task,
            "!" if run.flagged else "",
        ]
        for run in ordered
    ]
    header = [
        "session",
        "prompt",
        "type",
        "agent id",
        "model",
        "iters",
        "tools",
        "failed",
        "tokens",
        "cost",
        "task",
        "",
    ]
    return [
        f"Busiest runs (top {len(ordered)} by iterations; ! = flagged)",
        *format_table(header, rows, left=5, text=(10, 11)),
    ]


def _repeats_block(repeats: list[Repeat], limit: int) -> list[str]:
    title = (
        f"Repeated failing calls (>= {REPEAT_MIN_FAILURES} failures of one call within one run; "
        "basis: input = same transcript input, size = same input size)"
    )
    if not repeats:
        return [title.split(" (", maxsplit=1)[0] + ": none"]
    ordered = sorted(repeats, key=lambda r: (-r.failures, r.run.session, r.tool))
    rows = [
        [
            tr.session_label(r.run.session),
            r.run.agent_type,
            r.run.agent_id,
            r.tool,
            f"{r.failures:,}",
            r.error,
            "yes" if r.later_success else "no",
            r.basis,
        ]
        for r in ordered[:limit]
    ]
    header = ["session", "type", "agent id", "tool", "failures", "error", "later_ok", "basis"]
    lines = [title, *format_table(header, rows, left=4)]
    if len(ordered) > limit:
        lines.append(f"  ... {len(ordered) - limit:,} more (raise --limit)")
    return lines


def _side_block(side: dict[str, SideStats], loop_cost: float) -> list[str]:
    if not side:
        return ["Side requests: none"]
    ordered = sorted(side.items(), key=lambda item: (-item[1].requests, item[0]))
    total = loop_cost + sum(s.cost for s in side.values())
    rows = [
        [
            name.removeprefix(HOOK_KEY),
            f"{s.requests:,}",
            as_int(s.tokens),
            f"${s.cost:.2f}",
            "-" if total <= 0 else f"{s.cost / total * 100:.1f}%",
        ]
        for name, s in ordered
    ]
    lines = [
        "Side requests (not loop iterations, by query_source)",
        *format_table(["query_source", "requests", "tokens", "cost", "of cost"], rows),
        "  of cost = share of the cost of all costed requests, loop and side.",
    ]
    if any(name.startswith(HOOK_KEY) for name in side):
        lines.append(HOOK_PROMPT_NOTE)
    return lines


def format_report(report: IterationReport, limit: int | None = None) -> str:
    if not report.has_requests:
        return "No model requests found."
    runs = report.runs
    iterations = sum(r.iterations for r in runs)
    blocks: list[list[str]] = [
        [
            (
                "Iterations per agent run (iteration = one model request in an agent's loop plus the tool calls "
                "it asked for; run = the main loop answering one prompt, or one subagent)"
            ),
            (
                f"  runs {len(runs):,}, iterations {iterations:,} ({report.native_iterations:,} from native steps, "
                f"{report.otel_iterations:,} from api_request), tool calls {sum(r.tool_calls for r in runs):,}, "
                f"tokens {as_int(sum(r.tokens for r in runs))}, cost ${sum(r.cost for r in runs):.2f}"
            ),
        ]
    ]
    if runs:
        session_limit = limit or DEFAULT_SESSION_LIMIT
        shown, total = session_lines(runs, session_limit)
        session_rows = _group_rows(runs, lambda r: tr.session_label(r.session), order=shown)
        older = f"; {total - len(shown):,} older not shown, raise --limit" if total > len(shown) else ""
        blocks += [
            [
                (
                    f"By agent type (flagged: >= {FLAG_MIN_ITERATIONS} iterations and >= {FLAG_MEDIAN_FACTOR}x "
                    "the median of its type)"
                ),
                *format_table(["agent type", *GROUP_COLUMNS], _group_rows(runs, lambda r: r.agent_type)),
            ],
            [
                "By model (each run's most frequent model)",
                *format_table(["model", *GROUP_COLUMNS], _group_rows(runs, lambda r: r.model)),
            ],
            [
                "By task (#N from the subagent's task description, else the run's skill)",
                *format_table(["task", *GROUP_COLUMNS], _group_rows(runs, lambda r: r.task)),
            ],
            [f"By session (most recent first{older})", *format_table(["session", *GROUP_COLUMNS], session_rows)],
            _busiest(runs, limit or DEFAULT_RUN_LIMIT),
            _repeats_block(report.repeats, limit or DEFAULT_RUN_LIMIT),
        ]
    else:
        blocks.append(["No loop iterations found."])
    blocks.append(_side_block(report.side, sum(r.cost for r in runs)))
    notes = []
    if report.uncosted:
        notes.append(f"  {plural(report.uncosted, 'iteration')} without cost (left out of cost/iter)")
    if report.unmatched_requests:
        notes.append(
            f"  {plural(report.unmatched_requests, 'subagent request')} without a transcript match "
            "(counted in runs with agent id ?)"
        )
    if report.unattributed_calls:
        notes.append(
            f"  {plural(report.unattributed_calls, 'tool call')} not attributed to a run "
            "(prompt has subagent runs without an agent id, or no run)"
        )
    if notes:
        blocks.append(["Notes", *notes])
    return "\n\n".join("\n".join(block) for block in blocks)
