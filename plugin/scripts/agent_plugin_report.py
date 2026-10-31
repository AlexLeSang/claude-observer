#!/usr/bin/env python3
"""Plugin view for `agent_telemetry.py report --plugins`.

Reads `claude_code.plugin_loaded` (one per enabled plugin at session start),
`plugin_installed`, `hook_registered`, `hook_execution_complete`,
`skill_activated` and `api_request` log events from the OTLP capture and prints:

- the plugin inventory: sessions with each plugin loaded, first and last seen,
  versions;
- version drift, and an installs timeline;
- session cost with and without each plugin (a correlation, not the plugin's
  own cost);
- hook registrations per plugin and the merged hook activity;
- each plugin's hook overhead recovered from the project's local transcripts;
- from the usage-observer plugin's own records, for every session with a model request, `claude -p` included: the enabled plugin
  set, the hooks modules that joined the chain, and the wall time of the settings-hook chain per event and
  tool (silent hooks included), credited per session to the one plugin of that session whose hooks.json declares the event;
- skill activations per plugin.

Claude Code redacts non-official plugins to `third-party`. They are kept apart by
`plugin_id_hash` and named only when the local settings leave exactly one
candidate. Everything local (`~/.claude/plugins/installed_plugins.json`, settings,
plugin `hooks/hooks.json`, transcripts) is read on this machine and never sent
anywhere. Identity attributes are never read, and sessions are shown only as an
8-character prefix.

Each plugin's `hooks/hooks.json` is read under the install record's `installPath`. A marketplace added
from a local checkout runs the checkout's `plugin/` in place, which only `claude plugin list --json` names
(`readFromFolder`); for such an install the report reads the cached copy the install record names, which may
be stale, and no hooks at all when there is no record.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tool_report as tr
import transcript_iterator as ti
from safe_name import printable_line

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agent_telemetry import ProjectFilter

PLUGIN_LOADED = "plugin_loaded"
PLUGIN_INSTALLED = "plugin_installed"
HOOK_REGISTERED = "hook_registered"
HOOK_EXECUTION = "hook_execution_complete"
SKILL_ACTIVATED = "skill_activated"
EVENTS = (PLUGIN_LOADED, PLUGIN_INSTALLED, HOOK_REGISTERED, HOOK_EXECUTION, SKILL_ACTIVATED, tr.API_REQUEST)

THIRD_PARTY = "third-party"
HASH_PREFIX = 6
UNVERSIONED = "(unversioned)"
NOT_A_PLUGIN = "(not a plugin)"
AMBIGUOUS = "(ambiguous)"
HOOK_SUCCESS = "hook_success"
MIN_GROUP = 3
NAME_WIDTH = 34
CHARS_PER_TOKEN = 4

PLUGIN_SET = "plugin_set"
PLUGIN_REGISTER = "plugin_register"
HOOK_RUNS = "hook_runs"
SHARED = "(shared by plugins)"
NO_DECLARER = "(no plugin declares it)"
WITH_SETTINGS = "(plugins and settings hooks)"
SETTINGS_ONLY = "(settings hooks)"
WITH_MARKER = " + settings hooks"  # appended to the plugin names of a row a settings hook shares

EMPTY_MESSAGE = "No claude_code.plugin_loaded events found."
# plugin_loaded events exist, but only in sessions that never reached a model
IDLE_ONLY_MESSAGE = (
    "No claude_code.plugin_loaded events in a session with a model request (the row below counts the others)."
)
MIRROR_COVERAGE = (
    "usage-observer capture: its mirror carries plugin_loaded and hook_registered for interactive sessions only,"
    " not claude -p runs; the OTLP capture (--source logs/agent-telemetry.jsonl) has them for every session"
)
NEEDS_OTLP = (
    "No plugin_loaded events in the usage-observer capture: its mirror carries them for interactive sessions only,"
    " not claude -p runs. Use the OTLP capture (--source logs/agent-telemetry.jsonl)."
)

session_label = tr.session_label
_when = tr.when_label


@dataclasses.dataclass(frozen=True)
class PluginLoad:
    """One `plugin_loaded` event."""

    name: str
    marketplace: str
    scope: str
    version: str | None
    hash: str | None
    session: str | None
    timestamp: str | None


@dataclasses.dataclass(frozen=True)
class Install:
    """One `plugin_installed` event."""

    name: str
    version: str | None
    timestamp: str | None


@dataclasses.dataclass(frozen=True)
class Registration:
    """One `hook_registered` event of a plugin's hook."""

    plugin: str
    hash: str | None
    hook_event: str
    session: str | None


@dataclasses.dataclass(frozen=True)
class SkillUse:
    """One `skill_activated` event that names its plugin."""

    plugin: str
    hash: str | None
    skill: str
    session: str | None


@dataclasses.dataclass(frozen=True)
class ModuleRegistration:
    """One native `plugin_register` record: a hooks module that joined the chain."""

    plugin: str
    version: str | None
    provenance: str
    session: str | None
    events: tuple[str, ...]


@dataclasses.dataclass
class HookRunStats:
    runs: float = 0.0
    ms: float = 0.0
    max_ms: float = 0.0
    sessions: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class IdleSessions:
    """Sessions whose hooks ran but that never reached a model (`claude -p /usage` and the like)."""

    sessions: int = 0
    loaded: int = 0  # of them, sessions with a plugin_loaded event
    runs: float = 0.0
    ms: float = 0.0
    chars: float = 0.0
    # wall time of their settings-hook chain, from the usage-observer `hook_runs` records (a different count
    # from `runs`, which are hook_execution_complete events, so the two are never added)
    chain_runs: float = 0.0
    chain_ms: float = 0.0


