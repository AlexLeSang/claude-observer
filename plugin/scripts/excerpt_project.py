"""The allowlist projection behind the doctor's excerpt.

The excerpt is text a user posts publicly, so this module is built to hold NO free text, by construction:

* The output is built from a TABLE (`KINDS`): for each known record kind, the input fields it keeps and what each
  becomes. The walk is over the table, never over the record's own keys, so a field the table does not name (a
  future one, a `prompt`, a `ticket`, `tool_use_id`, `run`, `seq`) is never read, never copied.
* Each kept field goes through one converter, and a converter returns only: a bounded number (by NAMED key, never
  "any number": `agent_spawn.ticket` is a ticket id and is not in the table), a boolean, an element of a fixed set
  (a string outside the set becomes `other`), a model or version string that matched a strict pattern (else
  `other`), a pseudonym `<category>-N` (built-in names of a fixed set are kept), a count (the length of a list), or
  a time cut to the minute. A value of the wrong type is dropped.
* A record of an unknown kind, an OTel mirror record (no `kind`; `{ts, path, source, payload}`; dropped whole), a
  line that is not a JSON object, a line over `MAX_LINE`, a repeated key, NaN and a nested-too-deep line are
  counted and never shown.
* Defence in depth: `check_projection` refuses the whole excerpt if any key is not an allowed output key or any
  string value does not match `STRING_OK` (letters, digits and `._:@[]-`, at most 64 characters). That cannot fire
  unless a converter is wrong; it is the net under the table.

Pure: no file, no clock, no environment. Pseudonym maps live in memory for one call and are never returned.
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

MAX_LINE = 64 * 1024  # a longer line is counted, never parsed
MAX_NUM = 10**12
MAX_FLOAT = 1e12
MAX_DIGITS = 18  # a longer integer literal is out of range without being converted
MAX_KEEP = 50
LABEL_KEY_CAP = 256  # a longer name is cut for the pseudonym map only
RATE_LIMITS_CAP = 8
# Every string value the excerpt may hold (a pseudonym, an enum member, a model or version string, a kind, a time).
STRING_OK = re.compile(r"[A-Za-z0-9._:@\[\]-]{1,64}", re.ASCII)

DROP: Any = object()  # a converter's "this value is not kept"


class ProjectionRefused(Exception):  # noqa: N818  (a refusal, not an error)
    """A value outside the output grammar reached the net: no excerpt."""


# ---- fixed sets -------------------------------------------------------------------------------------------------

SURFACES = frozenset({"terminal", "desktop", "vscode", "mobile"})  # doctor_checks.SURFACES, equal by test
STOPS = frozenset(
    {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal", "model_context_window_exceeded"}
)
TURN_REASONS = frozenset({"answer", "aborted", "refusal", "error"})
END_REASONS = frozenset({"clear", "resume", "logout", "prompt_input_exit", "bypass_permissions_disabled", "other"})
SHAPES = frozenset({"fresh", "fork", "teammate"})
TRIGGERS = frozenset({"manual", "auto", "plugin"})
ORIGINS = frozenset({"engine", "hook", "plugin"})
DEFINITIONS = frozenset(
    {
        "built-in",
        "plugin",
        "userSettings",
        "projectSettings",
        "localSettings",
        "policySettings",
        "flagSettings",
        "cliArg",
    }
)
TIERS = frozenset(
    {"core", "builtin", "built-in", "bundled", "engine", "user", "project", "local", "managed", "plugin", "mcp"}
)
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
RATE_KINDS = frozenset({"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "session", "weekly"})
HOOK_EVENTS = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "UserPromptSubmit",
        "Notification",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "SessionStart",
        "SessionEnd",
        "PermissionRequest",
        "unknown",
    }
)
# plugin/hooks/records.ts REPORT_VIEWS, equal by test.
REPORT_VIEWS = frozenset(
    {
        "sessions",
        "subagents",
        "models",
        "tickets",
        "context",
        "fixed-context",
        "skills",
        "plugins",
        "digest",
        "personas",
    }
)
PROVENANCES = frozenset({"@inline", "@builtin"})
BUILTIN_TOOLS = frozenset(
    {
        "Bash",
        "BashOutput",
        "KillShell",
        "Read",
        "Edit",
        "MultiEdit",
        "Write",
        "Glob",
        "Grep",
        "Agent",
        "Task",
        "Skill",
        "WebFetch",
        "WebSearch",
        "NotebookEdit",
        "TodoWrite",
        "ToolSearch",
        "AskUserQuestion",
        "ExitPlanMode",
        "SlashCommand",
    }
)
BUILTIN_AGENT_TYPES = frozenset(
    {"fork", "Explore", "Plan", "general-purpose", "statusline-setup", "claude-code-guide", "output-style-setup"}
)
BUILTIN_PLUGINS = frozenset({"usage-observer"})
BUILTIN_PROVIDERS = frozenset({"engine"})

_MODEL = re.compile(
    r"(?:claude-(?:(?:opus|sonnet|haiku)(?:-[0-9]{1,2}){1,2}|[0-9]{1,2}(?:-[0-9]{1,2})?-(?:opus|sonnet|haiku))"
    r"(?:-[0-9]{8}|-latest)?|opus|sonnet|haiku|opusplan)(?:\[1m\])?",
    re.ASCII,
)
_VERSION = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){2}", re.ASCII)
_TS = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2})(?::[0-9]{2}(?:\.[0-9]{1,9})?)?Z", re.ASCII)


# ---- pseudonyms -------------------------------------------------------------------------------------------------


class Names:
    """Pseudonyms stable within one projection: `<category>-N`, N counting from 1 in order of first sight. The
    category is a constant of this module, never data. The map is never exposed."""

    def __init__(self) -> None:
        self._maps: dict[str, dict[str, int]] = {}

    def label(self, category: str, value: str) -> str:
        seen = self._maps.setdefault(category, {})
        number = seen.setdefault(value[:LABEL_KEY_CAP], len(seen) + 1)
        return f"{category}-{number}"


# ---- converters: (names, value) -> kept value or DROP --------------------------------------------------------------


def num(_names: Names, value: object) -> object:
    return value if type(value) is int and 0 <= value <= MAX_NUM else DROP


def flt(_names: Names, value: object) -> object:
    if type(value) is int:
        return num(_names, value)
    if type(value) is float and math.isfinite(value) and 0 <= value <= MAX_FLOAT:
        return round(value, 4)
    return DROP


def flag(_names: Names, value: object) -> object:
    return value if type(value) is bool else DROP


def numflag(names: Names, value: object) -> object:
    """A bool stays a flag, a list becomes its length (the engine's `changed` is a list of names, never kept), an int a
    count."""
    if type(value) is bool:
        return flag(names, value)
    return num(names, len(value)) if isinstance(value, list) else num(names, value)


def opt(conv: Any) -> Any:
    """`conv`, but `null` stays `null` (a field the engine did not record)."""

    def convert(names: Names, value: object) -> object:
        return None if value is None else conv(names, value)

    return convert


def enum(allowed: frozenset[str]) -> Any:
    def convert(_names: Names, value: object) -> object:
        if not isinstance(value, str):
            return DROP
        return value if value in allowed else "other"

    return convert


def effort(names: Names, value: object) -> object:
    return num(names, value) if type(value) is int else enum(EFFORTS)(names, value)


def _pattern(regex: re.Pattern[str]) -> Any:
    def convert(_names: Names, value: object) -> object:
        if not isinstance(value, str):
            return DROP
        return value if len(value) <= 64 and regex.fullmatch(value) else "other"

    return convert


model = _pattern(_MODEL)
version = _pattern(_VERSION)


def name(category: str, builtin: frozenset[str] = frozenset()) -> Any:
    def convert(names: Names, value: object) -> object:
        if not isinstance(value, str):
            return DROP
        return value if value in builtin else names.label(category, value)

    return convert


def provider(names: Names, value: object) -> object:
    return name("provider", BUILTIN_PROVIDERS)(names, value)


def count(_names: Names, value: object) -> object:
    return len(value) if isinstance(value, list) else DROP


def present(_names: Names, value: object) -> object:
    """Only whether the field holds a value (a skip reason is text; it becomes a boolean)."""
    return value is not None


def ts(_names: Names, value: object) -> object:
    if not isinstance(value, str) or len(value) > 40:
        return DROP
    match = _TS.fullmatch(value)
    return DROP if match is None else match[1] + "Z"


def _sub(table: dict[str, tuple[str, Any]]) -> Any:
    def convert(names: Names, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            return DROP
        return _apply(table, value, names)

    return convert


def _apply(table: dict[str, tuple[str, Any]], record: dict[str, Any], names: Names) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for source, (key, convert) in table.items():
        if source in record:
            got = convert(names, record[source])
            if got is not DROP:
                out[key] = got
    return out


_USAGE_TABLE: dict[str, tuple[str, Any]] = {
    "input_tokens": ("input_tokens", num),
    "output_tokens": ("output_tokens", num),
    "cache_read_input_tokens": ("cache_read_input_tokens", num),
    "cache_creation_input_tokens": ("cache_creation_input_tokens", num),
    "model": ("model", model),
}
_RATE_TABLE: dict[str, tuple[str, Any]] = {"kind": ("kind", enum(RATE_KINDS)), "percent": ("percent", flt)}
usage = _sub(_USAGE_TABLE)


def rate_limits(names: Names, value: object) -> object:
    if not isinstance(value, list):
        return DROP
    return [_apply(_RATE_TABLE, item, names) for item in value[:RATE_LIMITS_CAP] if isinstance(item, dict)]


# ---- the table --------------------------------------------------------------------------------------------------

Table = dict[str, tuple[str, Any]]

AGENT: Table = {
    "agent": ("agent", opt(name("agent"))),
    "agent_type": ("agent_type", opt(name("agent-type", BUILTIN_AGENT_TYPES))),
}
COMMON: Table = {"ts": ("ts", ts), "session": ("session", name("session"))}

KINDS: dict[str, Table] = {
    "session_start": {
        "version": ("version", version),
        "model": ("model", opt(model)),
        "interactive": ("interactive", flag),
        "surface": ("surface", opt(enum(SURFACES))),
    },
    "session_end": {"reason": ("reason", opt(enum(END_REASONS)))},
    "turn_start": {"prompt_chars": ("prompt_chars", num)},
    "step": {
        "index": ("index", num),
        **AGENT,
        "model": ("model", model),
        "effort": ("effort", opt(effort)),
        "messages": ("messages", num),
        "stop": ("stop", opt(enum(STOPS))),
        "tools": ("tools_count", count),
        "usage": ("usage", usage),
        "thinking_chars": ("thinking_chars", num),
        "ms": ("ms", num),
    },
    "turn": {
        **AGENT,
        "reason": ("reason", enum(TURN_REASONS)),
        "ms": ("ms", num),
        "usage": ("usage", usage),
    },
    "tool": {
        "tool": ("tool", name("tool", BUILTIN_TOOLS)),
        **AGENT,
        "ms": ("ms", num),
        "error": ("error", flag),
        "denied": ("denied", flag),
        "input_chars": ("input_chars", num),
        "result_chars": ("result_chars", num),
        "skill": ("skill", opt(name("skill"))),
        "subagent_type": ("subagent_type", opt(name("agent-type", BUILTIN_AGENT_TYPES))),
        "mcp_server": ("mcp_server", opt(name("mcp"))),
        "report_view": ("report_view", opt(enum(REPORT_VIEWS))),
    },
    "skill": {"skill": ("skill", opt(name("skill"))), "prompt_chars": ("prompt_chars", num)},
    "compaction": {
        "trigger": ("trigger", enum(TRIGGERS)),
        "agent": ("agent", opt(name("agent"))),
        "tokens_before": ("tokens_before", opt(num)),
        "tokens_after": ("tokens_after", opt(num)),
        "usage": ("usage", usage),
        "skip": ("skipped", present),
        "ms": ("ms", num),
    },
    "measure": {
        "changed": ("changed", numflag),
        "context_tokens": ("context_tokens", opt(num)),
        "context_window": ("context_window", opt(num)),
        "context_percent": ("context_percent", opt(flt)),
        "cost_usd": ("cost_usd", opt(flt)),
        "rate_limits": ("rate_limits", rate_limits),
    },
    "prompt_compose": {
        "model": ("model", model),
        "prompt_model": ("prompt_model", model),
        "traits": ("traits_count", count),
        "tool_count": ("tool_count", num),
        "sections": ("sections_count", count),
        "chars": ("chars", num),
    },
    "prompt_context": {
        "blocks": ("blocks_count", count),
        "chars": ("chars", num),
        "instruction_files": ("instruction_files", opt(num)),
    },
    "prompt_attachment": {
        "attachment": ("attachment", name("attachment")),
        "origin": ("origin", enum(ORIGINS)),
        **AGENT,
        "chars": ("chars", num),
    },
    "tool_describe": {
        "tool": ("tool", name("tool", BUILTIN_TOOLS)),
        "chars": ("chars", num),
        "deferred": ("deferred", flag),
        "provider": ("provider", provider),
        "tier": ("tier", enum(TIERS)),
        "mcp_server": ("mcp_server", opt(name("mcp"))),
    },
    "agent_spawn": {
        "shape": ("shape", enum(SHAPES)),
        "subagent_type": ("subagent_type", opt(name("agent-type", BUILTIN_AGENT_TYPES))),
        "provider": ("provider", provider),
        "tier": ("tier", enum(TIERS)),
        "agent": ("agent", opt(name("agent"))),
        "spawned": ("spawned", opt(name("agent"))),
        "model_requested": ("model_requested", opt(model)),
        "model": ("model", opt(model)),
        "parent_model": ("parent_model", opt(model)),
        "background": ("background", flag),
        "named": ("named", flag),
        "cwd_set": ("cwd_set", flag),
        "prompt_chars": ("prompt_chars", num),
        "description_chars": ("description_chars", num),
        "denied": ("denied", flag),
    },
    "agent_offer": {
        "subagent_type": ("subagent_type", opt(name("agent-type", BUILTIN_AGENT_TYPES))),
        "definition": ("definition", enum(DEFINITIONS)),
        "provider": ("provider", provider),
        "tier": ("tier", enum(TIERS)),
        "chars": ("chars", num),
        "offered": ("offered", flag),
    },
    "plugin_set": {"enabled": ("enabled_count", count)},
    "plugin_register": {
        "plugin": ("plugin", name("plugin", BUILTIN_PLUGINS)),
        "provenance": ("provenance", enum(PROVENANCES)),
        "tier": ("tier", enum(TIERS)),
        "events": ("events_count", count),
        "calls": ("calls_count", count),
    },
    "hook_runs": {
        "hook_event": ("hook_event", enum(HOOK_EVENTS)),
        "tool": ("tool", opt(name("tool", BUILTIN_TOOLS))),
        "runs": ("runs", num),
        "ms": ("ms", num),
        "max_ms": ("max_ms", num),
    },
}
for _kind, _table in KINDS.items():
    KINDS[_kind] = {**COMMON, **_table}

ALLOWED_KEYS = frozenset(
    {"kind"}
    | {key for table in KINDS.values() for key, _ in table.values()}
    | {key for key, _ in _USAGE_TABLE.values()}
    | {key for key, _ in _RATE_TABLE.values()}
)


# ---- the net ----------------------------------------------------------------------------------------------------


def check_projection(value: object, depth: int = 0) -> None:
    """Raise `ProjectionRefused` unless `value` is built only of allowed keys, grammar-checked strings, bounded
    numbers, booleans and null."""
    if depth > 4:
        raise ProjectionRefused
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in ALLOWED_KEYS:
                raise ProjectionRefused
            check_projection(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            check_projection(item, depth + 1)
    elif isinstance(value, str):
        if not STRING_OK.fullmatch(value):
            raise ProjectionRefused
    elif value is None or isinstance(value, bool):
        return
    elif type(value) is int:
        if not 0 <= value <= MAX_NUM:
            raise ProjectionRefused
    elif type(value) is float:
        if not (math.isfinite(value) and 0 <= value <= MAX_FLOAT):
            raise ProjectionRefused
    else:
        raise ProjectionRefused


def project_record(record: dict[str, Any], names: Names) -> dict[str, Any]:
    """One record of a known kind, through its table. The caller has checked `record["kind"]`."""
    kind = record["kind"]
    out: dict[str, Any] = {"kind": kind}
    out.update(_apply(KINDS[kind], record, names))
    check_projection(out)
    return out


# ---- reading lines ----------------------------------------------------------------------------------------------


class _Repeated(ValueError):  # noqa: N818
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record = dict(pairs)
    if len(record) != len(pairs):
        raise _Repeated
    return record


def _constant(_text: str) -> Any:
    raise ValueError  # NaN, Infinity, -Infinity


def _integer(text: str) -> Any:
    return int(text) if len(text) <= MAX_DIGITS else math.inf  # out of range by construction, never converted


def _parse(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant, parse_int=_integer)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass
class Projection:
    lines: int = 0
    # Records of a known kind, per kind (the kind names are this module's constants).
    counts: dict[str, int] = field(default_factory=dict)
    otel: int = 0
    unknown_kind: int = 0
    unreadable: int = 0
    oversize: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)


def project_lines(lines: list[bytes], keep: int) -> Projection:
    """Count every line; project the last `keep` (at most MAX_KEEP) records of a known kind."""
    keep = max(0, min(keep, MAX_KEEP))
    result = Projection(lines=len(lines))
    last: deque[dict[str, Any]] = deque(maxlen=keep) if keep else deque(maxlen=1)
    for raw in lines:
        if len(raw) > MAX_LINE:
            result.oversize += 1
            continue
        record = _parse(raw)
        if record is None:
            result.unreadable += 1
            continue
        kind = record.get("kind")
        if (kind is None and isinstance(record.get("payload"), dict)) or kind == "otel":
            result.otel += 1
        elif isinstance(kind, str) and kind in KINDS:
            result.counts[kind] = result.counts.get(kind, 0) + 1
            last.append(record)
        else:
            result.unknown_kind += 1
    if keep:
        names = Names()
        result.records = [project_record(record, names) for record in last]
    return result
