#!/usr/bin/env python3
"""Fixed context cost per session for `agent_telemetry.py report --fixed-context`.

Before the model reads a word of the operator's prompt, every request already
carries a system prompt, the first user message's context blocks (`claudeMd`,
...), the descriptions of the tools it may call and whatever the engine
injects (skill and agent listings, hook output, reminders). This view sizes
that fixed cost per session, so a heavy part can be trimmed or deferred.

Sources, all from the usage-observer plugin's native records (the OTLP capture
has none of them), in characters, about four per token (`CHARS_PER_TOKEN`):

- `prompt_compose`: the system prompt, per section. The first record of a
  session is used; `prompt.compose` carries no agent id, so a subagent's own
  system prompt is not sized.
- `prompt_context`: the first user message's context blocks, per block.
- `tool_describe`: one record per tool per session. Tools behind ToolSearch
  (`deferred`) are not in the request until the model loads them, so they are
  left out of the fixed cost and only counted.
- `prompt_attachment`: injected messages by type and origin. The main loop's
  count toward the fixed cost when they came before its first request ended
  (`attach`); later ones (`total_tokens_reminder` every step, listings that
  change) are shown in the detail block only. A subagent's attachments are
  listed per agent and not added to the session's fixed cost.

The baseline request is the first main-loop request (`step` with no agent, or
`api_request` event) at or after the session's first `prompt_compose` record; with
no compose record, the earliest main-loop request. A session resumed under a newer
plugin has an older segment whose requests the plugin did not size: taking the
session's earliest request would pair that segment's input with the later
segment's sizes. One baseline per session (the first compose's) keeps one row per
session; later resume segments are not separate rows. The baseline's input tokens
(fresh plus cache read plus cache creation) are the yardstick: `fixed%` is the
fixed tokens over them. Attachments count when they fall in [first compose,
baseline request).

A session with no baseline request (a startup-only session: compose and no
main-loop request after it) is left out of the rows and counted in a note; a session
with no compose record and no request at all (tool or attachment records only) is
counted in its own note, which does not mention a compose record.

Nothing is read from transcripts, and no prompt text, tool text, identity
attribute (`user.*`, `organization.id`, `vcs.*`) or project label is printed;
the plugin records sizes only.
"""

from __future__ import annotations

import dataclasses
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_session_report as sr
import agent_tool_report as tr
from agent_context_fill import text_of
from agent_iteration_report import NATIVE_TOKEN_KEYS, as_int, format_table, plural
from safe_name import clean_name

if TYPE_CHECKING:
    from agent_telemetry import ProjectFilter

NONE = sr.NONE
CHARS_PER_TOKEN = 4
MAX_TOOLS = 10  # listed tools shown, largest first
MAX_AGENTS = 20  # subagent rows shown, largest first
MAIN_SOURCES = ("repl_main_thread", "sdk")  # `api_request` query sources of the main loop
AGENT_PREFIX = 8
FIXED_KINDS = ("prompt_compose", "prompt_context", "prompt_attachment", "tool_describe")


# --- loading ---------------------------------------------------------------------


@dataclasses.dataclass
class Attachment:
    ts: str
    type: str
    origin: str
    event: str | None
    agent: str | None
    agent_type: str | None
    chars: int


@dataclasses.dataclass
class Tool:
    chars: int
    deferred: bool
    provider: str


@dataclasses.dataclass
class FirstRequest:
    ts: str
    input: float  # fresh input plus cache read plus cache creation


@dataclasses.dataclass
class RawSession:
    sections: dict[str, int] | None = None
    blocks: dict[str, int] | None = None
    tools: dict[str, Tool] = dataclasses.field(default_factory=dict)
    attachments: list[Attachment] = dataclasses.field(default_factory=list)
    requests: list[FirstRequest] = dataclasses.field(default_factory=list)  # main loop; only the baseline once loaded
    compose_ts: str | None = None
    agent_first: dict[str, FirstRequest] = dataclasses.field(default_factory=dict)
    first_ts: str = ""
    last_ts: str = ""


@dataclasses.dataclass
class Capture:
    sessions: dict[str, RawSession] = dataclasses.field(default_factory=dict)

    def session(self, session: str) -> RawSession:
        return self.sessions.setdefault(session, RawSession())


def _size(value: object) -> int:
    number = tr.as_number(value)
    return int(number) if number is not None else 0


