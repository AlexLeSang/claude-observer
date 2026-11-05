"""Prompt-cache misses of one session, by loop and by what the capture shows before each miss.

Pure functions over plain values; standard library and the repo's own bars only. Nothing here reads a
capture: `agent_session_report.py` turns its records into `Req`, `Step` and `Compaction` and calls
`analyse`. No id, model name or text of the capture is ever returned in a line: ids are dict keys only.

The miss rule is Claude Code's own: within one loop, a request is a miss when its read falls short of
the previous request's read plus write (`expected`) by more than 5% and by at least 2,000 tokens, with
no compaction of that loop in between (with one in between it is an expected rebuild). A loop's first
request is never a miss.

Loops (operator decision 2026-11-01): the main thread (query_source `sdk`, `repl_main_thread` or none),
one loop per `agent_id`, and, for a subagent request that carries no `agent_id`, the agent of the ONE
native step of the session with the same four token counts within 120 s either way. Two such steps are
ambiguous and unusable; no match leaves the request "not attributable". The key is never `query_source`:
measured where `agent_id` is the truth it invents 94% false misses by count. Step-matched misses are a
LOWER BOUND (recall about 79% of the `agent_id` tokens on 2026-11-01) and sit beside the totals, never
inside them.

What a miss followed, first that holds: a model switch; a gap past the known lifetime of the last write;
an effort change (both requests carry `effort`; a fact, not a cause); else unexplained.
"""

from __future__ import annotations

import bisect
import dataclasses
import itertools
from collections import defaultdict
from typing import TYPE_CHECKING

import decision_hints as dh
import model_prices as mp

if TYPE_CHECKING:
    from collections.abc import Callable

MISS_SHARE = dh.MISS_SHARE
MISS_TOKENS = dh.MISS_TOKENS
TTL_5M = dh.TTL_5M
TTL_1H = dh.TTL_1H
STEP_JOIN_SECONDS = 120.0
STEP_RECALL_TOKENS = 0.786  # measured 2026-11-01 on requests that carry agent_id: 78.6% of their miss tokens
MAIN_SOURCES = frozenset({"sdk", "repl_main_thread", ""})
AGENT_PREFIX = "agent:"
PER_MTOK = 1_000_000.0

MODEL, GAP, EFFORT, UNEXPLAINED = "model", "gap", "effort", "unexplained"
CAUSES = (MODEL, GAP, EFFORT, UNEXPLAINED)  # the order a miss is labelled in
FOLLOWED = {
    MODEL: "followed a model switch",
    GAP: "followed a gap past the cache lifetime",
    EFFORT: "followed an effort change",
}

MAIN, AGENT, ANY_AGENT = "main", "agent", "any_agent"  # whose compaction it is

Tokens = tuple[int, int, int, int]  # input, output, cache read, cache creation


@dataclasses.dataclass(frozen=True)
class Req:
    """One `api_request` of a session: the fields the miss rule reads."""

    t: float  # epoch seconds
    source: str  # query_source
    agent_id: str
    model: str
    effort: str  # "" when unset
    tokens: Tokens
    cc5m: int | None
    cc1h: int | None
    cost: float = 0.0
    request_id: str = ""  # a repeat of one id inside a session is one request


@dataclasses.dataclass(frozen=True)
class Step:
    t: float
    agent: str | None  # None: a main-loop step, which names no subagent
    tokens: Tokens


@dataclasses.dataclass(frozen=True)
class Compaction:
    t: float
    scope: str  # MAIN; AGENT (native, `agent` is its id); ANY_AGENT (an event names a type, not an id)
    agent: str = ""


@dataclasses.dataclass(frozen=True)
class Miss:
    cause: str
    shortfall: int
    cost: float | None  # None: the model has no price


@dataclasses.dataclass
class Row:
    """The loops of one kind: pairs compared, misses, expected rebuilds."""

    loops: int = 0
    requests: int = 0
    pairs: int = 0
    rebuilds: int = 0
    misses: list[Miss] = dataclasses.field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(m.shortfall for m in self.misses)

    @property
    def cost(self) -> float:
        return sum(m.cost or 0.0 for m in self.misses)

    @property
    def unpriced(self) -> int:
        return sum(m.cost is None for m in self.misses)

    def of(self, cause: str) -> tuple[int, int]:
        """(misses, tokens) that followed `cause`."""
        mine = [m for m in self.misses if m.cause == cause]
        return len(mine), sum(m.shortfall for m in mine)

    def add(self, other: Row) -> None:
        self.loops += other.loops
        self.requests += other.requests
        self.pairs += other.pairs
        self.rebuilds += other.rebuilds
        self.misses.extend(other.misses)