@dataclasses.dataclass
class PluginEvents:
    loads: list[PluginLoad] = dataclasses.field(default_factory=list)
    installs: list[Install] = dataclasses.field(default_factory=list)
    registrations: list[Registration] = dataclasses.field(default_factory=list)
    skill_uses: list[SkillUse] = dataclasses.field(default_factory=list)
    # (session or None, hook_event) -> [executions, duration_ms, additional_context_chars], merged across plugins;
    # `load_events` folds it into `executions` once the sessions with no model request are set apart
    session_executions: dict[tuple[str | None, str], list[float]] = dataclasses.field(
        default_factory=lambda: defaultdict(lambda: [0.0, 0.0, 0.0])
    )
    # hook_event -> [executions, duration_ms, additional_context_chars] of the sessions with a model request
    executions: dict[str, list[float]] = dataclasses.field(default_factory=lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
    # sessions with a model request: an api_request event or a native step record
    requested: set[str] = dataclasses.field(default_factory=set)
    # the kept sessions with none, set apart from every count and total
    idle: IdleSessions = dataclasses.field(default_factory=IdleSessions)
    # every session seen in a kept event, plugin_loaded or not
    sessions: set[str] = dataclasses.field(default_factory=set)
    # session -> [tokens, cost_usd] from api_request events
    usage: dict[str, list[float]] = dataclasses.field(default_factory=lambda: defaultdict(lambda: [0.0, 0.0]))
    native_lines: int = 0
    # native records of the usage-observer plugin: session -> enabled plugin ids
    enabled: dict[str, set[str]] = dataclasses.field(default_factory=lambda: defaultdict(set))
    modules: list[ModuleRegistration] = dataclasses.field(default_factory=list)
    # (session or None, hook event, tool or None) -> settings-hook chain time
    native_runs: dict[tuple[str | None, str, str | None], HookRunStats] = dataclasses.field(
        default_factory=lambda: defaultdict(HookRunStats)
    )


def _str(value: object) -> str | None:
    return None if value is None else str(value)


def load_events(capture: Path, project_filter: ProjectFilter | None = None) -> PluginEvents:
    """Every event of interest that passes `project_filter`."""
    events = PluginEvents()
    for record in tr.capture_records(capture):
        if "payload" not in record:
            if record.get("source") == "mod":
                events.native_lines += 1
                _take_native(events, record, project_filter)
            continue
        for name, attrs in tr.iter_log_events(record.get("payload")):
            if name not in EVENTS:
                continue
            if name == PLUGIN_INSTALLED:
                # Machine-wide, so neither the project nor the session filter applies.
                events.installs.append(
                    Install(
                        str(attrs.get("plugin.name") or "?"),
                        _str(attrs.get("plugin.version")),
                        _str(attrs.get("event.timestamp")),
                    )
                )
                continue
            if project_filter is not None and not project_filter.keep(attrs):
                continue
            session = _str(attrs.get("session.id"))
            if session is not None:
                events.sessions.add(session)
            _take(events, name, attrs, session)
    return _set_apart_idle(events)


def _set_apart_idle(events: PluginEvents) -> PluginEvents:
    """Keep only the sessions with a model request in every list, count and total, and tally the rest in
    `events.idle`. A record with no session cannot be told apart and stays."""
    idle = {session for session in events.sessions if session not in events.requested}
    events.idle.sessions = len(idle)
    events.idle.loaded = len({load.session for load in events.loads if load.session in idle})
    events.sessions -= idle
    events.loads = [load for load in events.loads if load.session not in idle]
    events.registrations = [item for item in events.registrations if item.session not in idle]
    events.skill_uses = [use for use in events.skill_uses if use.session not in idle]
    events.modules = [module for module in events.modules if module.session not in idle]
    for session in idle:
        events.enabled.pop(session, None)
    for key, stats in events.native_runs.items():
        if key[0] in idle:
            events.idle.chain_runs += stats.runs
            events.idle.chain_ms += stats.ms
    events.native_runs = defaultdict(
        HookRunStats, {key: stats for key, stats in events.native_runs.items() if key[0] not in idle}
    )
    for (owner, hook_event), (runs, ms, chars) in events.session_executions.items():
        if owner in idle:
            events.idle.runs += runs
            events.idle.ms += ms
            events.idle.chars += chars
            continue
        row = events.executions[hook_event]
        row[0] += runs
        row[1] += ms
        row[2] += chars
    events.session_executions.clear()
    return events


def _take_native(events: PluginEvents, record: Mapping[str, Any], project_filter: ProjectFilter | None) -> None:
    kind = record.get("kind")
    if kind == "step":  # a native record of a model request
        step_session = _str(record.get("session"))
        if step_session is not None and (project_filter is None or project_filter.keep({"session.id": step_session})):
            events.sessions.add(step_session)
            events.requested.add(step_session)
        return
    if kind not in (PLUGIN_SET, PLUGIN_REGISTER, HOOK_RUNS):
        return
    session = _str(record.get("session"))
    if project_filter is not None and not project_filter.keep({"session.id": session}):
        return
    if session is not None:
        events.sessions.add(session)
    if kind == PLUGIN_SET:
        ids = record.get("enabled")
        if isinstance(ids, list):
            events.enabled[session or "?"].update(str(i) for i in ids)
    elif kind == PLUGIN_REGISTER:
        listed = record.get("events")
        events.modules.append(
            ModuleRegistration(
                str(record.get("plugin") or "?"),
                _str(record.get("version")),
                str(record.get("provenance") or "?"),
                session,
                tuple(str(i) for i in listed) if isinstance(listed, list) else (),
            )
        )
    else:
        stats = events.native_runs[(session, str(record.get("hook_event") or "?"), _str(record.get("tool")))]
        stats.runs += tr.as_number(record.get("runs")) or 0.0
        stats.ms += tr.as_number(record.get("ms")) or 0.0
        stats.max_ms = max(stats.max_ms, tr.as_number(record.get("max_ms")) or 0.0)
        if session is not None:
            stats.sessions.add(session)


def _take(events: PluginEvents, name: str | None, attrs: Mapping[str, Any], session: str | None) -> None:
    if name == PLUGIN_LOADED:
        events.loads.append(
            PluginLoad(
                name=str(attrs.get("plugin.name") or "?"),
                marketplace=str(attrs.get("marketplace.name") or "?"),
                scope=str(attrs.get("plugin.scope") or "?"),
                version=_str(attrs.get("plugin.version")),
                hash=_str(attrs.get("plugin_id_hash")),
                session=session,
                timestamp=_str(attrs.get("event.timestamp")),
            )
        )
    elif name == HOOK_REGISTERED and attrs.get("plugin.name"):
        events.registrations.append(
            Registration(
                str(attrs["plugin.name"]),
                _str(attrs.get("plugin_id_hash")),
                str(attrs.get("hook_event") or "?"),
                session,
            )
        )
    elif name == HOOK_EXECUTION:
        row = events.session_executions[(session, str(attrs.get("hook_event") or "?"))]
        row[0] += 1
        row[1] += tr.as_number(attrs.get("total_duration_ms")) or 0.0
        row[2] += tr.as_number(attrs.get("additional_context_chars")) or 0.0
    elif name == SKILL_ACTIVATED and attrs.get("plugin.name"):
        events.skill_uses.append(
            SkillUse(
                str(attrs["plugin.name"]),
                _str(attrs.get("plugin_id_hash")),
                str(attrs.get("skill.name") or "?"),
                session,
            )
        )
    elif name == tr.API_REQUEST and session is not None:
        events.requested.add(session)
        usage = events.usage[session]
        usage[0] += sum(tr.as_number(attrs.get(key)) or 0.0 for key in tr.API_TOKEN_KEYS)
        usage[1] += tr.as_cost(attrs.get("cost_usd")) or 0.0


# --- local plugin facts -----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class InstalledPlugin:
    """One entry of `installed_plugins.json`."""

    name: str
    version: str | None
    scope: str | None
    install_path: Path | None
    installed_at: str | None
    updated_at: str | None


@dataclasses.dataclass
class LocalPlugins:
    """What this machine knows about its plugins, read from local files only."""

    registry_found: bool = False
    installed: list[InstalledPlugin] = dataclasses.field(default_factory=list)
    enabled: set[str] = dataclasses.field(default_factory=set)
    # (hook event, matcher) of each hook the user and project settings files declare
    settings_hooks: list[tuple[str, str]] = dataclasses.field(default_factory=list)


def claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):  # nested past the recursion limit
        return None


