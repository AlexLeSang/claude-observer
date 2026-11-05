#!/usr/bin/env python3
"""Costliest prompts, with the cost of the subagents each prompt dispatched rolled up.

    scripts/agent_telemetry.py report --prompts [--json] [--limit N] [--since T] [--until T]

A subagent's `api_request` carries the `prompt.id` current when it ran, which for a background subagent is
a later prompt than the one that dispatched it. This view charges each request to its *owner* prompt:

- a request without `agent_id` belongs to its own `prompt.id` (main-loop cost);
- a request with `agent_id` belongs to the prompt of the root `agent_spawn` (made from the main loop) reached
  by following `spawned == agent_id`, then each spawn's `agent` (the parent), upward.

Everything that cannot be tied that way (no `agent_id`, no spawn record for the agent, a chain that breaks above
a known spawn or loops, a spawn with no prompt, a request with no prompt.id) is put in the session's *untied*
row and in no prompt: it is never guessed onto a prompt. Coverage, tied subagent cost over all subagent-like
cost, is printed for the whole capture and for sessions on Claude Code 2.1.310 and later (the first version
that puts `agent_id` on a request) side by side, never the overall figure alone.

Two checks are printed: the partition (owner totals plus untied equal the deduplicated request sum, within float rounding) and
the measure agreement (that sum against the session's last `measure.cost_usd`, counted within $0.01, never an
assertion). A prompt is named by the first 8 characters of its session id, its start time and the first 8
characters of its prompt id (`--json`: the full ids). The view prints no prompt text, subagent description or
type name, tool text, error text or path.

Standard library only, plus the sibling report modules.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import agent_tool_report as tr
from agent_usage_report import parse_timestamp
from safe_name import clean_name

if TYPE_CHECKING:
    from pathlib import Path

    from agent_telemetry import ProjectFilter

DEFAULT_LIMIT = 10
MEASURE_TOLERANCE_USD = 0.01
PARTITION_TOLERANCE_USD = 1e-9  # absolute floor of the partition check; the check is also relative to the sum
PARTITION_TOLERANCE_REL = 1e-9  # float error grows with the sum: 300 requests of $1e6 each differ by about 1e-8
NOTIFY_GAP_MS = 2000.0  # a prompt starting this soon after a background subagent finished is likely its notification
MAX_CHAIN = 32
AGENT_ID_FROM = (2, 1, 310)  # the first Claude Code version that puts agent_id on api_request
AGENT_ID_FROM_TEXT = "2.1.310"
VERSION_RE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}")
AGENT_SOURCE_PREFIX = "agent:"
UNKNOWN = "unknown"
FLAGGED_SHOWN = 10

# Why a request is in a session's untied row.
NO_ID = "no_id"  # subagent-like, but no agent_id on the request
NO_SPAWN = "no_spawn"  # agent_id with no agent_spawn in its session
PARTIAL = "partial"  # the spawn chain breaks above a known spawn, or loops
NO_PROMPT = "no_prompt"  # the root spawn has no prompt
NO_PROMPT_ID = "no_prompt_id"  # a main-loop request without a prompt.id
CAUSES = (NO_ID, NO_SPAWN, PARTIAL, NO_PROMPT, NO_PROMPT_ID)
CAUSE_TEXT = {
    NO_SPAWN: "no agent_spawn for the agent",
    PARTIAL: "spawn chain breaks or loops",
    NO_PROMPT: "root spawn has no prompt",
    NO_PROMPT_ID: "main-loop request without a prompt.id",
}


@dataclass
class Request:
    prompt: str | None
    agent: str | None
    cost: float
    like: bool  # agent_id, agent.name or an `agent:` query source
    stamp: datetime | None


@dataclass
class Spawn:
    parent: str | None
    prompt: str | None
    background: bool


@dataclass
class Session:
    version: str = UNKNOWN
    requests: list[Request] = field(default_factory=list)
    spawns: dict[str, Spawn] = field(default_factory=dict)
    root_spawns: list[tuple[str | None, bool]] = field(default_factory=list)  # (prompt, background) per dispatch
    measures: list[tuple[datetime | None, float]] = field(default_factory=list)
    starts: dict[str, datetime] = field(default_factory=dict)  # prompt id -> its user_prompt time
    origins: dict[str, str] = field(default_factory=dict)  # prompt id -> recorded origin kind
    done: list[tuple[datetime, str | None]] = field(default_factory=list)  # subagent_completed (time, agent_id)


@dataclass
class Capture:
    sessions: dict[str, Session] = field(default_factory=lambda: defaultdict(Session))
    unparsed: int = 0
    mirrored: int = 0  # api_request repeats (same request id) dropped
    uncosted: int = 0  # api_request with no usable cost, left out


@dataclass
class PromptRow:
    session: str
    prompt: str
    start: datetime | None
    main: float = 0.0
    sub: float = 0.0
    dispatched: int = 0
    background: int = 0
    label: str = ""

    @property
    def owner(self) -> float:
        return self.main + self.sub


@dataclass
class SessionRow:
    session: str
    version: str
    total: float = 0.0
    owner: float = 0.0
    untied: float = 0.0
    untied_n: int = 0
    untied_by: Counter[str] = field(default_factory=Counter)
    like: float = 0.0
    like_n: int = 0
    tied: float = 0.0
    tied_n: int = 0
    last_measure: float | None = None

    @property
    def coverage(self) -> float | None:
        return _ratio(self.tied, self.like)


@dataclass
class Result:
    prompts: list[PromptRow]
    sessions: list[SessionRow]
    depths: Counter[int]
    versions: dict[str, tuple[int, int, int]]  # version -> (sessions, subagent-like requests, with agent_id)
    differs_n: int  # requests whose prompt.id is not their owner's
    differs_usd: float
    partition_off: list[str]
    windowed: bool


def _ratio(part: float, whole: float) -> float | None:
    return None if whole <= 0 else part / whole


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.1f}%"


def _usd(value: float) -> str:
    return f"${value:.2f}"


def _s(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _stamp(*values: object) -> datetime | None:
    for value in values:
        if isinstance(value, str):
            parsed = parse_timestamp(value)
            if parsed is not None:
                return parsed
    return None


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")) if VERSION_RE.fullmatch(version) else ()


def has_agent_id(version: str) -> bool:
    """Whether a session on `version` is expected to carry `agent_id` (unknown versions are not)."""
    key = version_key(version)
    return bool(key) and key >= AGENT_ID_FROM


def short(value: str) -> str:
    return (clean_name(value) or "?")[:8]


def full(value: str) -> str:
    return clean_name(value) or "?"


def read(source: Path, project_filter: ProjectFilter | None = None) -> Capture:
    """The capture's sessions: costed requests (deduplicated), agent spawns, prompt times and origins, measures."""
    out = Capture()
    skipped = tr.Skipped()
    seen: set[tuple[str, str]] = set()
    for record in tr.capture_records(source, skipped):
        if "payload" in record:
            for name, attrs in tr.iter_log_events(record.get("payload")):
                _event(out, seen, name, attrs, record, project_filter)
        else:
            _native(out, record, project_filter)
    out.unparsed = skipped.lines
    return out