@dataclasses.dataclass
class Analysis:
    headline: Row = dataclasses.field(default_factory=Row)  # main loop and agent_id loops
    stepped: Row = dataclasses.field(default_factory=Row)  # keyless subagents matched to a step: a lower bound
    not_attributable: int = 0  # keyless subagent requests with no unique step match: requests, no misses
    gaps_past_lifetime: int = 0  # main-loop gaps past a known lifetime, miss or not
    gaps_unknown: int = 0  # main-loop gaps over 5 min where no write of the loop carried a split
    split_checked: int = 0  # requests with a non-zero split
    split_violations: int = 0  # of those, 5m + 1h != cache_creation_tokens

    def add(self, other: Analysis) -> None:
        self.headline.add(other.headline)
        self.stepped.add(other.stepped)
        self.not_attributable += other.not_attributable
        self.gaps_past_lifetime += other.gaps_past_lifetime
        self.gaps_unknown += other.gaps_unknown
        self.split_checked += other.split_checked
        self.split_violations += other.split_violations


# --- the step join (the probe's `_step_index`, `_steps_for`, `_step_agent`) ---------------------------------

StepIndex = dict[Tokens, tuple[list[float], list[str | None]]]


def step_index(steps: list[Step]) -> StepIndex:
    """Steps by their four token counts, each group sorted by time: (times, agents)."""
    grouped: dict[Tokens, list[tuple[float, str | None]]] = defaultdict(list)
    for step in steps:
        grouped[step.tokens].append((step.t, step.agent))
    out: StepIndex = {}
    for tokens, found in grouped.items():
        found.sort(key=lambda pair: pair[0])
        out[tokens] = ([t for t, _ in found], [a for _, a in found])
    return out


def steps_for(index: StepIndex, req: Req) -> list[str | None]:
    """The agents of every step with `req`'s four token counts within 120 s either way (bounds inclusive)."""
    times, agents = index.get(req.tokens, ([], []))
    start = bisect.bisect_left(times, req.t - STEP_JOIN_SECONDS - 1)  # widened, then the exact rule below
    end = bisect.bisect_right(times, req.t + STEP_JOIN_SECONDS + 1)
    return [agents[i] for i in range(start, end) if abs(times[i] - req.t) <= STEP_JOIN_SECONDS]


def step_agent(index: StepIndex, req: Req) -> str | None:
    """The agent of the one step that matches `req`; None when there is none, two or more, or it names no agent."""
    found = steps_for(index, req)
    return found[0] if len(found) == 1 else None


# --- compactions ---------------------------------------------------------------------------------------------


class _Compactions:
    def __init__(self, compactions: list[Compaction]) -> None:
        self.main: list[float] = []
        self.any_agent: list[float] = []
        self.by_agent: dict[str, list[float]] = defaultdict(list)
        for c in sorted(compactions, key=lambda c: c.t):
            if c.scope == MAIN:
                self.main.append(c.t)
            elif c.scope == ANY_AGENT:
                self.any_agent.append(c.t)
            elif c.scope == AGENT:
                self.by_agent[c.agent].append(c.t)

    @staticmethod
    def _between(times: list[float], lo: float, hi: float) -> bool:
        return bisect.bisect_right(times, hi) > bisect.bisect_right(times, lo)

    def excuses_main(self, lo: float, hi: float) -> bool:
        return self._between(self.main, lo, hi)

    def excuses_agent(self, agent: str, lo: float, hi: float) -> bool:
        """A native compaction of this agent, or one an event names by type (it excuses every subagent)."""
        return self._between(self.any_agent, lo, hi) or self._between(self.by_agent.get(agent, []), lo, hi)

    def agent_excuser(self, agent: str) -> Callable[[float, float], bool]:
        return lambda lo, hi: self.excuses_agent(agent, lo, hi)


# --- the miss rule -------------------------------------------------------------------------------------------


def shortfall_cost(model: str, ttl: float, shortfall: int, prompt_tokens: float) -> float | None:
    """`shortfall * (write rate - read rate)` in USD for `model` at the lifetime `ttl`; None when unpriced.
    Write rates are 1.25x (5 min) and 2x (1 h) the input price."""
    prices = mp.model_prices(model, prompt_tokens)
    if prices is None:
        return None
    price_in, price_read = prices
    factor = mp.CACHE_WRITE_1H if ttl == TTL_1H else mp.CACHE_WRITE_5M
    return shortfall * (price_in * factor - price_read) / PER_MTOK


def label(prev: Req, now: Req, lifetime: float | None) -> str:
    """What the miss `now` followed: the first that holds."""
    if now.model != prev.model:
        return MODEL
    if lifetime is not None and now.t - prev.t > lifetime:
        return GAP
    if now.effort and prev.effort and now.effort != prev.effort:
        return EFFORT
    return UNEXPLAINED