def _plugin_name(plugin_id: str) -> str:
    return plugin_id.split("@", 1)[0]


def _has_hook(block: object) -> bool:
    """Whether a hooks block holds at least one hook object, of any type (command, prompt, agent, http)."""
    inner = block.get("hooks") if isinstance(block, dict) else None
    return isinstance(inner, list) and any(isinstance(h, dict) for h in inner)


def _settings_hooks(data: object) -> list[tuple[str, str]]:
    """(hook event, matcher) of each block of a settings file's `hooks` that has a hook of any type."""
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return [
        (str(event), str(block.get("matcher") or ""))
        for event, blocks in (hooks.items() if isinstance(hooks, dict) else [])
        for block in (blocks if isinstance(blocks, list) else [])
        if _has_hook(block)
    ]


def load_local(claude_dir: Path, project_root: Path | None) -> LocalPlugins:
    """The plugin registry, and the plugins enabled in the user and project
    settings (a later file overrides an earlier one); no `project_root` reads
    no project settings."""
    local = LocalPlugins()
    registry = _read_json(claude_dir / "plugins" / "installed_plugins.json")
    if isinstance(registry, dict) and isinstance(registry.get("plugins"), dict):
        local.registry_found = True
        for plugin_id, entries in registry["plugins"].items():
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("installPath")
                local.installed.append(
                    InstalledPlugin(
                        name=_plugin_name(str(plugin_id)),
                        version=_str(entry.get("version")),
                        scope=_str(entry.get("scope")),
                        install_path=Path(path) if isinstance(path, str) else None,
                        installed_at=_str(entry.get("installedAt")),
                        updated_at=_str(entry.get("lastUpdated")),
                    )
                )
    enabled: dict[str, bool] = {}
    for settings in (
        claude_dir / "settings.json",
        *(
            (project_root / ".claude" / "settings.json", project_root / ".claude" / "settings.local.json")
            if project_root is not None
            else ()
        ),
    ):
        data = _read_json(settings)
        plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
        if isinstance(plugins, dict):
            enabled.update({_plugin_name(str(key)): value is True for key, value in plugins.items()})
        local.settings_hooks.extend(_settings_hooks(data))
    local.enabled = {name for name, on in enabled.items() if on}
    return local


# --- aggregation -------------------------------------------------------------------


@dataclasses.dataclass
class VersionStats:
    sessions: set[str] = dataclasses.field(default_factory=set)
    first: str | None = None
    last: str | None = None


@dataclasses.dataclass
class PluginStats:
    marketplace: str
    scope: str
    sessions: set[str] = dataclasses.field(default_factory=set)
    first: str | None = None
    last: str | None = None
    versions: dict[str, VersionStats] = dataclasses.field(default_factory=dict)

    @property
    def real_versions(self) -> list[str]:
        return sorted(version for version in self.versions if version != UNVERSIONED)