def _event(
    out: Capture,
    seen: set[tuple[str, str]],
    name: str | None,
    attrs: dict[str, Any],
    record: dict[str, Any],
    project_filter: ProjectFilter | None,
) -> None:
    session = _s(attrs.get("session.id"))
    if session is None or name not in ("api_request", "user_prompt", "subagent_completed"):
        return
    if project_filter is not None and not project_filter.keep(attrs):
        return
    state = out.sessions[session]
    stamp = _stamp(attrs.get("event.timestamp"), record.get("ts"))
    if name == "user_prompt":
        prompt = _s(attrs.get("prompt.id"))
        if prompt is not None and stamp is not None:
            state.starts.setdefault(prompt, stamp)
    elif name == "subagent_completed":
        if stamp is not None:
            state.done.append((stamp, _s(attrs.get("agent_id"))))
    else:
        cost = tr.as_cost(attrs.get("cost_usd"))
        if cost is None:
            out.uncosted += 1
            return
        key = _s(attrs.get("request_id")) or _s(attrs.get("client_request_id"))
        if key is not None:
            if (session, key) in seen:
                out.mirrored += 1
                return
            seen.add((session, key))
        source = attrs.get("query_source")
        agent = _s(attrs.get("agent_id"))
        like = (
            agent is not None
            or _s(attrs.get("agent.name")) is not None
            or (isinstance(source, str) and source.startswith(AGENT_SOURCE_PREFIX))
        )
        state.requests.append(Request(_s(attrs.get("prompt.id")), agent, cost, like, stamp))


