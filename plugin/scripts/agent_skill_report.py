#!/usr/bin/env python3
"""Skill activation view for `agent_telemetry.py report --skills`.

Reads `claude_code.skill_activated` log events (one per activation, with
`skill.name`, `skill.source` and `invocation_trigger`) and `api_request`
events from the capture, plus the usage-observer plugin's native `skill` and
`tool` records when `--source` is the plugin capture, and prints:

- activations per skill and source, with sessions, prompts and triggers;
- activations by source and trigger;
- each prompt's API tokens and cost split across the skills it activated
  (work done while the skill was active, not what the skill costs);
- the sessions with activations, most recent first;
- each skill's load overhead from the plugin's native records: its loaded
  size, one cache write and a cache read for each later model request of the
  agent that loaded it, until that agent's run ends or compacts.

Claude Code redacts user and project skills to `custom_skill`. Their names are
recovered at report time from local sources only, most exact first:

1. the plugin `tool` record of the activation's `Skill` call, matched by
   `tool_use_id` (the `tool_result` event right after the activation, by
   `event.sequence`, carries it; `skill_activated` itself has none);
2. a plugin `skill` record of the same session and prompt that no other
   activation of that prompt accounts for;
3. the `Skill` `tool_use` input in the project's transcripts, by `tool_use_id`;
4. for a `user-slash` activation, the prompt's `<command-name>` in the
   transcripts.

Native plugin records are a name source; one counts as an activation only
when no `skill_activated` event accounts for it, so nothing is counted twice.
Identity attributes are never read, and sessions are shown only as an
8-character prefix.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tool_report as tr
import transcript_iterator as ti
from model_prices import CACHE_WRITE_1H, CACHE_WRITE_5M, model_prices, prompt_tokens, unpriced_footer
from safe_name import clean_name, clean_name_or

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from agent_telemetry import ProjectFilter

SKILL_ACTIVATED = "skill_activated"
SKILL_TOOL = "Skill"
REDACTED = "custom_skill"
USER_SLASH = "user-slash"
NO_SKILL = "(no skill activation)"
EMPTY_MESSAGE = "No claude_code.skill_activated events found."

# Trigger columns Claude Code is known to emit; other observed values follow, sorted.
KNOWN_TRIGGERS = (USER_SLASH, "claude-proactive", "nested-skill")

# Name sources, in the order they are tried and listed in the footer.
FROM_PLUGIN_TOOL = "plugin tool record"
FROM_PLUGIN_SKILL = "plugin skill record"
FROM_TRANSCRIPT_TOOL = "transcript Skill call"
FROM_TRANSCRIPT_SLASH = "transcript slash command"
NAME_SOURCES = (FROM_PLUGIN_TOOL, FROM_PLUGIN_SKILL, FROM_TRANSCRIPT_TOOL, FROM_TRANSCRIPT_SLASH)

NAME_WIDTH = 40

# Prices per MTok (input, cache read) live in model_prices.py, shared with the
# other reports, with the cache-write ratios. The capture does not say which TTL a
# write used, so a load is priced at the 5-minute rate (CACHE_WRITE_5M).
CHARS_PER_TOKEN = 4
OVERHEAD_NEEDS_PLUGIN = (
    "Skill load overhead needs the usage-observer plugin capture (--source logs/agent-telemetry.mod.jsonl):"
    " this capture has no plugin skill records."
)
COMMAND_NAME = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>")


@dataclasses.dataclass
class Activation:
    """One skill activation: a `skill_activated` event, or a plugin `skill`
    record no event accounts for (`plugin_only`)."""

    name: str
    source: str
    trigger: str
    session: str | None
    prompt: str | None
    sequence: float | None = None
    timestamp: str | None = None
    tool_use_id: str | None = None
    recovered_from: str | None = None
    plugin_only: bool = False

    @property
    def unnamed(self) -> bool:
        return self.name == REDACTED and self.recovered_from is None


@dataclasses.dataclass(frozen=True)
class SkillCall:
    """One `Skill` `tool_result` event, the pairing partner of an activation."""

    tool_use_id: str
    session: str | None
    prompt: str | None
    sequence: float | None
    timestamp: str | None


@dataclasses.dataclass(frozen=True)
class NativeSkill:
    """One usage-observer `skill` record."""

    name: str
    session: str | None
    prompt: str | None
    timestamp: str | None
    chars: int = 0
    # The project filter's verdict, taken once when the record is read so the
    # filter's kept/dropped counts see each record exactly once.
    kept: bool = True


@dataclasses.dataclass(frozen=True)
class NativeCall:
    """One usage-observer `tool` record of a Skill call: it names the agent
    that loaded the skill, which the `skill` record does not."""

    name: str
    session: str | None
    prompt: str | None
    timestamp: str | None
    agent: str | None


@dataclasses.dataclass(frozen=True)
class NativeStep:
    """One usage-observer `step` record (one model request)."""

    session: str | None
    agent: str | None
    timestamp: str
    model: str | None
    prompt_tokens: float = 0.0  # input + cache read + cache write: picks the rate card


@dataclasses.dataclass
class SkillEvents:
    activations: list[Activation] = dataclasses.field(default_factory=list)
    calls: list[SkillCall] = dataclasses.field(default_factory=list)
    native_skills: list[NativeSkill] = dataclasses.field(default_factory=list)
    # tool_use_id -> skill name, from usage-observer `tool` records of Skill calls.
    native_tools: dict[str, str] = dataclasses.field(default_factory=dict)
    native_calls: list[NativeCall] = dataclasses.field(default_factory=list)
    steps: list[NativeStep] = dataclasses.field(default_factory=list)
    # (session, agent) -> compaction timestamps
    compactions: dict[tuple[str | None, str | None], list[str]] = dataclasses.field(
        default_factory=lambda: defaultdict(list)
    )
    # prompt.id -> [tokens, cost_usd], as in the tool view.
    prompt_usage: dict[str, list[float]] = dataclasses.field(default_factory=lambda: defaultdict(lambda: [0.0, 0.0]))


def _str(value: object) -> str | None:
    return None if value is None else str(value)


def _skill_name(value: object) -> str | None:
    """A recovered name, cleaned and capped, without the leading `/` of a slash command."""
    name = clean_name(value)
    return None if name is None else (name.lstrip("/").strip() or None)


def _native(record: Mapping[str, Any], events: SkillEvents, project_filter: ProjectFilter | None = None) -> None:
    kind = record.get("kind")
    session, prompt, ts = _str(record.get("session")), _str(record.get("prompt")), _str(record.get("ts"))
    agent = _str(record.get("agent"))
    if kind == "skill":
        name = _skill_name(record.get("skill"))
        if name is not None:
            measured = tr.as_number(record.get("prompt_chars"))
            chars = int(measured) if measured else 0
            # Native records have no project label: kept when their session belongs to the project.
            kept = project_filter is None or project_filter.keep({"session.id": session})
            events.native_skills.append(NativeSkill(name, session, prompt, ts, chars, kept))
    elif kind == "tool" and record.get("tool") == SKILL_TOOL:
        name, use_id = _skill_name(record.get("skill")), record.get("tool_use_id")
        if name is not None and isinstance(use_id, str):
            events.native_tools[use_id] = name
        if name is not None:
            events.native_calls.append(NativeCall(name, session, prompt, ts, agent))
    elif kind == "step" and ts is not None:
        usage = record.get("usage")
        length = prompt_tokens(usage) if isinstance(usage, dict) else 0.0
        events.steps.append(NativeStep(session, agent, ts, _str(record.get("model")), length))
    elif kind == "compaction" and ts is not None:
        events.compactions[(session, agent)].append(ts)


def load_events(capture: Path, project_filter: ProjectFilter | None = None) -> SkillEvents:
    """Every skill_activated and api_request event that passes
    `project_filter`, every Skill tool_result event (used only for pairing,
    always within an activation's own session and prompt) and every native
    plugin `skill` record (its keep/drop verdict is taken here, once) and `tool` record."""
    events = SkillEvents()
    for record in tr.capture_records(capture):
        if "payload" not in record:
            if record.get("source") == "mod":
                _native(record, events, project_filter)
            continue
        for name, attrs in tr.iter_log_events(record.get("payload")):
            if name == tr.TOOL_RESULT:
                use_id = attrs.get("tool_use_id")
                if attrs.get("tool_name") == SKILL_TOOL and use_id is not None:
                    events.calls.append(
                        SkillCall(
                            tool_use_id=str(use_id),
                            session=_str(attrs.get("session.id")),
                            prompt=_str(attrs.get("prompt.id")),
                            sequence=tr.as_number(attrs.get("event.sequence")),
                            timestamp=_str(attrs.get("event.timestamp")),
                        )
                    )
                continue
            if name not in (SKILL_ACTIVATED, tr.API_REQUEST):
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            if name == SKILL_ACTIVATED:
                events.activations.append(
                    Activation(
                        name=clean_name_or(attrs.get("skill.name")),
                        source=clean_name_or(attrs.get("skill.source")),
                        trigger=clean_name_or(attrs.get("invocation_trigger")),
                        session=_str(attrs.get("session.id")),
                        prompt=_str(attrs.get("prompt.id")),
                        sequence=tr.as_number(attrs.get("event.sequence")),
                        timestamp=_str(attrs.get("event.timestamp")),
                    )
                )
            else:
                prompt = attrs.get("prompt.id")
                usage = events.prompt_usage[str(prompt) if prompt is not None else "?"]
                usage[0] += sum(tr.as_number(attrs.get(key)) or 0.0 for key in tr.API_TOKEN_KEYS)
                usage[1] += tr.as_cost(attrs.get("cost_usd")) or 0.0
    return events


# --- name recovery ---------------------------------------------------------------


def _order(item: Activation | SkillCall) -> tuple[float, str]:
    return (item.sequence if item.sequence is not None else float("inf"), item.timestamp or "")


def _follows(call: SkillCall, activation: Activation) -> bool:
    if call.sequence is not None and activation.sequence is not None:
        return call.sequence > activation.sequence
    return (call.timestamp or "") >= (activation.timestamp or "")


def _by_prompt(items: Iterable[Any]) -> dict[tuple[str | None, str | None], list[Any]]:
    grouped: dict[tuple[str | None, str | None], list[Any]] = defaultdict(list)
    for item in items:
        grouped[(item.session, item.prompt)].append(item)
    return grouped


def pair_calls(events: SkillEvents) -> None:
    """Give each activation that made a Skill call (every trigger but
    `user-slash`) the `tool_use_id` of the first unpaired Skill call after it
    in the same session and prompt: by `event.sequence`, else by timestamp."""
    calls = _by_prompt(events.calls)
    for key, activations in _by_prompt(events.activations).items():
        free = sorted(calls.get(key, []), key=_order)
        for activation in sorted(activations, key=_order):
            if activation.trigger == USER_SLASH:
                continue
            match = next((call for call in free if _follows(call, activation)), None)
            if match is not None:
                activation.tool_use_id = match.tool_use_id
                free.remove(match)


def _name(activation: Activation, name: str, source: str) -> None:
    activation.name = name
    activation.recovered_from = source


def _apply_plugin_records(events: SkillEvents) -> None:
    for activation in events.activations:
        if activation.unnamed and activation.tool_use_id in events.native_tools:
            _name(activation, events.native_tools[activation.tool_use_id], FROM_PLUGIN_TOOL)

    activations = _by_prompt(events.activations)
    for key, records in _by_prompt(events.native_skills).items():
        names = [record.name for record in sorted(records, key=lambda record: record.timestamp or "")]
        in_prompt = sorted(activations.get(key, []), key=_order)
        for activation in in_prompt:
            if not activation.unnamed and activation.name in names:
                names.remove(activation.name)
        for activation in in_prompt:
            if activation.unnamed and names:
                _name(activation, names.pop(0), FROM_PLUGIN_SKILL)
        for name in names:
            session, prompt = key
            if not any(record.kept for record in records):
                continue
            events.activations.append(
                Activation(name, "?", "?", session, prompt, timestamp=records[0].timestamp, plugin_only=True)
            )


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _scan_transcripts(sessions: set[str], dirs: list[Path]) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """(tool_use_id -> skill of a Skill tool_use, (session, promptId) -> first
    slash command) from the transcripts of `sessions` under `dirs`."""
    uses: dict[str, str] = {}
    slashes: dict[tuple[str, str], str] = {}
    for directory in dirs:
        for context in ti.discover_transcripts(directory):
            if context.session_id not in sessions:
                continue
            for record in ti.read_jsonl(context.transcript_path):
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if record.get("type") == "assistant" and isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("name") == SKILL_TOOL
                            and isinstance(block.get("id"), str)
                            and isinstance(block.get("input"), dict)
                        ):
                            name = _skill_name(block["input"].get("skill"))
                            if name is not None:
                                uses[block["id"]] = name
                elif record.get("type") == "user" and isinstance(prompt_id := record.get("promptId"), str):
                    key = (context.session_id, prompt_id)
                    match = COMMAND_NAME.search(_text_of(content))
                    if key not in slashes and match is not None:
                        name = _skill_name(match.group(1))
                        if name is not None:
                            slashes[key] = name
    return uses, slashes


def _apply_transcripts(events: SkillEvents, dirs: list[Path]) -> None:
    unnamed = [activation for activation in events.activations if activation.unnamed]
    sessions = {activation.session for activation in unnamed if activation.session}
    if not sessions:
        return
    uses, slashes = _scan_transcripts(sessions, dirs)
    for activation in unnamed:
        if activation.tool_use_id in uses:
            _name(activation, uses[activation.tool_use_id], FROM_TRANSCRIPT_TOOL)
        elif (
            activation.trigger == USER_SLASH
            and activation.session is not None
            and activation.prompt is not None
            and (activation.session, activation.prompt) in slashes
        ):
            _name(activation, slashes[(activation.session, activation.prompt)], FROM_TRANSCRIPT_SLASH)


def recover_names(events: SkillEvents, project_dir: Path | None) -> None:
    """Name the redacted activations in place, and add the plugin-only ones.
    Transcripts are read only if some activation is still unnamed."""
    pair_calls(events)
    _apply_plugin_records(events)
    if any(activation.unnamed for activation in events.activations):
        _apply_transcripts(events, tr.transcript_dirs(project_dir))


# --- aggregation -------------------------------------------------------------------


@dataclasses.dataclass
class SkillStats:
    activations: int = 0
    sessions: set[str] = dataclasses.field(default_factory=set)
    prompts: set[str] = dataclasses.field(default_factory=set)
    triggers: Counter[str] = dataclasses.field(default_factory=Counter)


@dataclasses.dataclass
class SkillSummary:
    skills: dict[tuple[str, str], SkillStats]
    by_source: dict[str, Counter[str]]
    triggers: list[str]
    activations: list[Activation]
    # skill name -> [tokens, cost]
    attributed: dict[str, list[float]]
    unattributed: list[float]
    api_total: list[float]


def summarize(events: SkillEvents) -> SkillSummary:
    skills: dict[tuple[str, str], SkillStats] = defaultdict(SkillStats)
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    per_prompt: dict[str, Counter[str]] = defaultdict(Counter)
    for activation in events.activations:
        stats = skills[(activation.name, activation.source)]
        stats.activations += 1
        stats.triggers[activation.trigger] += 1
        if activation.session is not None:
            stats.sessions.add(activation.session)
        if activation.prompt is not None:
            stats.prompts.add(activation.prompt)
            per_prompt[activation.prompt][activation.name] += 1
        by_source[activation.source][activation.trigger] += 1

    # Activation-share attribution at the activation prompt, the approximation
    # the tool view uses for tool calls.
    attributed: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    unattributed = [0.0, 0.0]
    for prompt, (tokens, cost) in events.prompt_usage.items():
        mix = per_prompt.get(prompt)
        if not mix:
            unattributed[0] += tokens
            unattributed[1] += cost
            continue
        total = sum(mix.values())
        for name, count in mix.items():
            attributed[name][0] += tokens * count / total
            attributed[name][1] += cost * count / total

    observed = {activation.trigger for activation in events.activations}
    triggers = [*KNOWN_TRIGGERS, *sorted(observed - set(KNOWN_TRIGGERS))]
    return SkillSummary(
        skills=dict(skills),
        by_source=dict(by_source),
        triggers=triggers,
        activations=events.activations,
        attributed=dict(attributed),
        unattributed=unattributed,
        api_total=[
            sum(usage[0] for usage in events.prompt_usage.values()),
            sum(usage[1] for usage in events.prompt_usage.values()),
        ],
    )


# --- load overhead -----------------------------------------------------------------


@dataclasses.dataclass
class OverheadStats:
    loads: int = 0
    tokens: float = 0.0
    rereads: int = 0
    reread_tokens: float = 0.0
    cost: float = 0.0
    unpriced: bool = False
    priced: bool = False


@dataclasses.dataclass
class Overhead:
    skills: dict[str, OverheadStats]
    assumed_main: int = 0
    unpriced_steps: dict[str, set[int]] = dataclasses.field(default_factory=dict)  # model -> ids of its steps


def _load_agents(events: SkillEvents) -> list[tuple[NativeSkill, str | None, bool]]:
    """Each native `skill` record with the agent of its Skill `tool` record:
    paired one to one per session, prompt and name in timestamp order, a call
    no earlier than the load. (load, agent, paired)."""
    calls: dict[tuple[str | None, str | None, str], list[NativeCall]] = defaultdict(list)
    for call in sorted(events.native_calls, key=lambda call: call.timestamp or ""):
        calls[(call.session, call.prompt, call.name)].append(call)
    loads = []
    for load in sorted(events.native_skills, key=lambda load: load.timestamp or ""):
        free = calls.get((load.session, load.prompt, load.name), [])
        match = next((call for call in free if (call.timestamp or "") >= (load.timestamp or "")), None)
        if match is not None:
            free.remove(match)
        loads.append((load, match.agent if match else None, match is not None))
    return loads


def skill_overhead(events: SkillEvents) -> Overhead:
    """Per skill: loads, loaded tokens (prompt_chars / 4), and the later model
    requests of the loading agent until it compacts -- the first writes the
    skill to the cache, each later one re-reads it."""
    steps: dict[tuple[str | None, str | None], list[NativeStep]] = defaultdict(list)
    for step in sorted(events.steps, key=lambda step: step.timestamp):
        steps[(step.session, step.agent)].append(step)
    overhead = Overhead(skills=defaultdict(OverheadStats))
    for load, agent, paired in _load_agents(events):
        if not load.kept:
            continue
        stats = overhead.skills[load.name]
        tokens = load.chars / CHARS_PER_TOKEN
        stats.loads += 1
        stats.tokens += tokens
        overhead.assumed_main += not paired
        start = load.timestamp or ""
        end = min((ts for ts in events.compactions.get((load.session, agent), []) if ts > start), default=None)
        window = [step for step in steps.get((load.session, agent), []) if step.timestamp > start]
        if end is not None:
            window = [step for step in window if step.timestamp < end]
        for index, step in enumerate(window):
            if index:
                stats.rereads += 1
                stats.reread_tokens += tokens
            prices = model_prices(step.model, step.prompt_tokens)
            if prices is None:
                stats.unpriced = True
                overhead.unpriced_steps.setdefault(step.model or "?", set()).add(id(step))
                continue
            input_price, read_price = prices
            stats.priced = True
            stats.cost += tokens * (read_price if index else CACHE_WRITE_5M * input_price) / 1_000_000
    overhead.skills = dict(overhead.skills)
    return overhead


# --- formatting --------------------------------------------------------------------


def _cut(name: str) -> str:
    return name if len(name) <= NAME_WIDTH else name[: NAME_WIDTH - 1] + "…"


def _trigger_cells(counts: Mapping[str, int], triggers: list[str]) -> str:
    return " ".join(f"{counts.get(trigger, 0):>{max(len(trigger), 5)},}" for trigger in triggers)


def _trigger_header(triggers: list[str]) -> str:
    return " ".join(f"{trigger:>{max(len(trigger), 5)}}" for trigger in triggers)


def _skills_table(summary: SkillSummary) -> list[str]:
    header = f"{'skill':<{NAME_WIDTH}} {'source':<16} {'acts':>6} {'sessions':>8} {'prompts':>8}  " + _trigger_header(
        summary.triggers
    )
    rule = "-" * len(header)
    lines = ["Skills by activations (redacted custom_skill names recovered locally; see the footer)", header, rule]
    rows = sorted(summary.skills.items(), key=lambda item: (-item[1].activations, item[0]))
    for (name, source), stats in rows:
        lines.append(
            f"{_cut(name):<{NAME_WIDTH}} {source:<16} {stats.activations:>6,} {len(stats.sessions):>8,}"
            f" {len(stats.prompts):>8,}  {_trigger_cells(stats.triggers, summary.triggers)}"
        )
    totals: Counter[str] = Counter()
    for stats in summary.skills.values():
        totals.update(stats.triggers)
    sessions = {a.session for a in summary.activations if a.session is not None}
    prompts = {a.prompt for a in summary.activations if a.prompt is not None}
    lines.append(rule)
    lines.append(
        f"{'TOTAL':<{NAME_WIDTH}} {'':<16} {len(summary.activations):>6,} {len(sessions):>8,}"
        f" {len(prompts):>8,}  {_trigger_cells(totals, summary.triggers)}"
    )
    return lines


def _source_table(summary: SkillSummary) -> list[str]:
    header = f"{'source':<16} " + _trigger_header(summary.triggers) + f" {'total':>6}"
    lines = ["Sources by trigger (activations)", header, "-" * len(header)]
    for source, counts in sorted(summary.by_source.items(), key=lambda item: (-sum(item[1].values()), item[0])):
        lines.append(f"{source:<16} {_trigger_cells(counts, summary.triggers)} {sum(counts.values()):>6,}")
    return lines


def _attribution_table(summary: SkillSummary) -> list[str]:
    width = NAME_WIDTH + 2
    lines = [
        "Tokens in prompts that activated the skill (activation share at the activation prompt)",
        f"{'skill':<{width}} {'tokens':>15} {'cost_usd':>10}",
        "-" * (width + 27),
    ]
    rows = sorted(summary.attributed.items(), key=lambda item: (-item[1][1], -item[1][0], item[0]))
    for name, (tokens, cost) in rows:
        lines.append(f"{_cut(name):<{width}} {tokens:>15,.0f} {cost:>10,.2f}")
    tokens, cost = summary.unattributed
    lines.append(f"{NO_SKILL:<{width}} {tokens:>15,.0f} {cost:>10,.2f}")
    lines.append("-" * (width + 27))
    tokens, cost = summary.api_total
    lines.append(f"{'TOTAL':<{width}} {tokens:>15,.0f} {cost:>10,.2f}")
    lines.append(
        "tokens = input + output + cacheRead + cacheCreation of claude_code.api_request; a prompt's usage is split"
    )
    lines.append("across the skills it activated by activation count. This is work done while the skill was active,")
    lines.append("not what the skill costs: see Skill load overhead for that.")
    return lines


def _cost_cell(stats: OverheadStats) -> str:
    if not stats.priced:
        return "-"
    return f"{stats.cost:,.4f}" + ("+?" if stats.unpriced else "")


def _overhead_table(overhead: Overhead | None) -> list[str]:
    if overhead is None:
        return [OVERHEAD_NEEDS_PLUGIN]
    header = (
        f"{'skill':<{NAME_WIDTH}} {'loads':>6} {'tokens':>10} {'rereads':>8} {'reread_tokens':>14} {'cost_usd':>10}"
    )
    rule = "-" * len(header)
    lines = ["Skill load overhead (plugin skill records; tokens = prompt_chars / 4)", header, rule]

    def row(name: str, stats: OverheadStats) -> str:
        return (
            f"{_cut(name):<{NAME_WIDTH}} {stats.loads:>6,} {stats.tokens:>10,.0f} {stats.rereads:>8,}"
            f" {stats.reread_tokens:>14,.0f} {_cost_cell(stats):>10}"
        )

    rows = sorted(overhead.skills.items(), key=lambda item: (-item[1].cost, -item[1].tokens, item[0]))
    lines += [row(name, stats) for name, stats in rows]
    total = OverheadStats(
        loads=sum(stats.loads for stats in overhead.skills.values()),
        tokens=sum(stats.tokens for stats in overhead.skills.values()),
        rereads=sum(stats.rereads for stats in overhead.skills.values()),
        reread_tokens=sum(stats.reread_tokens for stats in overhead.skills.values()),
        cost=sum(stats.cost for stats in overhead.skills.values()),
        unpriced=any(stats.unpriced for stats in overhead.skills.values()),
        priced=any(stats.priced for stats in overhead.skills.values()),
    )
    lines += [rule, row("TOTAL", total)]
    lines.append(
        "cost = one cache write at the loading agent's next request "
        f"(5-minute rate, {CACHE_WRITE_5M:g}x input; a 1-hour write is {CACHE_WRITE_1H:g}x)"
    )
    lines.append("+ one cache read per later request of that agent, until its run ends or compacts.")
    if overhead.assumed_main:
        plural = "s" if overhead.assumed_main != 1 else ""
        lines.append(f"{overhead.assumed_main:,} load{plural} without a Skill tool record, assumed in the main loop")
    return lines


def _unpriced_warning(overhead: Overhead | None) -> list[str]:
    """The shared unpriced-models line, printed last in the report."""
    if overhead is None:
        return []
    footer = unpriced_footer({model: len(steps) for model, steps in overhead.unpriced_steps.items()})
    return [] if footer is None else [footer]


def _sessions_table(summary: SkillSummary, limit: int) -> list[str]:
    per_session: dict[str, list[Activation]] = defaultdict(list)
    for activation in summary.activations:
        per_session[activation.session or "?"].append(activation)
    last = {
        session: max((a.timestamp or "" for a in activations), default="")
        for session, activations in per_session.items()
    }
    lines = [
        "Sessions with skill activations (most recent first)",
        f"{'session':<8} {'last_activation':<16} {'acts':>6}  top skills",
        "-" * 80,
    ]
    for session in sorted(per_session, key=lambda s: (last[s], s), reverse=True)[:limit]:
        activations = per_session[session]
        mix = Counter(a.name for a in activations)
        top = ", ".join(
            f"{name} {count:,}" for name, count in sorted(mix.items(), key=lambda item: (-item[1], item[0]))[:3]
        )
        lines.append(
            f"{tr.session_label(session):<8} {tr.when_label(last[session] or None):<16} {len(activations):>6,}  {top}"
        )
    if len(per_session) > limit:
        lines.append(f"... {len(per_session) - limit:,} more sessions (raise --limit)")
    return lines


def _footer(summary: SkillSummary) -> list[str]:
    lines = []
    redacted = [a for a in summary.activations if not a.plugin_only and (a.recovered_from or a.name == REDACTED)]
    if redacted:
        sources = Counter(a.recovered_from for a in redacted if a.recovered_from)
        recovered = sum(sources.values())
        detail = ", ".join(f"{source} {sources[source]:,}" for source in NAME_SOURCES if sources[source])
        lines.append(
            f"{REDACTED} names recovered: {recovered:,} of {len(redacted):,}"
            + (f" ({detail})" if detail else "")
            + f"; {len(redacted) - recovered:,} not recovered"
        )
    plugin_only = sum(a.plugin_only for a in summary.activations)
    if plugin_only:
        lines.append(
            f"plugin-only: {plugin_only:,} activation{'s' if plugin_only != 1 else ''} from plugin skill records"
            " without a skill_activated event (source and trigger ?)"
        )
    return lines


def format_skill_report(summary: SkillSummary, limit: int | None = None, overhead: Overhead | None = None) -> str:
    """`overhead` None means the capture has no plugin skill records."""
    if not summary.activations:
        return EMPTY_MESSAGE
    blocks = [
        _skills_table(summary),
        _source_table(summary),
        _attribution_table(summary),
        _overhead_table(overhead),
        _sessions_table(summary, limit or tr.DEFAULT_SESSION_LIMIT),
        _footer(summary),
        _unpriced_warning(overhead),
    ]
    return "\n\n".join("\n".join(block) for block in blocks if block)


def build_report(
    capture: Path, project_filter: ProjectFilter | None, project_dir: Path | None, limit: int | None = None
) -> str:
    """The whole `--skills` view: load, recover names, summarize, format."""
    events = load_events(capture, project_filter)
    recover_names(events, project_dir)
    overhead = skill_overhead(events) if events.native_skills else None
    return format_skill_report(summarize(events), limit, overhead)
