#!/usr/bin/env python3
"""Token table rows from model requests, for captures with no token metric.

The usage-observer plugin's capture holds no `claude_code.token.usage` metric
(the telemetry mirror carries log events only), so the default `report` table
has nothing to sum there. This module builds the same rows -- (agent, model)
by token type -- from what the capture does hold:

- `claude_code.api_request` events: one per model request, loop and side
  requests alike (session titles, hook prompts, agent summaries, compaction),
  which is what the OTLP metric counts. The agent is `agent.name`, or the main
  session when the request has none.
- native `step` records, for prompts with no loop `api_request` event (the
  mirror was off); they carry no side requests. The agent is the step's
  `agent_type`, or the main session for the main loop. A prompt that has both
  is counted from its events only, never twice.

Nothing is read from transcripts, and no prompt text, tool text or identity
attribute is read or printed.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tool_report as tr
from agent_iteration_report import NATIVE_TOKEN_KEYS, loop_type
from safe_name import agent_label

if TYPE_CHECKING:
    from agent_telemetry import ProjectFilter

MAIN_SESSION = "<main session>"
# The token table's `type` values, in the order of tr.API_TOKEN_KEYS and NATIVE_TOKEN_KEYS.
TOKEN_TYPES = ("input", "output", "cacheRead", "cacheCreation")

Totals = dict[tuple[str, str], dict[str, int]]


def _add(totals: Totals, agent: str, model: object, tokens: list[float | None]) -> None:
    row = totals[(agent, "?" if model is None else str(model))]
    for kind, value in zip(TOKEN_TYPES, tokens, strict=True):
        row[kind] += int(value or 0)


def aggregate_requests(capture: Path, project_filter: ProjectFilter | None = None) -> Totals:
    """Sum token counts per (agent, model), keyed by token type, from the
    capture's `api_request` events and native `step` records. A (session, prompt)
    with a loop `api_request` event is not also counted from its steps."""
    totals: Totals = defaultdict(lambda: defaultdict(int))
    steps: list[dict[str, Any]] = []
    event_prompts: set[tuple[str, str]] = set()
    for record in tr.capture_records(capture):
        if record.get("kind") == "step":
            if project_filter is None or project_filter.keep({"session.id": record.get("session")}):
                steps.append(record)
            continue
        for name, attrs in tr.iter_log_events(record.get("payload")):
            if name != tr.API_REQUEST:
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            if loop_type(str(attrs.get("query_source") or ""), attrs.get("agent.name")) is not None:
                event_prompts.add((str(attrs.get("session.id")), str(attrs.get("prompt.id"))))
            _add(
                totals,
                agent_label(attrs.get("agent.name"), MAIN_SESSION),
                attrs.get("model"),
                [tr.as_number(attrs.get(key)) for key in tr.API_TOKEN_KEYS],
            )
    for step in steps:
        usage = step.get("usage")
        if not isinstance(usage, dict) or (str(step.get("session")), str(step.get("prompt"))) in event_prompts:
            continue
        agent = MAIN_SESSION if step.get("agent") is None else agent_label(step.get("agent_type"), "?")
        _add(totals, agent, step.get("model"), [tr.as_number(usage.get(key)) for key in NATIVE_TOKEN_KEYS])
    return totals
