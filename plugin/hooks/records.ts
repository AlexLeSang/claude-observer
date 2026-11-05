// Pure record builders for hooks/observer.ts, kept apart so tests can call
// them without an engine.

import type { TelemetryRecordEntry } from 'claude-code'

export type ObserverRecord = Record<string, unknown>

// The receiver's drop list (scripts/agent_telemetry.py IDENTITY_ATTRIBUTES):
// dropped here so they never leave the process, and again by the appender.
export const IDENTITY_ATTRIBUTES: readonly string[] = [
  'user.email',
  'user.id',
  'user.account_uuid',
  'user.account_id',
  'organization.id',
  'vcs.owner.name',
  'vcs.repository.name',
  'vcs.repository.url.full',
]

// OTel event attributes that hold what the user or the model wrote or what a
// tool took and returned. Claude Code adds them when OTEL_LOG_USER_PROMPTS,
// OTEL_LOG_TOOL_DETAILS or OTEL_LOG_TOOL_CONTENT is set; the mirror drops them
// whatever the environment says, so only names, sizes and counts reach the capture.
export const CONTENT_ATTRIBUTES: readonly string[] = [
  'prompt',
  'prompt_text',
  'response',
  'tool_parameters',
  'tool_input',
  'tool_output',
  'tool_result_content',
]

type Usage = {
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
  model: string
}

type StepInput = {
  turnId: string
  index: number
  model: string
  effort?: string | number
  messageCount: number
  agentId?: string
}

type StepResult = {
  stopReason: string | null
  usage: Usage | null
  toolUses: readonly { tool?: string; name?: string }[]
}

type ToolResult = {
  deny?: string
  isError?: boolean
  text?: string
}

export function agentFields(agentId: string | undefined, agentType: string | undefined) {
  return { agent: agentId ?? null, agent_type: agentType ?? null }
}

export function stepRecord(e: StepInput, result: StepResult, thinkingChars: number, ms: number) {
  return {
    turn: e.turnId,
    index: e.index,
    agent: e.agentId ?? null,
    model: result.usage?.model ?? e.model,
    effort: e.effort ?? null,
    messages: e.messageCount,
    stop: result.stopReason,
    tools: result.toolUses.map(t => t.tool ?? t.name ?? '?'),
    usage: result.usage,
    thinking_chars: thinkingChars,
    ms,
  }
}

export function mcpServer(tool: string) {
  const match = /^mcp__(.+?)__/.exec(tool)
  return match ? match[1] : null
}

// The views scripts/reflect_report.py accepts (`VIEWS` there): the only argument of a runner call
// that is ever recorded. A test keeps the two lists equal.
export const REPORT_VIEWS: readonly string[] = [
  'sessions',
  'subagents',
  'models',
  'tickets',
  'context',
  'fixed-context',
  'skills',
  'plugins',
  'digest',
  'prompts',
  'personas', // the old name of subagents, still accepted
  'friction',
]

const PLUGIN_FILE_MAX = 128
const PLUGIN_FILE_CHARS = /^[A-Za-z0-9._/-]+$/
// Runner options whose next word is their value, not the view.
const VALUE_OPTIONS: readonly string[] = ['--limit', '--capture']

// The plugin root as the engine reports it, or null when matching on it would be unsafe: an empty
// root would make `/skills/` and `/scripts/` match anywhere, and a backslash means another form.
function usableRoot(root: string | undefined): string | null {
  if (typeof root !== 'string' || root.length === 0 || root.includes('\\')) return null
  return root
}

// A Read of a file under <root>/skills/: its path relative to the root, else null. Names only a
// path of plain characters with no `.`, `..` or empty segment, so nothing outside the plugin's
// skills directory and nothing a user typed beyond a file name can reach the capture.
export function pluginFileName(path: unknown, root: string | undefined): string | null {
  const base = usableRoot(root)
  if (base === null || typeof path !== 'string') return null
  const prefix = `${base}/skills/`
  if (!path.startsWith(prefix)) return null
  const relative = path.slice(base.length + 1)
  if (relative.length > PLUGIN_FILE_MAX || !PLUGIN_FILE_CHARS.test(relative)) return null
  if (relative.split('/').some(part => part === '' || part === '.' || part === '..')) return null
  return relative
}