def _native(out: Capture, record: dict[str, Any], project_filter: ProjectFilter | None) -> None:
    session = _s(record.get("session"))
    kind = record.get("kind")
    if session is None or kind not in ("session_start", "agent_spawn", "measure", "prompt_submit"):
        return
    if project_filter is not None and not project_filter.keep({"session.id": session}):
        return
    state = out.sessions[session]
    if kind == "session_start":
        version = record.get("version")
        if isinstance(version, str) and VERSION_RE.fullmatch(version):
            state.version = version
    elif kind == "agent_spawn":
        parent, prompt, background = _s(record.get("agent")), _s(record.get("prompt")), record.get("background") is True
        if parent is None and record.get("denied") is not True:
            state.root_spawns.append((prompt, background))
        spawned = _s(record.get("spawned"))
        if spawned is not None:
            state.spawns.setdefault(spawned, Spawn(parent, prompt, background))
    elif kind == "measure":
        cost = tr.as_cost(record.get("cost_usd"))
        if cost is not None:
            state.measures.append((_stamp(record.get("ts")), cost))
    else:  # prompt_submit
        prompt, origin = _s(record.get("prompt")), _s(record.get("origin"))
        if prompt is not None and origin is not None:
            state.origins.setdefault(prompt, origin)


def owner_of(spawns: dict[str, Spawn], agent: str) -> tuple[str, str | None, int]:
    """(status, owner prompt, chain depth) of a request made by `agent`. `tied`: the root spawn names the
    prompt; `partial`: the chain breaks above a known spawn or loops (the last prompt seen is a nested spawn's,
    so it is no owner); `none`: no spawn for the agent; `no_prompt`: the root spawn has no prompt."""
    current = agent
    seen: set[str] = set()
    last: Spawn | None = None
    for depth in range(1, MAX_CHAIN + 1):
        spawn = spawns.get(current)
        if spawn is None or current in seen:
            return ("partial", None, depth - 1) if last is not None else ("none", None, 0)
        seen.add(current)
        last = spawn
        if spawn.parent is None:
            return ("tied", spawn.prompt, depth) if spawn.prompt is not None else ("no_prompt", None, depth)
        current = spawn.parent
    return "partial", None, MAX_CHAIN