@dataclasses.dataclass
class Names:
    """Redacted third-party plugins that could be named from local settings,
    by `plugin_id_hash`, and how many groups could not be."""

    by_hash: dict[str, str] = dataclasses.field(default_factory=dict)
    groups: int = 0

    @property
    def unnamed(self) -> int:
        return self.groups - len(self.by_hash)

    def label(self, name: str, plugin_hash: str | None) -> str:
        """The plugin's display name: a third-party plugin is told apart by its hash."""
        if name != THIRD_PARTY:
            return name
        if plugin_hash:
            return self.by_hash.get(plugin_hash) or f"{THIRD_PARTY} #{plugin_hash[:HASH_PREFIX]}"
        if self.groups == 1 and len(self.by_hash) == 1:
            return next(iter(self.by_hash.values()))
        return THIRD_PARTY


def recover_names(events: PluginEvents, local: LocalPlugins) -> Names:
    """Name a redacted group only when one group and one candidate remain: an
    enabled, installed plugin that no named `plugin_loaded` event accounts for."""
    groups = {load.hash for load in events.loads if load.name == THIRD_PARTY and load.hash}
    named = {load.name for load in events.loads if load.name != THIRD_PARTY}
    installed = {plugin.name for plugin in local.installed}
    candidates = (local.enabled & installed) - named
    names = Names(groups=len(groups))
    if len(groups) == 1 and len(candidates) == 1:
        names.by_hash[next(iter(groups))] = next(iter(candidates))
    return names


@dataclasses.dataclass
class SessionCost:
    sessions: int = 0
    tokens: float = 0.0
    cost: float = 0.0

    def mean(self) -> tuple[float, float] | None:
        if self.sessions < MIN_GROUP:
            return None
        return self.tokens / self.sessions, self.cost / self.sessions


@dataclasses.dataclass
class PluginSummary:
    plugins: dict[str, PluginStats]
    loaded_sessions: set[str]
    without_events: int
    names: Names
    # plugin -> (with, without) session cost groups
    cost: dict[str, tuple[SessionCost, SessionCost]]
    # plugin -> registrations, sessions, hook events
    registrations: dict[str, tuple[int, set[str], set[str]]]
    # plugin -> activations, skills, sessions
    skills: dict[str, tuple[int, set[str], set[str]]]


def _seen(stats: PluginStats | VersionStats, timestamp: str | None) -> None:
    if timestamp:
        stats.first = timestamp if stats.first is None else min(stats.first, timestamp)
        stats.last = timestamp if stats.last is None else max(stats.last, timestamp)


def summarize(events: PluginEvents, names: Names) -> PluginSummary:
    plugins: dict[str, PluginStats] = {}
    loaded_sessions: set[str] = set()
    plugin_sessions: dict[str, set[str]] = defaultdict(set)
    for load in events.loads:
        label = names.label(load.name, load.hash)
        stats = plugins.setdefault(label, PluginStats(load.marketplace, load.scope))
        version = stats.versions.setdefault(load.version or UNVERSIONED, VersionStats())
        if load.session is not None:
            stats.sessions.add(load.session)
            version.sessions.add(load.session)
            loaded_sessions.add(load.session)
            plugin_sessions[label].add(load.session)
        _seen(stats, load.timestamp)
        _seen(version, load.timestamp)

    cost: dict[str, tuple[SessionCost, SessionCost]] = {}
    for label, sessions in plugin_sessions.items():
        with_group, without_group = SessionCost(), SessionCost()
        for session in loaded_sessions:
            tokens, usd = events.usage.get(session, (0.0, 0.0))
            group = with_group if session in sessions else without_group
            group.sessions += 1
            group.tokens += tokens
            group.cost += usd
        cost[label] = (with_group, without_group)

    registrations: dict[str, tuple[int, set[str], set[str]]] = {}
    for item in events.registrations:
        count, sessions, hooks = registrations.get(names.label(item.plugin, item.hash), (0, set(), set()))
        registrations[names.label(item.plugin, item.hash)] = (
            count + 1,
            sessions | ({item.session} if item.session else set()),
            hooks | {item.hook_event},
        )
    skills: dict[str, tuple[int, set[str], set[str]]] = {}
    for use in events.skill_uses:
        count, used, sessions = skills.get(names.label(use.plugin, use.hash), (0, set(), set()))
        skills[names.label(use.plugin, use.hash)] = (
            count + 1,
            used | {use.skill},
            sessions | ({use.session} if use.session else set()),
        )
    return PluginSummary(
        plugins, loaded_sessions, len(events.sessions - loaded_sessions), names, cost, registrations, skills
    )


# --- per-plugin hook overhead from local transcripts --------------------------------