def _judge(
    reqs: list[Req], excused: Callable[[float, float], bool], *, is_main: bool, out: Row, gaps: Analysis | None
) -> dict[int, Miss]:
    """Pairs, misses and rebuilds of one loop (`reqs` sorted by time) added to `out`; the misses by id of the
    missing request. `excused(lo, hi)` says whether a compaction of the loop lies in (lo, hi]."""
    found: dict[int, Miss] = {}
    known: float | None = None
    for prev, now in itertools.pairwise(reqs):
        known = dh.lifetime(prev.cc5m, prev.cc1h) or known  # the lifetime of the loop's last non-zero split
        out.pairs += 1
        if is_main and gaps is not None and now.t - prev.t > TTL_5M:
            if known is None:
                gaps.gaps_unknown += 1
            elif now.t - prev.t > known:
                gaps.gaps_past_lifetime += 1
        expected = prev.tokens[2] + prev.tokens[3]
        shortfall = expected - now.tokens[2]
        if shortfall <= MISS_SHARE * expected or shortfall < MISS_TOKENS:
            continue
        if excused(prev.t, now.t):
            out.rebuilds += 1
            continue
        prompt = now.tokens[0] + now.tokens[2] + now.tokens[3]
        cost = shortfall_cost(now.model, known if known is not None else TTL_5M, shortfall, prompt)
        miss = Miss(label(prev, now, known), shortfall, cost)
        out.misses.append(miss)
        found[id(now)] = miss
    return found


def _kind(req: Req) -> str:
    if req.agent_id:
        return "agent"
    if req.source.startswith(AGENT_PREFIX):
        return "keyless"
    return "main" if req.source in MAIN_SOURCES else "side"


def prepare(reqs: list[Req]) -> list[Req]:
    """Requests by time (stable), a repeated request id dropped: a mirrored repeat is one request."""
    seen: set[str] = set()
    kept: list[Req] = []
    for req in reqs:
        if req.request_id:
            if req.request_id in seen:
                continue
            seen.add(req.request_id)
        kept.append(req)
    kept.sort(key=lambda r: r.t)
    return kept


def analyse(reqs: list[Req], steps: list[Step], compactions: list[Compaction]) -> Analysis:
    """The misses of one session."""
    out = Analysis()
    ordered = prepare(reqs)
    for req in ordered:
        if req.cc5m is not None and req.cc1h is not None and (req.cc5m or req.cc1h):
            out.split_checked += 1
            out.split_violations += req.cc5m + req.cc1h != req.tokens[3]
    index = _Compactions(
        [*compactions, *(Compaction(r.t, MAIN) for r in ordered if _kind(r) == "side" and r.source == "compact")]
    )
    main: list[Req] = []
    agents: dict[str, list[Req]] = defaultdict(list)
    keyless: list[Req] = []
    for req in ordered:
        kind = _kind(req)
        if kind == "main":
            main.append(req)
        elif kind == "agent":
            agents[req.agent_id].append(req)
        elif kind == "keyless":
            keyless.append(req)
    if main:
        out.headline.loops += 1
        out.headline.requests += len(main)
        _judge(main, index.excuses_main, is_main=True, out=out.headline, gaps=out)
    for agent, loop in agents.items():
        out.headline.loops += 1
        out.headline.requests += len(loop)
        _judge(loop, index.agent_excuser(agent), is_main=False, out=out.headline, gaps=None)
    loops, out.not_attributable = step_loops(keyless, steps)
    for agent, loop in loops.items():  # the step names the agent: its native compaction excuses it, as for agent_id
        out.stepped.loops += 1
        out.stepped.requests += len(loop)
        _judge(loop, index.agent_excuser(agent), is_main=False, out=out.stepped, gaps=None)
    return out


def step_loops(keyless: list[Req], steps: list[Step]) -> tuple[dict[str, list[Req]], int]:
    """Keyless subagent requests (by time) grouped by the agent of their unique step match, and how many have none."""
    index = step_index(steps)
    loops: dict[str, list[Req]] = defaultdict(list)
    lost = 0
    for req in keyless:
        agent = step_agent(index, req)
        if agent is None:
            lost += 1
        else:
            loops[agent].append(req)
    return loops, lost


