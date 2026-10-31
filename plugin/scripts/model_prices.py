"""The one Claude API price table the reports share.

Prices are USD per MTok (input, cache read) by model id prefix. Source: the claude-api
skill's model table (cached 2026-09-25), corrected and extended against the official
pricing page https://platform.claude.com/docs/en/about-claude/pricing.md, retrieved
2026-10-15 (the operator chose
the page's figures, 2026-10-15). Cache reads are $0.25 on Fable 5.1 and Mythos 5.1,
$1.00 on Fable 5 and Mythos 5, $0.20 on Opus 5.5 and Sonnet 5, $0.10 on Sonnet 5.5
(0.05x input; it was $0.20 here before), and 0.1x input elsewhere
(Mythos is priced as Fable, Opus 4.6 as Opus 4.7). An id
matches a row only when it is the row's id, optionally with a `-YYYYMMDD` snapshot
date and a `[1m]` suffix, so `claude-opus-5-1` is not priced as Opus 5 and
`claude-opus-4-60` is not priced as Opus 4.6. A model that table
does not list is left out rather than guessed, so it stays unpriced, and the usage
report lists it apart instead of weighting it as another model.
The older rows (Opus 4.5, 4.1 and 4; Sonnet 4.5 and 4; Haiku 3.5) are the page's figures too; the retired ones (Opus 4.1 and 4, Sonnet 4, Haiku 3.5) stay priced because old
transcripts still carry them. A test checks every row against a fixture of the page's figures.

Prompt-length rate cards: a row may carry a `threshold` and a second card
(`long_input`, `long_cache_read`). Claude Haiku 5.5 is the first: up to 100,000 tokens it is
$0.10 in / $0.01 read, over 100,000 $0.50 / $0.05 (cache writes 1.25x / 2x and output 5x of
the card in force, so the shared ratios hold). The page says "up to / over 100,000 tokens"
and "all input tokens of the request, including cache reads and cache writes": a message's
prompt length is `prompt_tokens(usage)` = input + cache read + cache write (5-minute and
1-hour), the boundary is strict (exactly 100,000 uses the first card, 100,001 the second),
and every message is priced on its own. A row without a threshold has one card.
`model_prices(model, prompt_tokens)`, `input_weight` and `cache_read_equivalent` take the
message's prompt length; every report that prices goes through them. The weighting baseline
is always Sonnet 5.5's one card.

This is the first-party Claude API price list. Bedrock-style ids (`anthropic.*`, the
`us.` / `eu.` / `global.` prefixes, a `-v1:0` suffix) and Vertex `@date` ids
(`claude-opus-4-6@20260205`) are deliberately not stripped or looked up:
the table does not know what those platforms charge, so they stay unpriced and are
listed apart rather than guessed. A bare id such as `claude-opus-4-6` is looked up
whichever platform served it, since the id alone cannot tell. Whether Claude Code
transcripts record such ids at all is unverified.

Price ratios live here too, once each: a cache write costs
`CACHE_WRITE_5M` x input at the 5-minute TTL and `CACHE_WRITE_1H` x input at the
1-hour TTL, output costs `OUTPUT_PRICE_FACTOR` x input, and a cache read is the
row's own price (`cache_read_equivalent` expresses it against the baseline input).

`agent_skill_report` (load overhead) and `agent_reasoning_report` read the
prices; `agent_usage_report` derives its model weights from the prices.

Fast mode: a message whose `usage.speed` is exactly `"fast"` on a model in
`FAST_MODE_MODELS` costs `FAST_MODE_FACTOR` x the standard price for every token kind
(claude-api skill, model-migration and platform-availability references: Opus 5.5 $8 / $40,
Opus 5 and Opus 4.8 $10 / $50 per MTok, 2x each; research preview on the first-party API
only). Whether the multiplier also covers cache reads and writes is not stated there; it
is applied to all kinds, so a fast message is priced at 2x, never less. A `fast` speed on
a model outside the set is priced as standard. Web search is $10 per 1,000 requests
(`WEB_SEARCH_USD` each, claude-api skill, managed-agents-core), model- and speed-independent;
web fetch has no charge of its own.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tool_report as tr


# Adding or removing a row here means updating the priced-models list in the
# documentation (a test fails when it drifts) and, for a new family,
# `_MODEL_FAMILIES` in agent_usage_report.py.
class PriceRow(NamedTuple):
    """One model: input and cache-read USD per MTok; an optional prompt-length threshold
    with the second card (`long_*`) that applies to a message whose prompt is over it."""

    model: str
    input: float
    cache_read: float
    threshold: int | None = None
    long_input: float | None = None
    long_cache_read: float | None = None


MODEL_PRICES: Final[tuple[PriceRow, ...]] = (
    PriceRow("claude-fable-5-1", 10.00, 0.25),
    PriceRow("claude-fable-5", 10.00, 1.00),
    PriceRow("claude-mythos-5-1", 10.00, 0.25),
    PriceRow("claude-mythos-5", 10.00, 1.00),
    PriceRow("claude-opus-5-5", 4.00, 0.20),
    PriceRow("claude-opus-5", 5.00, 0.50),
    PriceRow("claude-opus-4-8", 5.00, 0.50),
    PriceRow("claude-opus-4-7", 5.00, 0.50),
    PriceRow("claude-opus-4-6", 5.00, 0.50),
    PriceRow("claude-opus-4-5", 5.00, 0.50),
    PriceRow("claude-opus-4-1", 15.00, 1.50),
    PriceRow("claude-opus-4", 15.00, 1.50),
    PriceRow("claude-sonnet-5-5", 2.00, 0.10),
    PriceRow("claude-sonnet-5", 2.00, 0.20),
    PriceRow("claude-sonnet-4-6", 3.00, 0.30),
    PriceRow("claude-sonnet-4-5", 3.00, 0.30),
    PriceRow("claude-sonnet-4", 3.00, 0.30),
    PriceRow("claude-haiku-5-5", 0.10, 0.01, threshold=100_000, long_input=0.50, long_cache_read=0.05),
    PriceRow("claude-haiku-4-5", 1.00, 0.10),
    PriceRow("claude-haiku-3-5", 0.80, 0.08),
)

# Models with fast mode (`speed: "fast"`), by table id; matched like any other id.
FAST_MODE_MODELS: Final[frozenset[str]] = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"})
FAST_MODE_FACTOR: Final[float] = 2.0  # fast-mode price, x the standard price of every token kind
WEB_SEARCH_USD: Final[float] = 0.01  # one web search request ($10 per 1,000)
# Most web search requests one message may carry; more is a corrupt record. A request
# count has a far smaller plausible range than the 1e15 token cap, which would price 1e15 requests
# at 5e18 sonnet-input-equivalents.
MAX_WEB_SEARCH_REQUESTS: Final[float] = 10_000.0

# The weighting baseline: a message's weight is its model's input price over this one's.
BASELINE_MODEL: Final[str] = "claude-sonnet-5-5"

CACHE_WRITE_5M: Final[float] = 1.25  # cache write at the 5-minute TTL, x input price
CACHE_WRITE_1H: Final[float] = 2.0  # cache write at the 1-hour TTL, x input price
OUTPUT_PRICE_FACTOR: Final[float] = 5.0  # output price per MTok = 5 x input price

# What may follow a table id: nothing, a context-window suffix (`[1m]`), or a dated
# snapshot (`-20251101`, optionally with the suffix). Anything else is another model.
_ID_TAIL: Final[re.Pattern[str]] = re.compile(r"(-\d{8})?(\[.*\])?")


def _is_model(model: str, prefix: str) -> bool:
    return model.startswith(prefix) and _ID_TAIL.fullmatch(model, len(prefix)) is not None


def _price_row(model: str | None) -> PriceRow | None:
    if model is None:
        return None
    best: PriceRow | None = None
    for row in MODEL_PRICES:
        if _is_model(model, row.model) and (best is None or len(row.model) > len(best.model)):
            best = row
    return best


def model_prices(model: str | None, prompt_tokens: float = 0.0) -> tuple[float, float] | None:
    """(input, cache read) USD per MTok for `model` (longest matching prefix, so
    `claude-opus-5[1m]` is Opus 5 and `claude-opus-5-5` is not), or None if unknown.
    For a row with a prompt-length threshold, a prompt of more than `threshold` tokens
    (see `prompt_tokens`) gets the second card; at or under it, or with no length given,
    the first."""
    row = _price_row(model)
    if row is None:
        return None
    if (
        row.threshold is not None
        and row.long_input is not None
        and row.long_cache_read is not None
        and prompt_tokens > row.threshold
    ):
        return (row.long_input, row.long_cache_read)
    return (row.input, row.cache_read)


def supports_fast_mode(model: str | None) -> bool:
    """Whether `model` is priced for fast mode (an id in `FAST_MODE_MODELS`)."""
    row = _price_row(model)
    return row is not None and row.model in FAST_MODE_MODELS


def speed_factor(usage: Mapping[str, object], model: str | None) -> float:
    """`FAST_MODE_FACTOR` for a fast-mode message on a model that has it, else 1.0."""
    if usage.get("speed") == "fast" and supports_fast_mode(model):
        return FAST_MODE_FACTOR
    return 1.0


def web_search_equivalent(usage: Mapping[str, object]) -> float:
    """One message's web search requests (`usage.server_tool_use.web_search_requests`)
    in input-token equivalents of the baseline Sonnet's input price; 0.0 when the block
    is absent or malformed, or the baseline is unpriced."""
    block = usage.get("server_tool_use")
    baseline = model_prices(BASELINE_MODEL)
    if not isinstance(block, Mapping) or baseline is None:
        return 0.0
    requests = token_count(
        block.get("web_search_requests"), MAX_WEB_SEARCH_REQUESTS, owner=block, field="web_search_requests"
    )
    return requests * WEB_SEARCH_USD * 1_000_000 / baseline[0]


def input_weight(model: str | None, prompt_tokens: float = 0.0) -> float | None:
    """`model`'s input price (the card for `prompt_tokens`) relative to the baseline
    Sonnet's, or None if unpriced."""
    prices = model_prices(model, prompt_tokens)
    baseline = model_prices(BASELINE_MODEL)
    if prices is None or baseline is None:
        return None
    return prices[0] / baseline[0]


