#!/usr/bin/env python3
"""Usage per subagent for `agent_telemetry.py report --subagents`.

A *subagent* is the agent type a request ran as: `main` for the main loop, else
the subagent type (`Explore`, `Plan`, `general-purpose`, a custom agent's
name). It comes from the usage-observer plugin's native `agent_type` or the
OTel `agent.name`; the *source* column says where the type is defined
(`built-in`, `custom`, ... from `query_source` and `subagent_completed`).
Claude Code exposes no output style or system-prompt identity, so those are
not subagents here, and only the type name is read: never a system prompt,
message text or identity attribute.

The runs, iterations and tool calls are the ones of `--iterations`
(agent_iteration_report.py); this view regroups them by subagent, with the
model split taken per request rather than per run.
"""

from __future__ import annotations

import dataclasses
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_iteration_report as ir
import agent_tool_report as tr

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_SESSION_LIMIT = 20
DEFAULT_TOOL_LIMIT = 5
NO_SOURCE = "-"
MAIN_SOURCE = "-"


@dataclasses.dataclass
class Totals:
    runs: int = 0
    iterations: int = 0
    tokens: float = 0.0
    cost: float = 0.0
    costed: int = 0
    tool_calls: int = 0
    failures: int = 0
    flagged: int = 0
    counts: list[int] = dataclasses.field(default_factory=list)

    def add(self, run: ir.Run) -> None:
        self.runs += 1
        self.iterations += run.iterations
        self.tokens += run.tokens
        self.cost += run.cost
        self.costed += run.costed
        self.tool_calls += run.tool_calls
        self.failures += run.failures
        self.flagged += run.flagged
        self.counts.append(run.iterations)


def _int(value: float) -> str:
    return f"{round(value):,}"


def _cost(cost: float, costed: int) -> str:
    return f"${cost:.2f}" if costed else "-"


def _per_iteration(cost: float, costed: int) -> str:
    return f"${cost / costed:.4f}" if costed else "-"


def _order(totals: dict[str, Totals]) -> list[str]:
    return sorted(totals, key=lambda k: (-totals[k].iterations, k))


def _totals_by(runs: list[ir.Run], key: Callable[[ir.Run], str]) -> dict[str, Totals]:
    out: dict[str, Totals] = defaultdict(Totals)
    for run in runs:
        out[key(run)].add(run)
    return out


def _subagent_block(runs: list[ir.Run], sources: dict[str, str]) -> list[str]:
    totals = _totals_by(runs, lambda r: r.agent_type)
    rows = []
    for subagent in _order(totals):
        t = totals[subagent]
        rows.append(
            [
                subagent,
                MAIN_SOURCE if subagent == ir.MAIN else sources.get(subagent, NO_SOURCE),
                f"{t.runs:,}",
                f"{t.iterations:,}",
                f"{statistics.median(t.counts):g}",
                f"{t.tool_calls:,}",
                f"{t.failures:,}",
                _int(t.tokens),
                _cost(t.cost, t.costed),
                _per_iteration(t.cost, t.costed),
                f"{t.flagged:,}",
            ]
        )
    header = [
        "subagent",
        "source",
        "runs",
        "iters",
        "median",
        "tools",
        "failed",
        "tokens",
        "cost",
        "cost/iter",
        "flagged",
    ]
    return [
        (
            f"By subagent type (flagged: >= {ir.FLAG_MIN_ITERATIONS} iterations and >= {ir.FLAG_MEDIAN_FACTOR}x "
            "the median of its subagent type)"
        ),
        *ir.format_table(header, rows, left=2),
    ]


def _model_block(runs: list[ir.Run]) -> list[str]:
    uses: dict[tuple[str, str], ir.ModelUse] = defaultdict(ir.ModelUse)
    for run in runs:
        for model, use in run.model_use.items():
            total = uses[(run.agent_type, model)]
            total.iterations += use.iterations
            total.tokens += use.tokens
            total.cost += use.cost
            total.costed += use.costed
    keys = sorted(uses, key=lambda k: (-uses[k].iterations, k))
    rows = [
        [
            subagent,
            model,
            f"{uses[(subagent, model)].iterations:,}",
            _int(uses[(subagent, model)].tokens),
            _cost(uses[(subagent, model)].cost, uses[(subagent, model)].costed),
            _per_iteration(uses[(subagent, model)].cost, uses[(subagent, model)].costed),
        ]
        for subagent, model in keys
    ]
    return [
        "By subagent and model (per request)",
        *ir.format_table(["subagent", "model", "iters", "tokens", "cost", "cost/iter"], rows, left=2),
    ]