@dataclasses.dataclass(frozen=True)
class Recall:
    """The step key against the `agent_id` key on requests that carry `agent_id`."""

    truth_misses: int
    truth_tokens: int
    found_misses: int  # misses the step key finds that the agent_id key also finds
    found_tokens: int
    false_misses: int  # misses the step key finds that the agent_id key does not
    false_tokens: int

    @property
    def recall_tokens(self) -> float | None:
        return self.found_tokens / self.truth_tokens if self.truth_tokens else None

    @property
    def recall_misses(self) -> float | None:
        return self.found_misses / self.truth_misses if self.truth_misses else None


def step_recall(reqs: list[Req], steps: list[Step], compactions: list[Compaction]) -> Recall:
    """Judge the requests that carry `agent_id` by `agent_id` (the truth), then again with the id removed and the
    step key; compare the misses found."""
    carrying = [r for r in prepare(reqs) if r.agent_id]
    index = _Compactions(compactions)
    truth: dict[int, Miss] = {}
    by_agent: dict[str, list[Req]] = defaultdict(list)
    for req in carrying:
        by_agent[req.agent_id].append(req)
    for agent, loop in by_agent.items():
        truth.update(_judge(loop, index.agent_excuser(agent), is_main=False, out=Row(), gaps=None))
    stripped = {id(r): dataclasses.replace(r, agent_id="") for r in carrying}
    loops, _ = step_loops([stripped[id(r)] for r in carrying], steps)
    back = {id(s): id(o) for o, s in ((o, stripped[id(o)]) for o in carrying)}
    got: dict[int, Miss] = {}
    for agent, loop in loops.items():
        got.update(_judge(loop, index.agent_excuser(agent), is_main=False, out=Row(), gaps=None))
    got = {back[k]: v for k, v in got.items()}
    hit = {k: v for k, v in got.items() if k in truth}
    false = {k: v for k, v in got.items() if k not in truth}
    return Recall(
        len(truth),
        sum(m.shortfall for m in truth.values()),
        len(hit),
        sum(m.shortfall for m in hit.values()),
        len(false),
        sum(m.shortfall for m in false.values()),
    )


# --- lines ---------------------------------------------------------------------------------------------------


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "0.0%"


def _count(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 'es' if word.endswith('s') else 's'}"


def _cause_lines(row: Row, indent: str) -> list[str]:
    """Unexplained first and always; each other label only when it holds a miss."""
    count, held = row.of(UNEXPLAINED)
    lines = [
        f"{indent}unexplained: {count:,} of {_count(len(row.misses), 'miss')}, {_share(held, row.tokens)} of tokens"
    ]
    for cause in (GAP, MODEL, EFFORT):
        count, held = row.of(cause)
        if count:
            lines.append(f"{indent}{FOLLOWED[cause]}: {_count(count, 'miss')}, {held:,} tokens")
    return lines


def headline_line(row: Row) -> str:
    dollars = f", about ${row.cost:,.2f} more than reading them" if row.misses else ""
    return (
        f"  main and agent_id loops: {_count(len(row.misses), 'miss')}, {row.tokens:,} tokens read short of the "
        f"cache{dollars}"
    )


def session_line(analysis: Analysis) -> str | None:
    """One session's headline-loop figures; None when it has no miss."""
    row = analysis.headline
    if not row.misses:
        return None
    return f"{_count(len(row.misses), 'miss')}, {row.tokens:,} tokens read short of the cache, about ${row.cost:,.2f} more than reading them"


def summary_lines(total: Analysis) -> list[str]:
    """The total block: headline, the step-matched row (a lower bound), the not-attributable row, then the notes."""
    head, step = total.headline, total.stepped
    lines = [headline_line(head), *_cause_lines(head, "    ")]
    if step.loops:
        lines.append(
            f"  subagents without agent_id, matched to a step (lower bound, recall about {STEP_RECALL_TOKENS:.0%} of "
            f"tokens, measured on requests with agent_id): {_count(step.loops, 'loop')}, "
            f"{_count(len(step.misses), 'miss')}, {step.tokens:,} tokens"
        )
        lines.extend(_cause_lines(step, "    "))
    if total.not_attributable:
        lines.append(f"  subagents without agent_id, not attributable: {_count(total.not_attributable, 'request')}")
    if total.gaps_unknown:
        lines.append(f"  main-loop gaps over 5 min with unknown lifetime (judged at 5 min): {total.gaps_unknown:,}")
    rebuilds = head.rebuilds + step.rebuilds
    if rebuilds:
        lines.append(f"  expected rebuilds, a compaction in between: {rebuilds:,}")
    unpriced = head.unpriced
    if unpriced:
        lines.append(f"  {_count(unpriced, 'miss')} on a model with no price, left out of the dollar figure")
    lines.append(
        f"  write split check: {total.split_violations:,} of {_count(total.split_checked, 'request')} with a "
        "non-zero split do not sum to cache_creation_tokens"
    )
    return lines