def cache_read_equivalent(model: str | None, prompt_tokens: float = 0.0) -> float | None:
    """`model`'s cache-read price per token (the card for `prompt_tokens`) over the
    baseline Sonnet's input price (0.05 on Sonnet 5.5), or None if unpriced."""
    prices = model_prices(model, prompt_tokens)
    baseline = model_prices(BASELINE_MODEL)
    if prices is None or baseline is None:
        return None
    return prices[1] / baseline[0]


def token_count(value: object, cap: float = tr.MAX_NUMBER, *, owner: object | None = None, field: str = "") -> float:
    """A token count read from a usage block: the number itself, or 0.0 for anything
    that is not a finite, non-negative int or float no larger than `MAX_NUMBER` (a bool,
    string, nan, infinity, negative or implausibly large count, or integer too large for
    a float), so one malformed field cannot crash a report or poison its totals. A number
    that parsed but was refused is counted by `agent_tool_report.as_number`, which the
    reports print as the same footer the capture reports use; a missing or
    non-numeric field is not counted. `cap` is a lower bound for a count with a smaller plausible
    range (`MAX_WEB_SEARCH_REQUESTS`); `owner` and `field` make a refusal count once however
    often the same field is read (`usage_tokens` is the usual way to pass them)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return tr.as_number(value, cap, owner=owner, field=field) or 0.0


def usage_tokens(block: Mapping[str, object], key: str) -> float:
    """`token_count` of `block[key]`, counting a refusal once per (block, key): the one way the
    reports read a usage field, since one message's fields are read more than once."""
    return token_count(block.get(key), owner=block, field=key)