def _sized(items: object, key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get(key) is not None:
            out[str(item[key])] = out.get(str(item[key]), 0) + _size(item.get("chars"))
    return out


def _request(ts: str, tokens: list[float]) -> FirstRequest:
    return FirstRequest(ts, tokens[0] + tokens[2] + tokens[3])


def _earlier(current: FirstRequest | None, ts: str, tokens: list[float]) -> FirstRequest:
    """The request that started first: `current`, or one at `ts` with `tokens`
    (fresh input, output, cache read, cache creation)."""
    candidate = FirstRequest(ts, tokens[0] + tokens[2] + tokens[3])
    return candidate if current is None or ts < current.ts else current


def _stamp(raw: RawSession, ts: str) -> None:
    if ts:
        raw.first_ts = min(raw.first_ts or ts, ts)
        raw.last_ts = max(raw.last_ts, ts)


def _native(out: Capture, record: dict[str, Any], kind: str) -> None:
    raw = out.session(text_of(record.get("session")))
    ts = text_of(record.get("ts"), "")
    if kind == "step":
        usage = record.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        tokens = [tr.as_number(usage.get(key)) or 0.0 for key in NATIVE_TOKEN_KEYS]
        agent = tr.as_str(record.get("agent"))
        if agent is None:
            raw.requests.append(_request(ts, tokens))
        else:
            raw.agent_first[agent] = _earlier(raw.agent_first.get(agent), ts, tokens)
        return
    _stamp(raw, ts)
    if kind == "prompt_compose" and raw.sections is None:
        raw.sections = _sized(record.get("sections"), "id")
        raw.compose_ts = ts
    elif kind == "prompt_context" and raw.blocks is None:
        raw.blocks = _sized(record.get("blocks"), "name")
    elif kind == "tool_describe":
        raw.tools[text_of(record.get("tool"))] = Tool(
            _size(record.get("chars")), record.get("deferred") is True, text_of(record.get("provider"))
        )
    elif kind == "prompt_attachment":
        raw.attachments.append(
            Attachment(
                ts,
                text_of(record.get("attachment")),
                text_of(record.get("origin")),
                tr.as_str(record.get("origin_event")),
                tr.as_str(record.get("agent")),
                clean_name(tr.as_str(record.get("agent_type"))),
                _size(record.get("chars")),
            )
        )


def load_capture(capture: Path, project_filter: ProjectFilter | None = None) -> Capture:
    """The fixed context records, main-loop first requests and the sessions that
    pass `project_filter`; native records are filtered by their session."""
    out = Capture()
    for record in tr.capture_records(capture):
        kind = record.get("kind")
        if kind in (*FIXED_KINDS, "step"):
            if project_filter is None or project_filter.keep({"session.id": record.get("session")}):
                _native(out, record, kind)
            continue
        for name, attrs in tr.iter_log_events(record.get("payload")):
            if name != tr.API_REQUEST or attrs.get("query_source") not in MAIN_SOURCES:
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            request = sr.api_request_of(attrs)
            raw = out.session(request.session)
            raw.requests.append(_request(request.timestamp, [request.tokens[0], 0.0, *request.tokens[2:]]))
    for raw in out.sessions.values():
        first = baseline(raw)
        raw.requests = [first] if first else []
    return out


# --- aggregation -----------------------------------------------------------------


@dataclasses.dataclass
class AgentFixed:
    agent_type: str
    count: int = 0
    chars: int = 0
    first_input: float | None = None


@dataclasses.dataclass
class SessionFixed:
    session: str
    first: str
    last: str
    sections: dict[str, int] | None
    blocks: dict[str, int] | None
    tools: dict[str, int]  # listed tools only
    providers: dict[str, str]
    deferred: int
    deferred_chars: int
    attachments: list[Attachment]  # main loop
    first_ts: str | None  # when the baseline request ended; None when not found
    compose_ts: str | None  # the first compose record's time; None without one
    first_input: float | None
    agents: dict[str, AgentFixed]

    @property
    def system(self) -> int | None:
        return None if self.sections is None else sum(self.sections.values())

    @property
    def context(self) -> int | None:
        return None if self.blocks is None else sum(self.blocks.values())

    @property
    def tools_chars(self) -> int:
        return sum(self.tools.values())

    def in_first(self, attachment: Attachment) -> bool:
        after_compose = self.compose_ts is None or attachment.ts >= self.compose_ts
        return after_compose and (self.first_ts is None or attachment.ts < self.first_ts)

    @property
    def attachments_first(self) -> int:
        return sum(a.chars for a in self.attachments if self.in_first(a))

    @property
    def attachments_all(self) -> int:
        return sum(a.chars for a in self.attachments)

    @property
    def fixed(self) -> int:
        return (self.system or 0) + (self.context or 0) + self.tools_chars + self.attachments_first

    @property
    def tokens(self) -> float:
        return self.fixed / CHARS_PER_TOKEN

    @property
    def share(self) -> float | None:
        return self.tokens / self.first_input * 100 if self.first_input else None


@dataclasses.dataclass
class FixedReport:
    sessions: list[SessionFixed]
    startup_only: int = 0  # sessions with fixed records and no baseline request, not listed
    startup_no_compose: int = 0  # of those, the ones with no compose record either


def baseline(raw: RawSession) -> FirstRequest | None:
    """The first main-loop request at or after the first compose record (the
    earliest one without a compose record); None when there is none."""
    later = [r for r in raw.requests if raw.compose_ts is None or r.ts >= raw.compose_ts]
    return min(later, key=lambda r: r.ts, default=None)


def _session_fixed(session: str, raw: RawSession) -> SessionFixed:
    agents: dict[str, AgentFixed] = {}
    main = []
    for attachment in raw.attachments:
        if attachment.agent is None:
            main.append(attachment)
            continue
        agent = agents.setdefault(attachment.agent, AgentFixed(attachment.agent_type or "?"))
        agent.count += 1
        agent.chars += attachment.chars
        if agent.agent_type == "?" and attachment.agent_type:
            agent.agent_type = attachment.agent_type
    for agent_id, agent in agents.items():
        agent_request = raw.agent_first.get(agent_id)
        agent.first_input = agent_request.input if agent_request else None
    first = baseline(raw)
    listed = {name: tool for name, tool in raw.tools.items() if not tool.deferred}
    return SessionFixed(
        session=session,
        first=raw.first_ts,
        last=raw.last_ts,
        sections=raw.sections,
        blocks=raw.blocks,
        tools={name: tool.chars for name, tool in listed.items()},
        providers={name: tool.provider for name, tool in listed.items()},
        deferred=len(raw.tools) - len(listed),
        deferred_chars=sum(t.chars for t in raw.tools.values() if t.deferred),
        attachments=main,
        first_ts=first.ts if first else None,
        compose_ts=raw.compose_ts,
        first_input=first.input if first else None,
        agents=agents,
    )


def build_report(capture: Capture) -> FixedReport:
    found = [
        _session_fixed(session, raw)
        for session, raw in capture.sessions.items()
        if raw.sections is not None or raw.blocks is not None or raw.tools or raw.attachments
    ]
    sessions = [s for s in found if s.first_ts is not None]
    sessions.sort(key=lambda s: (s.last, s.session), reverse=True)
    startup = [s for s in found if s.first_ts is None]
    return FixedReport(sessions, len(startup), sum(s.compose_ts is None for s in startup))


# --- formatting ------------------------------------------------------------------


def _chars(value: float | None) -> str:
    return NONE if value is None else as_int(value)


def _mean(values: list[int]) -> str:
    return as_int(sum(values) / len(values))


SUMMARY_HEADER = ["session", "started", "system", "context", "tools", "attach", "fixed", "~tok", "input", "fixed%"]
SUMMARY_LEGEND = [
    "  Sizes are characters, about four per token. tools = listed tool descriptions (deferred ones left out);",
    "  attach = main-loop attachments before its first request ended; fixed = system + context + tools + attach;",
    "  ~tok = fixed / 4; input = the first request's input tokens (fresh + cache read + cache creation);",
    "  fixed% = ~tok over input",
]


def _summary_block(sessions: list[SessionFixed], older: str) -> list[str]:
    rows = [
        [
            tr.session_label(s.session),
            tr.when_label(s.first),
            _chars(s.system),
            _chars(s.context),
            as_int(s.tools_chars),
            as_int(s.attachments_first),
            as_int(s.fixed),
            as_int(s.tokens),
            _chars(s.first_input),
            NONE if s.share is None else f"{s.share:.0f}%",
        ]
        for s in sessions
    ]
    table = format_table(SUMMARY_HEADER, rows, left=1, text=(1,))
    return [f"Fixed context per session (most recent first{older})", *table, *SUMMARY_LEGEND]


def _by_id(sessions: list[SessionFixed], parts: str) -> list[list[str]]:
    """Rows of (id, sessions with it, mean chars), largest mean first, for the
    system prompt sections (`sections`) or the context blocks (`blocks`)."""
    sizes: dict[str, list[int]] = defaultdict(list)
    for s in sessions:
        for name, chars in (getattr(s, parts) or {}).items():
            sizes[name].append(chars)
    ordered = sorted(sizes.items(), key=lambda item: (-sum(item[1]) / len(item[1]), item[0]))
    return [[name, f"{len(values):,}", _mean(values)] for name, values in ordered]


def _id_block(title: str, label: str, rows: list[list[str]]) -> list[str]:
    return [title, *format_table([label, "sessions", "mean chars"], rows, left=1)] if rows else []


@dataclasses.dataclass
class AttachmentGroup:
    sessions: set[str] = dataclasses.field(default_factory=set)
    count: int = 0
    chars: int = 0
    first: int = 0


def _attachment_block(sessions: list[SessionFixed]) -> list[str]:
    groups: dict[tuple[str, str, str], AttachmentGroup] = defaultdict(AttachmentGroup)
    for s in sessions:
        for a in s.attachments:
            group = groups[(a.type, a.origin, a.event or NONE)]
            group.sessions.add(s.session)
            group.count += 1
            group.chars += a.chars
            group.first += a.chars if s.in_first(a) else 0
    ordered = sorted(groups.items(), key=lambda item: (-item[1].chars, item[0]))
    rows = [[*key, f"{len(g.sessions):,}", f"{g.count:,}", as_int(g.chars), as_int(g.first)] for key, g in ordered]
    if not rows:
        return []
    header = ["attachment", "origin", "event", "sessions", "count", "chars", "first request"]
    return ["Main-loop attachments by type and origin", *format_table(header, rows, left=3)]


def _tool_block(sessions: list[SessionFixed]) -> list[str]:
    sizes: dict[str, list[int]] = defaultdict(list)
    providers: dict[str, str] = {}
    for s in sessions:
        for name, chars in s.tools.items():
            sizes[name].append(chars)
            providers[name] = s.providers[name]
    if not sizes:
        return []
    ordered = sorted(sizes.items(), key=lambda item: (-sum(item[1]) / len(item[1]), item[0]))
    rows = [[name, providers[name], f"{len(v):,}", _mean(v)] for name, v in ordered[:MAX_TOOLS]]
    table = format_table(["tool", "provider", "sessions", "mean chars"], rows, left=2)
    if len(ordered) > MAX_TOOLS:
        table.append(f"  +{len(ordered) - MAX_TOOLS} more listed tools")
    return ["Listed tools, largest first", *table]


def _agent_block(sessions: list[SessionFixed]) -> list[str]:
    found = [(s, agent_id, a) for s in sessions for agent_id, a in s.agents.items()]
    if not found:
        return []
    found.sort(key=lambda item: (-item[2].chars, item[0].session, item[1]))
    rows = [
        [
            tr.session_label(s.session),
            a.agent_type,
            agent_id[:AGENT_PREFIX],
            f"{a.count:,}",
            as_int(a.chars),
            as_int(a.chars / CHARS_PER_TOKEN),
            _chars(a.first_input),
        ]
        for s, agent_id, a in found[:MAX_AGENTS]
    ]
    table = format_table(["session", "type", "agent", "attach", "chars", "~tok", "input"], rows, left=3)
    if len(found) > MAX_AGENTS:
        table.append(f"  +{len(found) - MAX_AGENTS} more subagents")
    return ["Subagent attachments (not in the session's fixed cost; input = the subagent's first request)", *table]


def _notes(sessions: list[SessionFixed], startup_only: int = 0, no_compose: int = 0) -> list[str]:
    notes = []
    if startup_only > no_compose:
        count = plural(startup_only - no_compose, "startup-only session")
        notes.append(f"  {count} (no main-loop request after its compose record) not listed")
    if no_compose:
        notes.append(f"  {plural(no_compose, 'startup-only session')} (no main-loop request) not listed")
    no_system = sum(s.sections is None for s in sessions)
    if no_system:
        notes.append(f"  {plural(no_system, 'session')} without a system prompt record (counted as 0)")
    deferred = sum(s.deferred for s in sessions)
    if deferred:
        chars = sum(s.deferred_chars for s in sessions)
        notes.append(f"  {plural(deferred, 'deferred tool')} ({as_int(chars)} chars) left out: not sent until loaded")
    return notes


def format_report(report: FixedReport, limit: int | None = None) -> str:
    if not report.sessions and report.startup_only:
        return f"No session with a main-loop request; {plural(report.startup_only, 'startup-only session')} not listed."
    if not report.sessions:
        return "No fixed context records found; they need the plugin capture."
    shown = report.sessions[: limit or sr.DEFAULT_SESSION_LIMIT]
    hidden = len(report.sessions) - len(shown)
    older = f"; {plural(hidden, 'older session')} not shown, raise --limit" if hidden else ""
    blocks = [
        _summary_block(shown, older),
        _id_block("System prompt sections (mean over the sessions shown)", "section", _by_id(shown, "sections")),
        _id_block("Context blocks (mean over the sessions shown)", "block", _by_id(shown, "blocks")),
        _attachment_block(shown),
        _tool_block(shown),
        _agent_block(shown),
    ]
    notes = _notes(shown, report.startup_only, report.startup_no_compose)
    if notes:
        blocks.append(["Notes", *notes])
    return "\n\n".join("\n".join(block) for block in blocks if block)
