#!/usr/bin/env python3
"""Context fill and compactions, read once for both `report --context` and `report --sessions`.

One place decides what a measure record's fill is, which compaction records count and
whose they are, so the two reports cannot disagree:

- fill: `context_percent`, else `context_tokens` over `context_window`; a record with
  neither (or a non-finite number) has no fill. A session's peak is its highest fill.
- compactions: the plugin's native `compaction` records when an agent (the main loop or one
  subagent) has any, else that agent's `claude_code.compaction` events; never both.
  Skipped and failed attempts install nothing and are left out. Only main-loop compactions
  are the session's count; subagent ones are counted apart.

Nothing here reads transcripts or prints anything.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tool_report as tr

COMPACTION = "compaction"


TRIGGERS = ("auto", "manual")  # what Claude Code records as a compaction's trigger
OTHER_TRIGGER = "other"


def text_of(value: object, default: str = "?") -> str:
    """`value` as a label: each non-printable character (control characters, line and paragraph
    separators) becomes `?`, so no record can break a table row or send a terminal escape."""
    if value is None:
        return default
    text = tr.encodable(str(value))
    return text if text.isprintable() else "".join(c if c.isprintable() else "?" for c in text)


def trigger_of(value: object) -> str:
    """A compaction's trigger for printing: `auto` or `manual`, `?` when absent, else `other`.
    The text is a label for a table cell, never a place for a stranger's text or secrets."""
    if value is None:
        return "?"
    return value if isinstance(value, str) and value in TRIGGERS else OTHER_TRIGGER


@dataclasses.dataclass(frozen=True)
class Sample:
    session: str
    timestamp: str
    tokens: float | None
    window: float | None
    percent: float


@dataclasses.dataclass(frozen=True)
class Compaction:
    session: str
    timestamp: str
    trigger: str
    before: float | None
    after: float | None
    native: bool
    agent: str | None = None  # None = the main loop


def sample_of(record: dict[str, Any]) -> Sample | None:
    """A native `measure` record's fill, or None when it has none."""
    tokens = tr.as_number(record.get("context_tokens"))
    window = tr.as_number(record.get("context_window"))
    percent = tr.as_number(record.get("context_percent"))
    if percent is None and tokens is not None and window:
        percent = tokens / window * 100
    if percent is None:
        return None
    return Sample(text_of(record.get("session")), text_of(record.get("ts"), ""), tokens, window, percent)


def _agent_of(value: object) -> str | None:
    """The compacting agent; absent, null or empty is the main loop, as for OTel events."""
    return text_of(value) if value else None


def native_compaction_of(record: dict[str, Any]) -> Compaction:
    return Compaction(
        text_of(record.get("session")),
        text_of(record.get("ts"), ""),
        trigger_of(record.get("trigger")),
        tr.as_number(record.get("tokens_before")),
        tr.as_number(record.get("tokens_after")),
        True,
        _agent_of(record.get("agent")),
    )


def event_compaction_of(attrs: dict[str, Any]) -> Compaction:
    return Compaction(
        text_of(attrs.get("session.id")),
        text_of(attrs.get("event.timestamp"), ""),
        trigger_of(attrs.get("trigger")),
        tr.as_number(attrs.get("pre_tokens")),
        tr.as_number(attrs.get("post_tokens")),
        False,
        text_of(attrs.get("agent.name")) if attrs.get("agent.name") else None,
    )


def select_compactions(native: list[Compaction], events: list[Compaction]) -> list[Compaction]:
    """Native records per agent when that agent has any, else that agent's events.

    Native names a subagent by id and OTel by type, so the two cannot be matched:
    a session with any native subagent record takes its subagent compactions from
    those alone."""
    native_agents = {(c.session, c.agent) for c in native}
    native_subagent_sessions = {c.session for c in native if c.agent is not None}
    return [
        *native,
        *(
            c
            for c in events
            if (c.session, c.agent) not in native_agents
            and not (c.agent is not None and c.session in native_subagent_sessions)
        ),
    ]


def peak_of(samples: list[Sample]) -> Sample | None:
    return max(samples, key=lambda s: s.percent, default=None)


def count_compactions(compactions: list[Compaction]) -> tuple[dict[str, int], dict[str, int]]:
    """Per session: main-loop compactions, and subagent compactions."""
    main: dict[str, int] = {}
    sub: dict[str, int] = {}
    for c in compactions:
        counts = main if c.agent is None else sub
        counts[c.session] = counts.get(c.session, 0) + 1
    return main, sub