const RUNNER_BEFORE = /^[\s"';&|(]$/
const RUNNER_AFTER = /^[\s"';&|)]$/

// A Bash command that runs <root>/scripts/reflect_report.py: the allowed view it names, else null.
// The first word after the runner, up to ; & | or a newline, that is not an option or the value of
// --limit or --capture; recorded only when it is one of REPORT_VIEWS. Nothing else of the command.
export function reportViewName(command: unknown, root: string | undefined): string | null {
  const base = usableRoot(root)
  if (base === null || typeof command !== 'string') return null
  const runner = `${base}/scripts/reflect_report.py`
  let from = 0
  for (;;) {
    const at = command.indexOf(runner, from)
    if (at < 0) return null
    from = at + 1
    const before = at === 0 ? ' ' : command[at - 1]
    const end = at + runner.length
    const after = end === command.length ? ' ' : command[end]
    // The runner as a whole word: not the tail of a longer path, not the head of a longer name.
    if (!RUNNER_BEFORE.test(before) || !RUNNER_AFTER.test(after)) continue
    const quoted = (before === '"' || before === "'") && after === before
    const rest = command.slice(quoted ? end + 1 : end)
    const stop = rest.search(/[;&|\n\r]/)
    const words = (stop < 0 ? rest : rest.slice(0, stop)).split(/\s+/).filter(w => w !== '')
    for (let i = 0; i < words.length; i++) {
      if (VALUE_OPTIONS.includes(words[i])) i++
      else if (!words[i].startsWith('-')) return REPORT_VIEWS.includes(words[i]) ? words[i] : null
    }
    return null
  }
}

export function toolRecord(e: Readonly<Record<string, unknown>>, result: ToolResult, ms: number, pluginRoot?: string) {
  const input = Object.fromEntries(
    Object.entries(e).filter(([key]) => !['tool', 'tool_use_id', 'agentId', 'consent'].includes(key)),
  )
  return {
    tool: String(e.tool),
    tool_use_id: e.tool_use_id ?? null,
    agent: e.agentId ?? null,
    ms,
    error: result.isError === true,
    denied: result.deny !== undefined,
    input_chars: JSON.stringify(input).length,
    result_chars: (result.text ?? result.deny ?? '').length,
    // Names, never content: which skill or subagent type a call asked for, read
    // only from the tools that take one (an MCP tool's argument may share the name).
    // Cleaned and capped: a name is written by the skill's author.
    skill: e.tool === 'Skill' ? cleanName(e.skill) : null,
    subagent_type: e.tool === 'Agent' || e.tool === 'Task' ? cleanName(e.subagent_type) : null,
    // Plugin names: a skill file of this plugin that was read, and the report view the
    // plugin's runner was asked for. The tools' other arguments never leave the process.
    plugin_file:
      e.tool === 'Read' && result.isError !== true && result.deny === undefined
        ? pluginFileName(e.file_path, pluginRoot)
        : null,
    report_view:
      e.tool === 'Bash' && result.isError !== true && result.deny === undefined
        ? reportViewName(e.command, pluginRoot)
        : null,
  }
}

// A collector attribute as the kit declares it (TelemetryAttribute): a string, a
// number, a boolean or a list of strings. Anything else (a nested object a later
// engine might send) is left out, so no content key can ride inside it.
function flatAttribute(value: unknown): boolean {
  return (
    typeof value === 'string' ||
    typeof value === 'number' ||
    typeof value === 'boolean' ||
    (Array.isArray(value) && value.every(item => typeof item === 'string'))
  )
}

export function otelRecord(e: TelemetryRecordEntry, identity: readonly string[]) {
  const attributes = Object.fromEntries(
    Object.entries(e.attributes).filter(
      ([key, value]) =>
        !identity.includes(key) && !CONTENT_ATTRIBUTES.includes(key.toLowerCase()) && flatAttribute(value),
    ),
  )
  return { v: 1, source: 'mod', kind: 'otel', ts: e.loggedAt, event: e.event, attributes }
}

type CompactResult = {
  tokensBefore?: number
  tokensAfter?: number
  usage?: Usage
  skip?: string
}

// One compaction attempt: its trigger, the loop that compacted, the size
// before and after in tokens, the summarizer's own usage, or why it was skipped.
export function compactionRecord(
  e: { trigger: string; agentId?: string },
  result: CompactResult,
  ms: number,
) {
  return {
    trigger: e.trigger,
    agent: e.agentId ?? null,
    tokens_before: result.tokensBefore ?? null,
    tokens_after: result.tokensAfter ?? null,
    usage: result.usage ?? null,
    skip: result.skip ?? null,
    ms,
  }
}

type ComposeSection = { id: string; text: string; scope: string }

// One system prompt as the engine renders it: each section's id, side of the
// cache boundary and size, never its text.
export function composeRecord(
  e: { model: string; promptModel: string; tools: readonly string[]; traits: readonly string[]; outputStyle: { name: string } | null },
  result: { sections: readonly ComposeSection[] },
) {
  return {
    model: e.model,
    prompt_model: e.promptModel,
    traits: e.traits,
    tool_count: e.tools.length,
    output_style: e.outputStyle?.name ?? null,
    sections: result.sections.map(s => ({ id: s.id, scope: s.scope, chars: s.text.length })),
    chars: result.sections.reduce((sum, s) => sum + s.text.length, 0),
  }
}

// Who authored a submitted main-loop prompt. Only the engine's own kinds are kept; any other value is
// `other`, so a name an SDK caller made up never reaches the capture.
const PROMPT_ORIGINS: readonly string[] = ['composer', 'sdk', 'task-notification', 'scheduled-trigger', 'peer']

// One prompt submission: the kind of its origin (null when the engine sent none), never the prompt text
// or any other field of the origin.
export function promptSubmitRecord(e: { origin?: { kind?: unknown } | null }) {
  const kind = e.origin?.kind
  if (kind === undefined || kind === null) return { origin: null }
  return { origin: typeof kind === 'string' && PROMPT_ORIGINS.includes(kind) ? kind : 'other' }
}

type AttachmentInput = {
  type: string
  text: string
  origin: { kind: string; event?: string }
  agentId?: string
}

// One message the engine injected for the model: its kind, who authored it
// and the size of the text the model reads (0 when a hook left it out), never
// the text.
export function attachmentRecord(e: AttachmentInput, result: { text: string | null }) {
  return {
    attachment: e.type,
    origin: e.origin.kind,
    origin_event: e.origin.event ?? null,
    agent: e.agentId ?? null,
    chars: result.text?.length ?? 0,
  }
}

// The context blocks the first user message carries (`claudeMd`, `currentDate`,
// ...): each block's name and size, and how many instruction files stand behind
// the `claudeMd` block.
export function contextRecord(result: {
  blocks: readonly { name: string; text: string }[]
  instructionFiles?: readonly unknown[]
}) {
  return {
    blocks: result.blocks.map(b => ({ name: b.name, chars: b.text.length })),
    chars: result.blocks.reduce((sum, b) => sum + b.text.length, 0),
    instruction_files: result.instructionFiles?.length ?? null,
  }
}

// One tool's description, as the engine first rendered the tool's schema in the
// session: its size, whether it waits behind ToolSearch and who provides it.
export function describeRecord(
  e: { tool: string; description: string; isDeferred?: true; provider: { plugin: string; tier: string } },
  result: { description: string; isDeferred?: boolean },
) {
  return {
    tool: e.tool,
    chars: result.description.length,
    deferred: (result.isDeferred ?? e.isDeferred) === true,
    provider: e.provider.plugin,
    tier: e.provider.tier,
  }
}

type SpawnInput = {
  tool_use_id: string
  prompt: string
  description: string
  subagentType: string
  provider: { plugin: string; tier: string }
  model?: string
  parentModel: string
  parentAgentId?: string
  background: boolean
  fork: boolean
  isTeammate?: true
  name?: string
  cwd?: string
}

const MAX_TYPE_CHARS = 64
const MAX_TICKET_DIGITS = 5
// A dispatch description names its ticket up front (`kanban #<id> ...`). Only
// that leading reference counts, with ASCII digits and a boundary that is no letter, digit or
// underscore of any script, so `#N` and `kanban #12abc` name nothing.
// The leading match needs 64 characters, but a later `#n` of another ticket must still be seen (the
// report refuses it), so the second-reference scan reads this many characters, bounded.
const MAX_SCAN_CHARS = 65536
const TICKET_RE = /^\s*kanban\s+#([0-9]+)(?![\p{L}\p{N}_])/iu

// The integer ticket id a dispatch description starts with (`kanban #<id>`), else null. The
// description is never kept: no text, path or name leaves this function, only an integer of at
// most five digits that is not 0. Reads a bounded part (MAX_SCAN_CHARS), whatever the size.
export function dispatchTicket(description: unknown): number | null {
  if (typeof description !== 'string') return null
  const head = description.slice(0, 64)
  const match = TICKET_RE.exec(head)
  if (!match || match[1].length > MAX_TICKET_DIGITS) return null
  // A digit run cut by the prefix limit is no id.
  if (head.length < description.length && match[0].length === head.length) return null
  const id = Number(match[1])
  // A later reference to another ticket (`#N`) names no one ticket, like the
  // report's rule, and may sit past the prefix. The same id repeated is still that ticket.
  for (const other of description.slice(match[0].length, MAX_SCAN_CHARS).matchAll(/#([0-9]+)/g)) {
    if (other[1].length > MAX_TICKET_DIGITS || Number(other[1]) !== id) return null
  }
  return id > 0 ? id : null
}

// A skill or agent type name as recorded: a string only, with controls (Cc), format
// characters such as bidi overrides (Cf), line and paragraph separators, private use, lone surrogates and every
// space but U+0020 removed, capped at MAX_TYPE_CHARS code points (never cut inside a surrogate pair), never
// empty. Unassigned code points (Cn) stay, so this and `scripts/safe_name.py` cannot disagree across Unicode
// versions. The same rule runs when a report reads a name, because older captures hold names as written, and
// both look at the first 4 * MAX_TYPE_CHARS code points only.
export function cleanName(name: unknown): string | null {
  if (typeof name !== 'string') return null
  let window = ''
  let seen = 0
  for (const char of name) {
    if (seen++ >= 4 * MAX_TYPE_CHARS) break
    window += char
  }
  const clean = Array.from(
    window
      .replace(/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/g, '')
      .replace(/[\p{Cc}\p{Cf}\p{Co}\p{Zl}\p{Zp}]|(?! )\p{Zs}/gu, '')
      .trim(),
  )
    .slice(0, MAX_TYPE_CHARS)
    .join('')
    .trim()
  return clean === '' ? null : clean
}

// An agent type name as recorded: the same rule as any other name.
export const cleanAgentType = cleanName

// One skill load: the cleaned name (null when nothing printable is left) and the prompt's size.
export function skillRecord(skill: unknown, promptChars: number) {
  return { skill: cleanName(skill), prompt_chars: promptChars }
}

// One subagent dispatch and its shape: `fork` (inherits the parent's context
// and model), `teammate` (a named agent of the session's team) or `fresh` (a
// new context holding only the prompt). Work that never dispatches stays on the
// main loop and has no spawn. Sizes and names of types, never the prompt, the
// description or the teammate's name.
export function spawnRecord(
  e: SpawnInput,
  result: { model?: string; agentId?: string; teammateId?: string; deny?: string },
) {
  return {
    tool_use_id: e.tool_use_id,
    shape: e.fork ? 'fork' : e.isTeammate === true ? 'teammate' : 'fresh',
    subagent_type: cleanAgentType(e.subagentType),
    // The integer id of a leading `kanban #<id>` in the description, nothing else of it.
    ticket: dispatchTicket(e.description),
    provider: e.provider.plugin,
    tier: e.provider.tier,
    agent: e.parentAgentId ?? null,
    spawned: result.agentId ?? null,
    model_requested: e.model ?? null,
    model: result.model ?? null,
    parent_model: e.parentModel,
    background: e.background,
    named: e.name !== undefined,
    cwd_set: e.cwd !== undefined,
    prompt_chars: typeof e.prompt === 'string' ? e.prompt.length : 0,
    description_chars: typeof e.description === 'string' ? e.description.length : 0,
    denied: result.deny !== undefined,
  }
}

// One agent type the engine offered the model (listed or named at dispatch):
// its name, where it came from, who provides it, the size of its listing line
// and whether a hook offered it at all.
export function offerRecord(
  e: { agent: string; description: string; source: string; provider: { plugin: string; tier: string } },
  result: { isOffered: boolean },
) {
  return {
    subagent_type: cleanName(e.agent),
    definition: e.source,
    provider: e.provider.plugin,
    tier: e.provider.tier,
    chars: e.description.length,
    offered: result.isOffered,
  }
}

// One hooks module the engine admitted to the chain, from `plugin.register`: its
// name, version, how it was installed (`<name>@<marketplace>`, `@inline` for
// --plugin-dir, `@builtin`), its tier, and the events and mods API calls it
// uses. Never its directory, which holds the user's home path. Fires in `claude
// -p` too, but only for hooks modules that load after this one: a plugin with
// no module has no record here (the OTel mirror's plugin_loaded covers it in an
// interactive session).
export function pluginRegisterRecord(e: {
  name: string
  version?: string
  provenance: string
  tier: string
  uses: { events: readonly string[]; calls: readonly string[] }
}) {
  return {
    plugin: e.name,
    version: e.version ?? null,
    provenance: e.provenance,
    tier: e.tier,
    events: e.uses.events,
    calls: e.uses.calls,
  }
}

// The tool a settings hook event is about: `tool_name` on a classic hook's stdin
// input, `tool` on PreToolUse's envelope. A name, so a plugin's hooks.json
// matcher can be tested against it.
export function hookTool(e: Readonly<Record<string, unknown>>) {
  const tool = e.tool_name ?? e.tool
  return typeof tool === 'string' ? tool : null
}

// The settings hook event of `e`: its `hook_event_name`, except on PreToolUse,
// whose envelope is a tool call's and carries none.
export function hookEvent(e: Readonly<Record<string, unknown>>) {
  if (typeof e.hook_event_name === 'string') return e.hook_event_name
  return typeof e.tool === 'string' ? 'PreToolUse' : 'unknown'
}

// The plugins enabled in the merged settings, by id (`name@marketplace`), the
// way `enabledPlugins` holds them; the OTel plugin_loaded event redacts a
// third-party plugin's name, the local settings do not. A plugin loaded with
// --plugin-dir is not in settings: plugin.register names its module instead.
export function pluginSetRecord(settings: Readonly<Record<string, unknown>>) {
  const enabled = settings.enabledPlugins
  const ids =
    typeof enabled === 'object' && enabled !== null
      ? Object.entries(enabled).filter(([, on]) => on === true).map(([id]) => id)
      : []
  return { enabled: ids.sort() }
}

export type HookRunTally = { runs: number; ms: number; maxMs: number }

// Ids a listing did not name, with when it was asked: an id is not listed again for
// `windowMs`, so five events of a workflow agent cost one list. `now` is injected for tests; a clock
// that went backwards counts as outside the window. Bounded: past `max` ids the oldest answers go.
export class AgentMisses {
  private readonly at = new Map<string, number>()
  constructor(
    private readonly windowMs = 60000,
    private readonly max = 1000,
    private readonly now: () => number = Date.now,
  ) {}
  isRecent(id: string): boolean {
    const at = this.at.get(id)
    if (at === undefined) return false
    const age = this.now() - at
    return age >= 0 && age < this.windowMs
  }
  add(id: string) {
    this.at.delete(id)
    this.at.set(id, this.now())
    if (this.at.size > this.max) this.at.delete(this.at.keys().next().value as string)
  }
}

// Wall time the settings-hook chain took for one classic event, summed per
// session, event and tool so a session's thousands of tool hooks stay a few
// records. The engine fires every classic event whether or not a hook is
// configured, so a row of 0 ms is an event nothing hooked.
export class HookRuns {
  private tallies = new Map<string, { session: string; event: string; tool: string | null } & HookRunTally>()

  add(session: string | undefined, event: string, tool: string | null, ms: number) {
    const key = JSON.stringify([session, event, tool])
    const tally = this.tallies.get(key) ?? { session: session ?? '', event, tool, runs: 0, ms: 0, maxMs: 0 }
    tally.runs += 1
    tally.ms += ms
    tally.maxMs = Math.max(tally.maxMs, ms)
    this.tallies.set(key, tally)
  }

  // The tallies since the last drain, as records, and start over.
  drain(ts: string): ObserverRecord[] {
    const out = [...this.tallies.values()].map(t => ({
      v: 1,
      source: 'mod',
      kind: 'hook_runs',
      ts,
      session: t.session || undefined,
      hook_event: t.event,
      tool: t.tool,
      runs: t.runs,
      ms: t.ms,
      max_ms: t.maxMs,
    }))
    this.tallies.clear()
    return out
  }
}
