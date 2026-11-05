"""Per-session decision hints for `report --sessions`.

A hint names a cost (a token count, a share, a dollar figure) and the axis it sits on (the
session, side requests, subagents). It never claims an outcome. Every bar below is a hypothesis:
it comes from the probe distribution over 126 sessions cited beside it, and is meant to be moved
when a larger capture says otherwise.

Pure functions over plain numbers; standard library only. The cache facts (read short of the
cache, gaps past the lifetime) follow Claude Code's own miss rule: within one loop (the main
thread, or one `agent_id`), a request read short of the previous request's read plus write by
more than 5% and at least 2,000 tokens, with no compaction in between.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections import defaultdict

# --- bars (D5). Each: value, probe distribution (n=126), sessions it fired on. -------------------

SHORTFALL_SHARE_MIN = 0.10  # H1: the floor (a hint covers at least 10% of usage); p50 0.000, p75 0.135, p90 1.329, p95 2.691, max 4.539; fires on 32 of 126
READ_SHARE_BELOW = (
    0.85  # H3: session-report's bar; p50 0.951, p75 0.964, p90 0.972, p95 0.977; fires on 7 of 126 (below 0.70: 4)
)
SIDE_SHARE_MIN = 0.172  # H4: p90; p50 0.006, p75 0.125, p90 0.172, p95 0.193, max 0.252; about 13 of 126
SUBAGENT_SHARE_MIN = 0.809  # H5: p90; p50 0.000, p75 0.342, p90 0.809, p95 0.858, max 0.976; about 13 of 126
GAPS_PAST_TTL_MIN = 1  # H2: one event; 54 main-loop gaps in all, 10 with a known lifetime; session count unverified
# H6 (compactions) is dropped: p50 to p95 all 0, max 3, p90 under the one-event floor.

MISS_SHARE = 0.05  # Claude Code's rule: shortfall over 5% of the expected read ...
MISS_TOKENS = 2000  # ... and at least 2,000 tokens
TTL_5M = 300.0
TTL_1H = 3600.0
ROUND = 9  # shares are compared after rounding, so 17.2 / 100 and 0.172 meet exactly at the bar

MAIN_LOOP = ""  # loop key of the main thread; an agent loop is keyed by its agent_id

HINTS = ("shortfall", "gaps", "read", "side", "subagent")  # print order; the keys of `fired`


@dataclasses.dataclass(frozen=True)
class CacheRow:
    """The cache-relevant part of one loop request."""

    t: float  # epoch seconds
    loop: str  # MAIN_LOOP, or an agent_id (never printed)
    read: float
    created: float
    created_5m: float | None
    created_1h: float | None


@dataclasses.dataclass
class CacheFacts:
    shortfall_tokens: float = 0.0  # summed over misses
    misses: int = 0
    gaps_past_ttl: int = 0  # main-loop gaps past a known lifetime
    gaps_unknown: int = 0  # main-loop gaps over 5 min where no write of the loop carried a split


def lifetime(created_5m: float | None, created_1h: float | None) -> float | None:
    """Seconds a write stays cached, from the split: 1 h when only the 1 h part is non-zero, else 5 min
    (a mixed write expires with its 5 min part). None when there is no non-zero split."""
    if created_5m is None or created_1h is None or not (created_5m or created_1h):
        return None
    return TTL_1H if created_1h and not created_5m else TTL_5M


def cache_facts(rows: list[CacheRow], main_compactions: list[float], agent_compactions: list[float]) -> CacheFacts:
    """Misses and gaps over the loops in `rows`. A compaction in (prev, now] of the loop makes a short
    read an expected rebuild, not a miss; a subagent compaction cannot be matched to one agent here, so
    it excuses every agent loop of the session (fewer misses, never more)."""
    facts = CacheFacts()
    loops: dict[str, list[CacheRow]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: r.t):
        loops[row.loop].append(row)
    for key, loop in loops.items():
        compactions = main_compactions if key == MAIN_LOOP else agent_compactions
        split: float | None = None
        for prev, now in itertools.pairwise(loop):
            split = lifetime(prev.created_5m, prev.created_1h) or split
            if key == MAIN_LOOP and (gap := now.t - prev.t) > TTL_5M:
                if split is None:
                    facts.gaps_unknown += 1
                elif gap > split:
                    facts.gaps_past_ttl += 1
            expected = prev.read + prev.created
            shortfall = expected - now.read
            if shortfall <= MISS_SHARE * expected or shortfall < MISS_TOKENS:
                continue
            if any(prev.t < c <= now.t for c in compactions):
                continue
            facts.misses += 1
            facts.shortfall_tokens += shortfall
    return facts


@dataclasses.dataclass(frozen=True)
class HintInput:
    """What one session offers the hints. A share is None when its denominator is 0."""

    shortfall_tokens: float = 0.0
    misses: int = 0
    cache_writes: float = 0.0
    gaps_past_ttl: int = 0
    read_share: float | None = None  # cache read over all input tokens, 0 to 1
    side_cost: float = 0.0
    subagent_cost: float = 0.0
    cost: float = 0.0


def _ratio(part: float, whole: float) -> float | None:
    return round(part / whole, ROUND) if whole > 0 else None


def shortfall_share(s: HintInput) -> float | None:
    return _ratio(s.shortfall_tokens, s.cache_writes)


def side_share(s: HintInput) -> float | None:
    return _ratio(s.side_cost, s.cost)


def subagent_share(s: HintInput) -> float | None:
    return _ratio(s.subagent_cost, s.cost)


def _count(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def hint_shortfall(s: HintInput) -> str | None:
    share = shortfall_share(s)
    if share is None or s.misses < 1 or share < SHORTFALL_SHARE_MIN:
        return None
    return (
        f"read short of the cache by {s.shortfall_tokens:,.0f} tokens in {_count(s.misses, 'request')}, "
        f"{share:.2f} of the session's cache writes"
    )


def hint_gaps(s: HintInput) -> str | None:
    if s.gaps_past_ttl < GAPS_PAST_TTL_MIN:
        return None
    return f"{_count(s.gaps_past_ttl, 'main-loop gap')} longer than the cache lifetime of the last write"


def hint_read(s: HintInput) -> str | None:
    if s.read_share is None or round(s.read_share, ROUND) >= READ_SHARE_BELOW:
        return None
    # Cut, not rounded, to two decimals: a share just under the bar must not print as the bar ("85.0% ... below 85%").
    # The share is cut at the precision of the bar check above (ROUND places), as a whole number of billionths.
    percent = (round(round(s.read_share, ROUND) * 10**ROUND) // 10 ** (ROUND - 4)) / 100
    return f"cache reads were {percent:.2f}% of input tokens, below {READ_SHARE_BELOW:.0%}"


def hint_side(s: HintInput) -> str | None:
    share = side_share(s)
    if share is None or share < SIDE_SHARE_MIN:
        return None
    return f"side requests cost ${s.side_cost:,.2f} of ${s.cost:,.2f}, {share:.1%} of the session's cost"


def hint_subagent(s: HintInput) -> str | None:
    share = subagent_share(s)
    if share is None or share < SUBAGENT_SHARE_MIN:
        return None
    return f"subagents cost ${s.subagent_cost:,.2f} of ${s.cost:,.2f}, {share:.1%} of the session's cost"


_HINT_FUNCTIONS = {
    "shortfall": hint_shortfall,
    "gaps": hint_gaps,
    "read": hint_read,
    "side": hint_side,
    "subagent": hint_subagent,
}


def hint_lines(s: HintInput) -> dict[str, str]:
    """The hints that fire on one session, by key, in `HINTS` order."""
    out = {}
    for key in HINTS:
        if (line := _HINT_FUNCTIONS[key](s)) is not None:
            out[key] = line
    return out