def _within(stamp: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    if stamp is None:  # cannot be compared: kept, as the other views do
        return True
    return (since is None or stamp >= since) and (until is None or stamp <= until)


def _label(state: Session, prompt: str, start: datetime | None) -> str:
    origin = state.origins.get(prompt)
    if origin is not None:  # recorded: the 2 s rule is only the fallback for captures written before the record
        return "notification" if origin == "task-notification" else ""
    if start is not None:
        for done, agent in state.done:
            spawn = state.spawns.get(agent) if agent is not None else None
            if spawn is None or not spawn.background:  # only a background subagent's completion queues a notification
                continue
            if 0 <= (start - done).total_seconds() * 1000.0 <= NOTIFY_GAP_MS:
                return "likely notification"
    return ""


def build(capture: Capture, since: datetime | None = None, until: datetime | None = None) -> Result:
    windowed = since is not None or until is not None
    prompts: list[PromptRow] = []
    sessions: list[SessionRow] = []
    depths: Counter[int] = Counter()
    versions: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    differs_n = 0
    differs_usd = 0.0
    partition_off: list[str] = []
    for sid, state in capture.sessions.items():
        requests = [r for r in state.requests if _within(r.stamp, since, until)]
        if not requests:
            continue
        row = SessionRow(sid, state.version)
        by_prompt: dict[str, PromptRow] = {}
        first_seen: dict[str, datetime] = {}

        def prompt_row(
            prompt: str, sid: str = sid, state: Session = state, by_prompt: dict[str, PromptRow] = by_prompt
        ) -> PromptRow:
            if prompt not in by_prompt:
                by_prompt[prompt] = PromptRow(sid, prompt, state.starts.get(prompt))
            return by_prompt[prompt]

        for request in requests:
            row.total += request.cost
            if request.stamp is not None and request.prompt is not None:
                first_seen[request.prompt] = min(request.stamp, first_seen.get(request.prompt, request.stamp))
            if request.like:
                row.like += request.cost
                row.like_n += 1
            version_row = versions[state.version]
            if request.like:
                version_row[1] += 1
            if request.agent is not None:
                version_row[2] += 1
            owner: str | None
            if request.agent is None:
                if request.like:
                    row.untied_by[NO_ID] += 1
                    owner = None
                elif request.prompt is None:
                    row.untied_by[NO_PROMPT_ID] += 1
                    owner = None
                else:
                    prompt_row(request.prompt).main += request.cost
                    row.owner += request.cost
                    continue
            else:
                status, owner, depth = owner_of(state.spawns, request.agent)
                depths[depth] += 1
                if status == "tied" and owner is not None:
                    target = prompt_row(owner)
                    target.sub += request.cost
                    row.owner += request.cost
                    row.tied += request.cost
                    row.tied_n += 1
                    if owner != request.prompt:
                        differs_n += 1
                        differs_usd += request.cost
                    continue
                row.untied_by[{"none": NO_SPAWN, "partial": PARTIAL}.get(status, NO_PROMPT)] += 1
            row.untied += request.cost
            row.untied_n += 1
        for prompt, spawned_bg in ((p, bg) for p, bg in state.root_spawns if p is not None):
            if prompt in by_prompt:
                by_prompt[prompt].dispatched += 1
                by_prompt[prompt].background += spawned_bg
        for prompt, item in by_prompt.items():
            item.start = item.start or first_seen.get(prompt)
            item.label = _label(state, prompt, item.start)
        if not windowed and state.measures:
            row.last_measure = _last_measure(state.measures)
        if not math.isclose(
            sum(r.cost for r in requests),
            row.owner + row.untied,
            rel_tol=PARTITION_TOLERANCE_REL,
            abs_tol=PARTITION_TOLERANCE_USD,
        ):
            partition_off.append(sid)
        versions[state.version][0] += 1
        prompts.extend(by_prompt.values())
        sessions.append(row)
    prompts.sort(key=lambda p: (-p.owner, p.start or datetime.max.replace(tzinfo=timezone.utc), p.session, p.prompt))
    return Result(
        prompts,
        sessions,
        depths,
        {v: (c[0], c[1], c[2]) for v, c in versions.items()},
        differs_n,
        differs_usd,
        partition_off,
        windowed,
    )


def _last_measure(measures: list[tuple[datetime | None, float]]) -> float:
    """The cost of the latest measure; among equal or undated stamps the one written last."""
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    return max(enumerate(measures), key=lambda m: (m[1][0] or oldest, m[0]))[1][1]


def _when(stamp: datetime | None) -> str:
    return "?" if stamp is None else stamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _version_order(version: str) -> tuple[int, tuple[int, ...]]:
    return (version == UNKNOWN, version_key(version))


def _coverage_groups(result: Result) -> tuple[SessionRow, SessionRow]:
    """(all sessions, sessions on Claude Code 2.1.310 and later) summed."""
    everything, recent = SessionRow("", "all"), SessionRow("", AGENT_ID_FROM_TEXT)
    for row in result.sessions:
        for target in (everything, recent) if has_agent_id(row.version) else (everything,):
            target.like += row.like
            target.like_n += row.like_n
            target.tied += row.tied
            target.tied_n += row.tied_n
            target.untied += row.untied
            target.untied_n += row.untied_n
            target.total += row.total
            target.untied_by.update(row.untied_by)
    return everything, recent


def _agreement(result: Result) -> tuple[int, int, list[tuple[SessionRow, float]]]:
    checked = [r for r in result.sessions if r.last_measure is not None]
    outside = [
        (r, (r.owner + r.untied) - r.last_measure)
        for r in checked
        if r.last_measure is not None and abs(r.owner + r.untied - r.last_measure) > MEASURE_TOLERANCE_USD
    ]
    return len(checked) - len(outside), len(checked), outside


def _untied_text(row: SessionRow) -> str:
    parts = []
    for cause in CAUSES:
        if row.untied_by[cause]:
            text = _no_id_label(row.version) if cause == NO_ID else CAUSE_TEXT[cause]
            parts.append(f"{text}: {row.untied_by[cause]}")
    return "; ".join(parts)


def _no_id_label(version: str) -> str:
    if version == "all":
        return f"no agent_id (Claude Code before {AGENT_ID_FROM_TEXT} or unknown version)"
    key = version_key(version)
    if key and key < AGENT_ID_FROM:
        return f"no agent_id (Claude Code before {AGENT_ID_FROM_TEXT})"
    if not key:
        return "no agent_id (version unknown)"
    return "no agent_id"


def format_table(result: Result, capture: Capture, limit: int) -> str:
    out: list[str] = []
    ranked = result.prompts
    if not result.sessions:
        return "no costed requests found: nothing to rank"
    out.append(
        f"Costliest prompts by owner cost (the prompt's own requests plus the requests of the subagents it dispatched): "
        f"{len(ranked)} prompts, top {min(limit, len(ranked))}"
        + ("" if ranked else "; no prompt owns any cost (every costed request is untied)")
    )
    header = (
        "session",
        "start (UTC)",
        "prompt",
        "owner $",
        "main $",
        "subagent $",
        "sub share",
        "subagents",
        "bg",
        "label",
    )
    rows = [header]
    rows.extend(
        (
            short(item.session),
            _when(item.start),
            short(item.prompt),
            _usd(item.owner),
            _usd(item.main),
            _usd(item.sub),
            _pct(_ratio(item.sub, item.owner)),
            str(item.dispatched),
            str(item.background),
            item.label,
        )
        for item in ranked[:limit]
    )
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    for index, line in enumerate(rows):
        out.append(
            "  ".join(
                cell.ljust(widths[i]) if i in (0, 1, 2, 9) else cell.rjust(widths[i]) for i, cell in enumerate(line)
            ).rstrip()
        )
        if index == 0:
            out.append("  ".join("-" * w for w in widths))
    everything, recent = _coverage_groups(result)
    out += [
        "",
        "Subagent-like cost (an agent_id, an agent name or an agent: query source) and what ties it to a prompt",
    ]
    out.append(
        f"  subagent-like cost: {_usd(everything.like)} = {_pct(_ratio(everything.like, everything.total))} of request cost "
        f"{_usd(everything.total)}"
    )
    out.append(
        f"  all sessions: coverage {_pct(everything.coverage)} ({everything.tied_n} of {everything.like_n} requests tied, "
        f"{_usd(everything.tied)} of {_usd(everything.like)} subagent-like cost)"
    )
    recent_sessions = sum(1 for r in result.sessions if has_agent_id(r.version))
    out.append(
        f"  sessions on Claude Code {AGENT_ID_FROM_TEXT} and later ({recent_sessions} sessions): coverage "
        f"{_pct(recent.coverage)} ({recent.tied_n} of {recent.like_n} requests tied, "
        f"{_usd(recent.tied)} of {_usd(recent.like)})"
    )
    out.append(f"  tied row: {_usd(everything.tied)} ({everything.tied_n} requests)")
    out.append(
        f"  untied row: {_usd(everything.untied)} ({everything.untied_n} requests); subagent-like part "
        f"{_pct(_ratio(everything.like - everything.tied, everything.like))} of subagent-like cost; "
        f"{_untied_text(everything) or 'none'}"
    )
    out.append(
        "  spawn chain depth of subagent requests (0 = no spawn recorded; a broken or promptless chain counts the spawns found): "
        + (", ".join(f"{d}={n}" for d, n in sorted(result.depths.items())) or "none")
    )
    sub_like = everything.like
    out.append(
        f"  {result.differs_n} requests ({_pct(_ratio(result.differs_usd, sub_like))} of subagent-like cost) carry a prompt.id "
        "other than their owner's: grouping by prompt.id would charge them to another prompt"
    )
    out.append("per Claude Code version (sessions / subagent-like requests / with agent_id):")
    for version in sorted(result.versions, key=_version_order):
        s, like_n, ids = result.versions[version]
        out.append(f"  {version}: {s} / {like_n} / {ids}")
    out += ["", "Sessions with an untied row (largest first)"]
    untied_sessions = sorted((r for r in result.sessions if r.untied_n), key=lambda r: (-r.untied, r.session))
    out.append(f"  {len(untied_sessions)} of {len(result.sessions)} sessions have untied cost")
    for row in untied_sessions[:limit]:
        out.append(
            f"  {short(row.session)}  v{row.version}  untied {_usd(row.untied)} ({row.untied_n} requests)  "
            f"coverage {_pct(row.coverage)}  {_untied_text(row)}"
        )
    out += ["", "Checks"]
    if result.partition_off:
        out.append(
            f"  partition: FAILED in {len(result.partition_off)} sessions (owner totals + untied differ from the request sum)"
        )
    else:
        out.append(
            f"  partition: owner totals + untied = deduplicated request sum in all {len(result.sessions)} sessions"
        )
    if result.windowed:
        out.append("  measure agreement: not checked in a --since/--until window (the measure is cumulative)")
    else:
        within, checked, outside = _agreement(result)
        out.append(
            f"  measure agreement: {within} of {checked} sessions within ${MEASURE_TOLERANCE_USD:.2f} of the last measure cost"
        )
        for row, delta in sorted(outside, key=lambda o: -abs(o[1]))[:FLAGGED_SHOWN]:
            out.append(f"    outside: {short(row.session)} requests differ from the measure by {_usd(delta)}")
        if len(outside) > FLAGGED_SHOWN:
            out.append(f"    ... and {len(outside) - FLAGGED_SHOWN} more outside")
    out.append(
        f"  unparsed lines: {capture.unparsed}; mirrored requests counted once: {capture.mirrored}; "
        f"requests without a usable cost left out: {capture.uncosted}"
    )
    return "\n".join(out)


JSON_DIGITS = 6  # decimal places of every float in --json: dollars and shares, not binary sum noise


def _rounded(value: Any) -> Any:
    """`value` with every float rounded to JSON_DIGITS places (ints, bools, strings and None pass through)."""
    if isinstance(value, float):
        return round(value, JSON_DIGITS) + 0.0  # + 0.0: a tiny negative rounds to -0.0, which must not print
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    return value


def to_json(result: Result, capture: Capture, limit: int) -> str:
    everything, recent = _coverage_groups(result)
    within, checked, outside = _agreement(result)

    def group(row: SessionRow) -> dict[str, Any]:
        return {
            "subagent_like_usd": row.like,
            "subagent_like_requests": row.like_n,
            "tied_usd": row.tied,
            "tied_requests": row.tied_n,
            "coverage": row.coverage,
            "untied_usd": row.untied,
            "untied_requests": row.untied_n,
        }

    body = {
        "prompts": [
            {
                "session": full(p.session),
                "start": None if p.start is None else _when(p.start),
                "prompt": full(p.prompt),
                "owner_usd": p.owner,
                "main_usd": p.main,
                "subagent_usd": p.sub,
                "subagent_share": _ratio(p.sub, p.owner),
                "subagents": p.dispatched,
                "background": p.background,
                "label": p.label or None,
            }
            for p in result.prompts[:limit]
        ],
        "prompt_count": len(result.prompts),
        "coverage": {"all": group(everything), f"from_{AGENT_ID_FROM_TEXT}": group(recent)},
        "sessions": [
            {
                "session": full(r.session),
                "version": r.version,
                "request_usd": r.total,
                "owner_usd": r.owner,
                **group(r),
                "untied_causes": {c: r.untied_by[c] for c in CAUSES if r.untied_by[c]},
                "last_measure_usd": r.last_measure,
            }
            for r in sorted(result.sessions, key=lambda r: (-r.untied, r.session))[:limit]
        ],
        "versions": {
            v: {"sessions": s, "subagent_like_requests": n, "with_agent_id": i}
            for v, (s, n, i) in sorted(result.versions.items(), key=lambda kv: _version_order(kv[0]))
        },
        "depths": {str(d): n for d, n in sorted(result.depths.items())},
        "prompt_id_differs": {"requests": result.differs_n, "usd": result.differs_usd},
        "checks": {
            "partition_failed_sessions": len(result.partition_off),
            "measure_sessions_within_tolerance": None if result.windowed else within,
            "measure_sessions_checked": None if result.windowed else checked,
            "measure_outside": []
            if result.windowed
            else [{"session": full(r.session), "delta_usd": d} for r, d in outside],
        },
        "unparsed_lines": capture.unparsed,
        "mirrored_requests": capture.mirrored,
        "uncosted_requests": capture.uncosted,
    }
    return json.dumps(_rounded(body), indent=2, allow_nan=False)