def model_label(model: str) -> str:
    """A model id for a text report: each non-printable character (a newline, an escape) becomes
    `?`, so an id from a transcript cannot add a line or send a terminal escape (the
    rule of `agent_context_fill.text_of`)."""
    return model if model.isprintable() else "".join(c if c.isprintable() else "?" for c in model)


def cache_write_equivalent(usage: Mapping[str, object]) -> tuple[float, float]:
    """(cache-write tokens in input-token equivalents, cache-write tokens assumed to
    be 5-minute writes) for one message's `usage`. Transcripts carry the TTL split in
    `usage.cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`, so
    a split that covers `cache_creation_input_tokens` is priced exactly (1.25x / 2x).
    Writes the split does not cover, or every write when there is no split (the plugin
    and OTLP captures have none), are priced at the 5-minute rate and counted as
    assumed: the 1-hour share is then a floor, not a measurement."""
    total = usage_tokens(usage, "cache_creation_input_tokens")
    split = usage.get("cache_creation")
    five = hour = 0.0
    if isinstance(split, Mapping):
        five = usage_tokens(split, "ephemeral_5m_input_tokens")
        hour = usage_tokens(split, "ephemeral_1h_input_tokens")
    assumed = max(total - five - hour, 0.0)
    return (five + assumed) * CACHE_WRITE_5M + hour * CACHE_WRITE_1H, assumed


def prompt_tokens(usage: Mapping[str, object]) -> float:
    """One message's prompt length for the rate-card threshold: every input token of the
    request, input + cache read + cache write (5-minute and 1-hour; the larger of the total
    and the TTL split, so a split that exceeds the total is not under-counted). Each field
    goes through `usage_tokens` (a refusal counts once), so a malformed or negative count adds 0 and cannot pull a
    long prompt under the threshold."""
    split = usage.get("cache_creation")
    split_total = 0.0
    if isinstance(split, Mapping):
        split_total = usage_tokens(split, "ephemeral_5m_input_tokens") + usage_tokens(
            split, "ephemeral_1h_input_tokens"
        )
    return (
        usage_tokens(usage, "input_tokens")
        + usage_tokens(usage, "cache_read_input_tokens")
        + max(usage_tokens(usage, "cache_creation_input_tokens"), split_total)
    )


def unpriced_footer(requests: Mapping[str, int]) -> str | None:
    """The one footer line every pricing report prints for the models it saw without a
    price row (model id -> request count), or None when there are none."""
    if not requests:
        return None
    named = ", ".join(
        f"{model_label(model)} ({n:,} request{'' if n == 1 else 's'})" for model, n in sorted(requests.items())
    )
    return f"WARNING: models without a price row: {named}"