@dataclasses.dataclass
class HookOverhead:
    # plugin label (or NOT_A_PLUGIN / AMBIGUOUS) -> [runs, duration_ms, stdout chars]
    rows: dict[str, list[float]] = dataclasses.field(default_factory=lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
    transcripts: int = 0
    unreadable: int = 0


def declared_commands(local: LocalPlugins) -> tuple[dict[tuple[str, str], set[str]], int]:
    """(hook event, command) -> names of the plugins declaring it in their
    hooks/hooks.json, and how many installed plugins' hooks.json could not be read."""
    declared: dict[tuple[str, str], set[str]] = defaultdict(set)
    unreadable = 0
    for plugin in local.installed:
        if plugin.install_path is None:
            continue
        path = plugin.install_path / "hooks" / "hooks.json"
        if not path.is_file():
            continue
        data = _read_json(path)
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            unreadable += 1
            continue
        for event, matchers in hooks.items():
            for matcher in matchers if isinstance(matchers, list) else []:
                for hook in matcher.get("hooks") if _has_hook(matcher) else []:
                    if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                        declared[(str(event), hook["command"])].add(plugin.name)
    return declared, unreadable


def declared_matchers(local: LocalPlugins, only: set[str] | None = None) -> dict[str, list[tuple[str, str]]]:
    """hook event -> (matcher, plugin name) for each plugin hooks.json block that
    declares a hook of any type for it, among the plugins named in `only` (all installed
    ones when None). An empty matcher matches everything."""
    declared: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for plugin in local.installed:
        if plugin.install_path is None or (only is not None and plugin.name not in only):
            continue
        data = _read_json(plugin.install_path / "hooks" / "hooks.json")
        hooks = data.get("hooks") if isinstance(data, dict) else None
        for event, blocks in hooks.items() if isinstance(hooks, dict) else []:
            for block in blocks if isinstance(blocks, list) else []:
                if _has_hook(block):
                    declared[str(event)].append((str(block.get("matcher") or ""), plugin.name))
    return declared


def _matches(matcher: str, tool: str | None) -> bool:
    if tool is None or matcher in ("", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return matcher == tool


def declarers(declared: Mapping[str, list[tuple[str, str]]], event: str, tool: str | None) -> list[str]:
    """The plugins whose hooks.json declares a hook that this event (and tool) runs."""
    return sorted({name for matcher, name in declared.get(event, []) if _matches(matcher, tool)})


def _hook_successes(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if HOOK_SUCCESS not in line:  # cheap test before parsing a large transcript
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):  # ValueError: an integer past the digit limit
                    continue
                attachment = record.get("attachment") if isinstance(record, dict) else None
                if isinstance(attachment, dict) and attachment.get("type") == HOOK_SUCCESS:
                    yield attachment
    except OSError:
        return


def hook_overhead(sessions: set[str], dirs: list[Path], local: LocalPlugins) -> HookOverhead:
    """Attribute each `hook_success` attachment of `sessions`' transcripts to the
    one plugin whose hooks.json declares its command for that hook event."""
    overhead = HookOverhead()
    declared, overhead.unreadable = declared_commands(local)
    for directory in dirs:
        for context in ti.discover_transcripts(directory):
            if context.session_id not in sessions:
                continue
            overhead.transcripts += 1
            for attachment in _hook_successes(context.transcript_path):
                owners = declared.get((str(attachment.get("hookEvent")), str(attachment.get("command"))), set())
                label = next(iter(owners)) if len(owners) == 1 else AMBIGUOUS if owners else NOT_A_PLUGIN
                row = overhead.rows[label]
                row[0] += 1
                row[1] += tr.as_number(attachment.get("durationMs")) or 0.0
                row[2] += len(str(attachment.get("stdout") or ""))
    return overhead


# --- formatting --------------------------------------------------------------------


def _cut(name: str, width: int = NAME_WIDTH) -> str:
    name = printable_line(name)  # the one rule of `safe_name`: controls, bidi, zero-width, private use, odd spaces
    return name if len(name) <= width else name[: width - 1] + "…"


def _day(timestamp: str | None) -> str:
    return timestamp[:10] if timestamp else "?"


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _inventory_table(summary: PluginSummary) -> list[str]:
    header = (
        f"{'plugin':<{NAME_WIDTH}} {'marketplace':<26} {'scope':<15} {'sessions':>8} {'share':>6}"
        f" {'first_seen':<10} {'last_seen':<10} versions"
    )
    lines = ["Plugins loaded (one plugin_loaded event per enabled plugin per session)", header, "-" * len(header)]
    total = len(summary.loaded_sessions)
    rows = sorted(summary.plugins.items(), key=lambda item: (-len(item[1].sessions), item[0]))
    for name, stats in rows:
        share = f"{round(100 * len(stats.sessions) / total)}%" if total else "-"
        versions = ",".join(stats.real_versions) or "-"
        lines.append(
            f"{_cut(name):<{NAME_WIDTH}} {stats.marketplace:<26} {stats.scope:<15} {len(stats.sessions):>8,}"
            f" {share:>6} {_day(stats.first):<10} {_day(stats.last):<10} {versions}"
        )
    return lines


def _idle_table(idle: IdleSessions) -> list[str]:
    """The one row for sessions that never reached a model: kept out of every other count and total."""
    if not idle.sessions:
        return []
    header = (
        f"{'sessions':>8} {'plugin_loaded':>13} {'hook_runs':>9} {'hook_ms':>9} {'hook_tokens':>11}"
        f" {'chain_runs':>10} {'chain_ms':>9}"
    )
    return [
        "Sessions with no model request (hooks ran, nothing reached a model; not counted in any block here)",
        header,
        "-" * len(header),
        (
            f"{idle.sessions:>8,} {idle.loaded:>13,} {idle.runs:>9,.0f} {idle.ms:>9,.0f}"
            f" {idle.chars / CHARS_PER_TOKEN:>11,.0f} {idle.chain_runs:>10,.0f} {idle.chain_ms:>9,.0f}"
        ),
        (
            "No claude_code.api_request event and no native step record: typically claude -p /usage."
            " hook_tokens = additional_context_chars / 4 of their hook_execution_complete events;"
            " chain_runs and chain_ms are their usage-observer hook_runs records (settings-hook chain wall time)."
        ),
    ]


def _drift_table(summary: PluginSummary) -> list[str]:
    drifting = {
        name: stats for name, stats in summary.plugins.items() if stats.real_versions and len(stats.versions) > 1
    }
    if not drifting:
        return []
    header = f"{'plugin':<{NAME_WIDTH}} {'version':<14} {'sessions':>8} {'first_seen':<10} {'last_seen':<10}"
    lines = [
        "Version drift (plugin.version is absent on many events, shown as (unversioned))",
        header,
        "-" * len(header),
    ]
    for name, stats in sorted(drifting.items()):
        for version, info in sorted(stats.versions.items(), key=lambda item: (item[1].first or "", item[0])):
            lines.append(
                f"{_cut(name):<{NAME_WIDTH}} {version:<14} {len(info.sessions):>8,} {_day(info.first):<10}"
                f" {_day(info.last):<10}"
            )
    return lines


def _installs_table(events: PluginEvents, local: LocalPlugins) -> list[str]:
    note = "machine-wide: not filtered by project"
    if events.installs:
        header = f"{'plugin':<{NAME_WIDTH}} {'version':<14} {'installed':<16}"
        lines = [f"Installs (plugin_installed events; {note})", header, "-" * len(header)]
        lines.extend(
            f"{_cut(install.name):<{NAME_WIDTH}} {install.version or '-':<14} {_when(install.timestamp):<16}"
            for install in sorted(events.installs, key=lambda item: (item.timestamp or "", item.name))
        )
        return lines
    if not local.registry_found:
        return ["Installs: no plugin_installed events and installed_plugins.json not found"]
    header = f"{'plugin':<{NAME_WIDTH}} {'version':<14} {'scope':<8} {'installed':<16} {'updated':<16}"
    lines = [f"Installs (installed_plugins.json; {note})", header, "-" * len(header)]
    for plugin in sorted(local.installed, key=lambda item: (item.installed_at or "", item.name)):
        lines.append(
            f"{_cut(plugin.name):<{NAME_WIDTH}} {plugin.version or '-':<14} {plugin.scope or '-':<8}"
            f" {_when(plugin.installed_at):<16} {_when(plugin.updated_at):<16}"
        )
    return lines


def _cost_table(summary: PluginSummary) -> list[str]:
    header = (
        f"{'plugin':<{NAME_WIDTH}} {'with':>5} {'without':>8} {'tok_with':>12} {'tok_without':>12}"
        f" {'usd_with':>9} {'usd_without':>12}"
    )
    lines = ["Session cost with and without each plugin (mean per session)", header, "-" * len(header)]

    def cell(means: tuple[float, float] | None, index: int, spec: str, width: int) -> str:
        return f"{means[index]:{spec}}".rjust(width) if means else "-".rjust(width)

    rows = sorted(summary.cost.items(), key=lambda item: (-item[1][0].sessions, item[0]))
    for name, (with_group, without_group) in rows:
        a, b = with_group.mean(), without_group.mean()
        if a is None or b is None:
            a = b = None
        lines.append(
            f"{_cut(name):<{NAME_WIDTH}} {with_group.sessions:>5,} {without_group.sessions:>8,}"
            f" {cell(a, 0, ',.0f', 12)} {cell(b, 0, ',.0f', 12)} {cell(a, 1, ',.2f', 9)} {cell(b, 1, ',.2f', 12)}"
        )
    lines.append(
        f"A correlation, not the plugin's own cost: sessions differ in length and task. A group needs {MIN_GROUP}"
        " sessions or the means show -."
    )
    lines.append("tokens = input + output + cacheRead + cacheCreation of claude_code.api_request.")
    return lines


def _registrations_table(summary: PluginSummary) -> list[str]:
    if not summary.registrations:
        return []
    header = f"{'plugin':<{NAME_WIDTH}} {'registrations':>13} {'sessions':>8} hook_events"
    lines = ["Hook registrations per plugin (hook_registered events)", header, "-" * len(header)]
    for name, (count, sessions, hooks) in sorted(
        summary.registrations.items(), key=lambda item: (-item[1][0], item[0])
    ):
        lines.append(f"{_cut(name):<{NAME_WIDTH}} {count:>13,} {len(sessions):>8,} {','.join(sorted(hooks))}")
    return lines


def _merged_table(events: PluginEvents) -> list[str]:
    if not events.executions:
        return []
    header = f"{'hook_event':<{NAME_WIDTH}} {'runs':>7} {'ms':>10} {'chars':>10} {'tokens':>8}"
    lines = [
        "Hook activity (merged across plugins and user hooks: hook_source is merged)",
        header,
        "-" * len(header),
    ]
    for event, (runs, ms, chars) in sorted(events.executions.items(), key=lambda item: (-item[1][2], item[0])):
        lines.append(
            f"{_cut(event):<{NAME_WIDTH}} {runs:>7,.0f} {ms:>10,.0f} {chars:>10,.0f} {chars / CHARS_PER_TOKEN:>8,.0f}"
        )
    lines.append(
        "chars = additional_context_chars, tokens = chars / 4. The capture has no per-plugin hook metrics"
        " (no hook_plugin_metrics event); see the per-plugin block."
    )
    return lines


def _overhead_table(overhead: HookOverhead | None, reason: str | None) -> list[str]:
    title = "Hook overhead per plugin (local transcripts)"
    if overhead is None or not overhead.rows:
        return [f"{title}: {reason or 'no hook_success attachments found'}"]
    header = f"{'plugin':<{NAME_WIDTH}} {'runs':>7} {'ms':>10} {'chars':>10} {'tokens':>8}"
    lines = [f"{title}; tokens = chars / 4", header, "-" * len(header)]
    for label, (runs, ms, chars) in sorted(overhead.rows.items(), key=lambda item: (-item[1][2], item[0])):
        lines.append(
            f"{_cut(label):<{NAME_WIDTH}} {runs:>7,.0f} {ms:>10,.0f} {chars:>10,.0f} {chars / CHARS_PER_TOKEN:>8,.0f}"
        )
    lines.append(
        "Transcripts record only hooks that produced output: silent hooks are not here, only in the merged rows."
    )
    lines.append("chars = the hook's stdout, which includes its own JSON wrapper.")
    return lines


def _enabled_table(events: PluginEvents) -> list[str]:
    if not events.enabled:
        return []
    counts: dict[str, int] = defaultdict(int)
    for ids in events.enabled.values():
        for plugin_id in ids:
            counts[plugin_id] += 1
    total = len(events.enabled)
    header = f"{'plugin':<{NAME_WIDTH}} {'sessions':>8} {'share':>6}"
    lines = [
        "Plugins enabled per session (usage-observer plugin_set, sessions with a model request, claude -p included)",
        header,
        "-" * len(header),
    ]
    for plugin_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"{_cut(plugin_id):<{NAME_WIDTH}} {count:>8,} {round(100 * count / total):>5}%")
    lines.append(
        "From the merged settings' enabledPlugins: a plugin loaded with --plugin-dir is not listed (see the modules block)."
    )
    return lines


def _modules_table(events: PluginEvents) -> list[str]:
    if not events.modules:
        return []
    rows: dict[tuple[str, str | None, str], tuple[set[str], tuple[str, ...]]] = {}
    for module in events.modules:
        key = (module.plugin, module.version, module.provenance)
        sessions, listed = rows.get(key, (set(), ()))
        rows[key] = (sessions | ({module.session} if module.session else set()), listed or module.events)
    header = f"{'plugin':<{NAME_WIDTH}} {'version':<10} {'provenance':<28} {'sessions':>8} events"
    lines = ["Hooks modules joined (usage-observer plugin_register)", header, "-" * len(header)]
    for (name, version, provenance), (sessions, listed) in sorted(
        rows.items(), key=lambda item: (-len(item[1][0]), item[0])
    ):
        lines.append(
            f"{_cut(name):<{NAME_WIDTH}} {version or '-':<10} {_cut(provenance):<28} {len(sessions):>8,} {','.join(listed)}"
        )
    lines.append("Only plugins with a hooks module are recorded, and only those loaded after usage-observer.")
    return lines


@dataclasses.dataclass
class NativeRow:
    """One (hook event, tool) row of the hook_runs records, merged across sessions."""

    event: str
    tool: str | None
    stats: HookRunStats
    owners: set[str] = dataclasses.field(default_factory=set)  # plugins declaring it, in the sessions that ran it
    settings: bool = False  # a user or project settings hook runs in the same chain
    # bucket label -> [runs, ms], each session's time credited from its own plugin set
    credit: dict[str, list[float]] = dataclasses.field(default_factory=lambda: defaultdict(lambda: [0.0, 0.0]))


def _bucket(owners: list[str], settings: bool) -> str:
    if settings:
        return WITH_SETTINGS if owners else SETTINGS_ONLY
    return owners[0] if len(owners) == 1 else SHARED if owners else NO_DECLARER


def _session_plugins(events: PluginEvents, session: str | None) -> set[str]:
    """The plugin names a session ran with: its plugin_set, plus the hooks modules it joined.
    A session with neither, or a record with no session, has no plugin credited."""
    if session is None:
        return set()
    names = {_plugin_name(i) for i in events.enabled.get(session, ())}
    return names | {m.plugin for m in events.modules if m.session == session}


def _native_rows(events: PluginEvents, local: LocalPlugins) -> list[NativeRow]:
    """Hook event rows worth showing: some plugin or settings hook declares them, or they took time.
    A session's time is credited only to plugins enabled in that session."""
    cache: dict[frozenset[str], dict[str, list[tuple[str, str]]]] = {}
    merged: dict[tuple[str, str | None], NativeRow] = {}
    for (session, event, tool), stats in events.native_runs.items():
        names = frozenset(_session_plugins(events, session))
        if names not in cache:
            cache[names] = declared_matchers(local, set(names))
        owners = declarers(cache[names], event, tool)
        settings = any(e == event and _matches(m, tool) for e, m in local.settings_hooks)
        row = merged.setdefault((event, tool), NativeRow(event, tool, HookRunStats()))
        row.stats.runs += stats.runs
        row.stats.ms += stats.ms
        row.stats.max_ms = max(row.stats.max_ms, stats.max_ms)
        row.stats.sessions |= stats.sessions
        row.owners.update(owners)
        row.settings = settings
        credit = row.credit[_bucket(owners, settings)]
        credit[0] += stats.runs
        credit[1] += stats.ms
    rows = [r for r in merged.values() if r.owners or r.settings or r.stats.ms >= r.stats.runs * 2]
    return sorted(rows, key=lambda row: (-row.stats.ms, row.event, row.tool or ""))


def _native_hooks_table(rows: list[NativeRow], events: PluginEvents) -> list[str]:
    if not events.native_runs:
        return []
    if not rows:
        return ["Hook runs (usage-observer): no event took time or has a plugin hook declared"]
    header = f"{'hook_event':<20} {'tool':<14} {'runs':>7} {'ms':>9} {'mean':>7} {'max':>7}  declared by"
    lines = [
        "Hook runs (usage-observer hook_runs: wall time of every settings hook chain, silent hooks included)",
        header,
        "-" * len(header),
    ]
    for row in rows[:40]:
        stats = row.stats
        names = ",".join(sorted(row.owners))
        if row.settings and names:
            # cut the plugin part only, so the marker that says a settings hook shares the chain stays whole
            declared_by = _cut(names, NAME_WIDTH - len(WITH_MARKER)) + WITH_MARKER
        else:
            declared_by = _cut(SETTINGS_ONLY if row.settings else names or NO_DECLARER)
        lines.append(
            f"{row.event:<20} {_cut(row.tool or '-')[:14]:<14} {stats.runs:>7,.0f} {stats.ms:>9,.0f}"
            f" {stats.ms / stats.runs if stats.runs else 0.0:>7.1f} {stats.max_ms:>7,.0f}  {declared_by}"
        )
    if len(rows) > 40:
        lines.append(f"... {len(rows) - 40} more rows")
    lines.append(
        "ms = the engine's time to run the chain for the event: every plugin's hook and the user's together, in"
        " parallel; the engine fires each event with none configured (about 1 ms, left out). declared by = plugins"
        " enabled in the sessions that ran it whose hooks.json (at the install record's installPath, a cached copy"
        " for a local-checkout install) declares a hook for the event and tool; settings hooks are those in the"
        " current user and project settings files."
    )
    return lines


def _native_per_plugin_table(rows: list[NativeRow]) -> list[str]:
    if not rows:
        return []
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        for label, (runs, ms) in row.credit.items():
            totals[label][0] += runs
            totals[label][1] += ms
    header = f"{'plugin':<{NAME_WIDTH}} {'runs':>7} {'ms':>10}"
    lines = ["Hook time per plugin (usage-observer hook_runs)", header, "-" * len(header)]
    for label, (runs, ms) in sorted(totals.items(), key=lambda item: (-item[1][1], item[0])):
        lines.append(f"{_cut(label):<{NAME_WIDTH}} {runs:>7,.0f} {ms:>10,.0f}")
    lines.append(
        f"Each session's time goes to the plugin enabled in that session that is the one declaring a hook for the"
        f" event; {SHARED} could not be split; {WITH_SETTINGS} ran in a chain with a settings hook, so its time"
        f" is not the plugins' alone; {SETTINGS_ONLY} and {NO_DECLARER} (also a session with no plugin_set)"
        " are the user's own hooks or the engine's."
    )
    return lines


def _skills_table(summary: PluginSummary) -> list[str]:
    if not summary.skills:
        return []
    header = f"{'plugin':<{NAME_WIDTH}} {'acts':>6} {'skills':>7} {'sessions':>8}"
    lines = ["Skill activations by plugin (skill_activated events with plugin.name)", header, "-" * len(header)]
    for name, (count, used, sessions) in sorted(summary.skills.items(), key=lambda item: (-item[1][0], item[0])):
        lines.append(f"{_cut(name):<{NAME_WIDTH}} {count:>6,} {len(used):>7,} {len(sessions):>8,}")
    lines.append("Per-skill detail and load overhead: report --skills.")
    return lines


def _footer(summary: PluginSummary, hooks_unreadable: int, from_mirror: bool) -> list[str]:
    lines = [
        (
            f"shares are over {_plural(len(summary.loaded_sessions), 'session')} with plugin_loaded events and a model request;"
            f" {_plural(summary.without_events, 'kept session')} had no plugin_loaded event"
        )
    ]
    if from_mirror:
        lines.append(MIRROR_COVERAGE)
    names = summary.names
    if names.by_hash:
        lines.append(
            f"{_plural(len(names.by_hash), 'third-party plugin')} named from local settings (current settings,"
            " not as they were when the session ran)"
        )
    if names.unnamed:
        lines.append(f"{_plural(names.unnamed, 'third-party plugin')} could not be named")
    if hooks_unreadable:
        lines.append(f"{_plural(hooks_unreadable, 'installed plugin')}'s hooks.json could not be read")
    return lines


def format_plugin_report(
    events: PluginEvents,
    local: LocalPlugins,
    overhead: HookOverhead | None = None,
    overhead_reason: str | None = None,
) -> str:
    # An installed plugin that no session enabled ran none of its hooks.
    rows = _native_rows(events, local)
    native = [
        _enabled_table(events),
        _modules_table(events),
        _native_hooks_table(rows, events),
        _native_per_plugin_table(rows),
    ]
    idle = _idle_table(events.idle)
    if not events.loads:
        if any(native):
            note = ["No plugin_loaded events: the OTLP capture (--source logs/agent-telemetry.jsonl) has them."]
            return "\n\n".join("\n".join(block) for block in [*native, note, idle] if block)
        head = IDLE_ONLY_MESSAGE if events.idle.loaded else NEEDS_OTLP if events.native_lines else EMPTY_MESSAGE
        return "\n\n".join([head, "\n".join(idle)]) if idle else head
    summary = summarize(events, recover_names(events, local))
    blocks = [
        _inventory_table(summary),
        idle,
        _drift_table(summary),
        _installs_table(events, local),
        _cost_table(summary),
        _registrations_table(summary),
        _merged_table(events),
        _overhead_table(overhead, overhead_reason),
        *native,
        _skills_table(summary),
        _footer(summary, declared_commands(local)[1], bool(events.native_lines)),
    ]
    return "\n\n".join("\n".join(block) for block in blocks if block)


# --- entry point -------------------------------------------------------------------


def build_report(
    capture: Path,
    project_filter: ProjectFilter | None,
    project_dir: Path | None,
    local: LocalPlugins | None = None,
) -> str:
    """The whole `--plugins` view: load, recover names, read transcripts, format."""
    events = load_events(capture, project_filter)
    if local is None:
        # The label's project root, not the cwd (a subdirectory or a worktree still finds
        # its project's settings); the cwd only when nothing filters by project.
        local = load_local(claude_config_dir(), project_filter.root if project_filter else Path.cwd())
    overhead, reason = None, None
    if events.loads:
        dirs = [directory for directory in tr.transcript_dirs(project_dir) if directory.is_dir()]
        if dirs:
            overhead = hook_overhead({load.session for load in events.loads if load.session}, dirs, local)
        else:
            reason = "no transcripts found for this project"
    return format_plugin_report(events, local, overhead, reason)