def _tool_block(runs: list[ir.Run], limit: int) -> list[str]:
    calls: dict[str, Counter[str]] = defaultdict(Counter)
    failures: dict[str, Counter[str]] = defaultdict(Counter)
    for run in runs:
        for call in run.calls:
            calls[run.agent_type][call.tool] += 1
            failures[run.agent_type][call.tool] += not call.success
    if not calls:
        return ["Tool usage per subagent: none"]
    rows = []
    hidden = 0
    for subagent in sorted(calls, key=lambda p: (-sum(calls[p].values()), p)):
        ordered = sorted(calls[subagent].items(), key=lambda item: (-item[1], item[0]))
        hidden += max(0, len(ordered) - limit)
        rows += [[subagent, tool, f"{n:,}", f"{failures[subagent][tool]:,}"] for tool, n in ordered[:limit]]
    lines = [
        f"Tool usage per subagent (top {limit} tools each)",
        *ir.format_table(["subagent", "tool", "calls", "failed"], rows, left=2),
    ]
    if hidden:
        lines.append(f"  ... {hidden:,} more tool rows (raise --limit)")
    return lines


def _session_block(runs: list[ir.Run], limit: int) -> list[str]:
    shown, total = ir.session_lines(runs, limit)
    keep = set(shown)
    totals: dict[tuple[str, str], Totals] = defaultdict(Totals)
    for run in runs:
        label = tr.session_label(run.session)
        if label in keep:
            totals[(label, run.agent_type)].add(run)
    keys = sorted(totals, key=lambda k: (shown.index(k[0]), -totals[k].iterations, k[1]))
    rows = [
        [
            label,
            subagent,
            f"{t.runs:,}",
            f"{t.iterations:,}",
            f"{t.tool_calls:,}",
            _int(t.tokens),
            _cost(t.cost, t.costed),
        ]
        for (label, subagent), t in ((k, totals[k]) for k in keys)
    ]
    older = f"; {total - len(shown):,} older not shown, raise --limit" if total > len(shown) else ""
    return [
        f"By session and subagent (most recent first{older})",
        *ir.format_table(["session", "subagent", "runs", "iters", "tools", "tokens", "cost"], rows, left=2),
    ]


def format_report(report: ir.IterationReport, limit: int | None = None) -> str:
    if not report.has_requests:
        return "No model requests found."
    runs = report.runs
    if not runs:
        return "No loop iterations found."
    total = Totals()
    for run in runs:
        total.add(run)
    blocks = [
        [
            (
                "Usage per subagent type (subagent type = the agent type a request ran as, `main` for the main loop; run = "
                "the main loop answering one prompt, or one subagent)"
            ),
            (
                f"  subagents {len({r.agent_type for r in runs}):,}, runs {total.runs:,}, iterations "
                f"{total.iterations:,}, tool calls {total.tool_calls:,}, tokens {_int(total.tokens)}, "
                f"cost {_cost(total.cost, total.costed)}"
            ),
        ],
        _subagent_block(runs, report.subagent_sources),
        _model_block(runs),
        _tool_block(runs, limit or DEFAULT_TOOL_LIMIT),
        _session_block(runs, limit or DEFAULT_SESSION_LIMIT),
    ]
    notes = []
    if report.uncosted:
        notes.append(f"  {report.uncosted:,} iterations without cost (left out of cost and cost/iter)")
    if report.unmatched_requests:
        notes.append(
            f"  {report.unmatched_requests:,} subagent requests without a transcript match "
            "(counted in runs with agent id ?)"
        )
    if report.unattributed_calls:
        notes.append(f"  {report.unattributed_calls:,} tool calls not attributed to a subagent")
    if notes:
        blocks.append(["Notes", *notes])
    return "\n\n".join("\n".join(block) for block in blocks)
