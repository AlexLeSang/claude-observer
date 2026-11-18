// The subagent alerts: a live tally per subagent id (one agent id of one session, summed over every run of
// it, resumes included), and two alerts read from it, a size alert and a time alert, each fired once per id.
// Pure and clock-free: every time is a number of milliseconds the caller passes in. It draws nothing and
// calls no engine API; alertEvents.ts feeds it and shows what it returns.
//
// The tally is per session; the fired marks are kept apart by agent id alone (FiredMarks), so they outlast a
// /clear and the eviction of a tally.
//
// Held per agent: counts, milliseconds, a cost sum, two fired marks and two allowlisted class names (type
// and model). The agent id is an opaque map key and is in no text. Nothing else about the agent is kept: no
// path, model id, custom type name, prompt, description, tool text or error text.

import { SubLabels } from './subLabels'

// The size limit before any calibration, in tokens.
const DEFAULT_SIZE_CUTOFF = 10_000_000
// The time limit before any calibration, in hours.
const DEFAULT_HOURS = 2
// The time alert is checked on a tick at most this often (a step end always checks).
export const TIME_CHECK_MS = 60_000
const MAX_AGENTS = 200
export const MAX_ID_CHARS = 200
// One count of one request; anything above is not a token count (the probe's bound).
const MAX_TOKENS = 10_000_000
const MAX_COST_USD = 1000
const MAX_STEP_MS = 86_400_000
// One run; a clock that jumps, or a run left open by a dead process, counts no more than this.
const MAX_RUN_MS = 7 * 86_400_000
// A rate is shown for an agent with this many steps, over at least this much model time (the probe's rule).
const RATE_MIN_STEPS = 10
const RATE_FLOOR_MS = 60_000

// The type and model classes of the runaway probe script, copied: a Python test
// fails when either drifts. A custom agent's own name is never shown: any other name is `custom`.
export const TYPE_CLASSES = ['general-purpose', 'Explore', 'Plan', 'statusline-setup', 'claude-code-guide', 'fork'] as const
export const MODEL_CLASSES = ['opus', 'sonnet', 'haiku', 'fable', 'mythos'] as const

export function typeClass(value: unknown): string {
  if (typeof value !== 'string' || value === '') return 'unknown'
  return (TYPE_CLASSES as readonly string[]).includes(value) ? value : 'custom'
}

export function modelClass(value: unknown): string {
  if (typeof value !== 'string' || value === '') return 'unknown'
  const low = value.toLowerCase()
  return MODEL_CLASSES.find(name => low.includes(name)) ?? 'other'
}

// A JSON number or an ASCII numeric string, else NaN; the probe's `_number`.
const NUMERIC = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/
function toNumber(value: unknown): number {
  if (typeof value === 'number') return value
  if (typeof value === 'string' && value.length <= 64 && NUMERIC.test(value)) return Number(value)
  return NaN
}

function bounded(value: unknown, top: number): number | undefined {
  const n = toNumber(value)
  return Number.isFinite(n) && n >= 0 && n <= top ? n : undefined
}

type AgentRec = {
  input: number
  output: number
  cacheRead: number
  cacheCreation: number
  steps: number
  // Model time: the sum of the steps' own milliseconds.
  modelMs: number
  costUsd: number
  isCosted: boolean
  runs: number
  // Active time of the runs that ended, in milliseconds.
  activeMs: number
  // When the run in progress started (epoch ms), or null.
  startedAt: number | null
  // The last time this agent was heard (epoch ms).
  seenAt: number
  type: string
  model: string
  isSizeFired: boolean
  isTimeFired: boolean
}

function emptyAgent(nowMs: number): AgentRec {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheCreation: 0,
    steps: 0,
    modelMs: 0,
    costUsd: 0,
    isCosted: false,
    runs: 0,
    activeMs: 0,
    startedAt: null,
    seenAt: nowMs,
    type: 'unknown',
    model: 'unknown',
    isSizeFired: false,
    isTimeFired: false,
  }
}

export type Limits = {
  sizeCutoff: number
  // Set with `sizeAgents` when the size cutoff comes from a calibration; both absent means the default.
  sizePercent: number | undefined
  sizeAgents: number | undefined
  hours: number
  isHoursDefault: boolean
}

export function defaultLimits(): Limits {
  return { sizeCutoff: DEFAULT_SIZE_CUTOFF, sizePercent: undefined, sizeAgents: undefined, hours: DEFAULT_HOURS, isHoursDefault: true }
}

export const MAX_CALIBRATION_BYTES = 65536

// alert-calibration.json as the mod reads it: `percentile`, `cutoff` (tokens), `agents` and `hours`. Each
// value must be a JSON number in range, else it is ignored and its default applies. The size cutoff needs all
// three of its fields. The floors are the script's (agent_alert_calibration.py MIN_PERCENTILE, MIN_HOURS, and a
// cutoff of at least one token, which it refuses to write below): a tiny value is a typo or a damaged file and
// would flag every subagent. Any text that is not a JSON object gives the defaults.
export function parseCalibration(text: unknown): Limits {
  const limits = defaultLimits()
  if (typeof text !== 'string' || text.length > MAX_CALIBRATION_BYTES) return limits
  let raw: unknown
  try {
    raw = JSON.parse(text)
  } catch {
    return limits
  }
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return limits
  const { percentile, cutoff, agents, hours } = raw as Record<string, unknown>
  const isNumber = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
  if (
    isNumber(percentile) &&
    percentile >= 1 &&
    percentile < 100 &&
    isNumber(cutoff) &&
    cutoff >= 1 &&
    cutoff <= 1e12 &&
    isNumber(agents) &&
    Number.isSafeInteger(agents) &&
    agents >= 1 &&
    agents <= 1e9
  ) {
    limits.sizeCutoff = cutoff
    limits.sizePercent = percentile
    limits.sizeAgents = agents
  }
  if (isNumber(hours) && hours >= 0.1 && hours <= 1000) {
    limits.hours = hours
    limits.isHoursDefault = false
  }
  return limits
}

type Firing = { id: string; kind: 'size' | 'time'; text: string }

function tokensOf(a: AgentRec): number {
  return a.input + a.output + a.cacheRead + a.cacheCreation
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1000) return `${Math.round(n / 1000)}K`
  return String(Math.round(n))
}

export function formatDuration(ms: number): string {
  const minutes = Math.floor(Math.max(0, ms) / 60_000)
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  if (h === 0) return `${m}m`
  return m === 0 ? `${h}h` : `${h}h ${String(m).padStart(2, '0')}m`
}

function who(label: string, a: AgentRec): string {
  return `${label} (${a.type}, ${a.model})`
}

function runsText(a: AgentRec): string {
  return a.runs > 0 ? ` over ${a.runs} run${a.runs === 1 ? '' : 's'}` : ''
}

export function activeMs(a: AgentRec, nowMs: number): number {
  const open = a.startedAt === null ? 0 : Math.min(MAX_RUN_MS, Math.max(0, nowMs - a.startedAt))
  return a.activeMs + open
}

function sizeText(label: string, a: AgentRec, limits: Limits): string {
  const tokens = tokensOf(a)
  const head = `${who(label, a)} has used ${formatTokens(tokens)} tokens${runsText(a)}: `
  const rule =
    limits.sizePercent === undefined
      ? `over the default limit, not calibrated to your history (${formatTokens(limits.sizeCutoff)} tokens).`
      : `larger than ${Math.min(99.99, Number(limits.sizePercent.toFixed(2)))}% of your ${limits.sizeAgents} past subagents.`
  const context: string[] = []
  if (a.steps > 0) context.push(`${formatTokens(tokens / a.steps)} tokens per step`)
  if (a.steps >= RATE_MIN_STEPS) {
    context.push(`${formatTokens(tokens / (Math.max(RATE_FLOOR_MS, a.modelMs) / 60_000))} per minute`)
  }
  if (a.isCosted) context.push(`$${a.costUsd.toFixed(2)} so far`)
  return `${head}${rule}${context.length > 0 ? ` ${context.join(', ')}.` : ''}`
}

function timeText(label: string, a: AgentRec, limits: Limits, nowMs: number): string {
  const limit = formatDuration(limits.hours * 3_600_000)
  const rule = limits.isHoursDefault ? `default limit, not calibrated to your history: ${limit}` : `your limit: ${limit}`
  return `${who(label, a)} has been running ${formatDuration(activeMs(a, nowMs))}${runsText(a)} (${rule}). ${formatTokens(tokensOf(a))} tokens so far.`
}

// The alerts that have fired, per agent id alone: an agent id is unique, so the marks outlast the session id
// (/clear, /resume, /branch) and the eviction of an agent's tally. At most MAX_FIRED ids are kept, the oldest
// forgotten first.
const MAX_FIRED = 1000
const SIZE_BIT = 1
const TIME_BIT = 2

export class FiredMarks {
  private marks = new Map<string, number>()
  // Set by every change the store has not seen yet.
  isDirty = false

  // True once the store's marks have been read (or there was nothing to read); until then a write of the
  // marks would replace the stored ones with only those made in this process.
  isSynced = false

  constructor(raw?: unknown) {
    this.load(raw)
  }

  private load(raw: unknown): void {
    if (!Array.isArray(raw)) return
    for (const row of raw.slice(-MAX_FIRED)) {
      if (!Array.isArray(row) || typeof row[0] !== 'string' || row[0] === '' || row[0].length > MAX_ID_CHARS) continue
      const bits = row[1]
      if (bits === 1 || bits === 2 || bits === 3) this.marks.set(row[0], (this.marks.get(row[0]) ?? 0) | bits)
    }
  }

  // The stored marks, read late: they go first (older), the marks made meanwhile keep their place after them,
  // and the merged list is written at the next firing.
  absorb(raw: unknown): void {
    const mine = [...this.marks]
    this.load(raw)
    for (const [id, bits] of mine) {
      const both = (this.marks.get(id) ?? 0) | bits
      this.marks.delete(id)
      this.marks.set(id, both)
    }
    while (this.marks.size > MAX_FIRED) this.marks.delete(this.marks.keys().next().value as string)
    if (mine.length > 0) this.isDirty = true
    this.isSynced = true
  }

  has(id: string, kind: 'size' | 'time'): boolean {
    return ((this.marks.get(id) ?? 0) & (kind === 'size' ? SIZE_BIT : TIME_BIT)) !== 0
  }

  add(id: string, kind: 'size' | 'time'): void {
    const bits = (this.marks.get(id) ?? 0) | (kind === 'size' ? SIZE_BIT : TIME_BIT)
    this.marks.delete(id)
    this.marks.set(id, bits)
    if (this.marks.size > MAX_FIRED) this.marks.delete(this.marks.keys().next().value as string)
    this.isDirty = true
  }

  snapshot(): [string, number][] {
    return [...this.marks]
  }
}

export class AlertTracker {
  private agents = new Map<string, AgentRec>()
  private labels: SubLabels
  // Set by every change the store has not seen yet.
  isDirty = false
  readonly marks: FiredMarks

  constructor(raw?: unknown, marks: FiredMarks = new FiredMarks()) {
    this.marks = marks
    const body = typeof raw === 'object' && raw !== null ? (raw as { agents?: unknown; labels?: unknown }) : {}
    this.labels = new SubLabels(body.labels)
    if (!Array.isArray(body.agents)) return
    for (const row of body.agents.slice(0, MAX_AGENTS)) {
      if (!Array.isArray(row) || typeof row[0] !== 'string' || row[0] === '' || row[0].length > MAX_ID_CHARS) continue
      if (this.agents.has(row[0]) || typeof row[1] !== 'object' || row[1] === null) continue
      const rec = sanitizeAgent(row[1] as Record<string, unknown>)
      this.agents.set(row[0], rec)
      // A tally written before the marks had a store of their own carries them on the row.
      if (rec.isSizeFired) this.marks.add(row[0], 'size')
      if (rec.isTimeFired) this.marks.add(row[0], 'time')
    }
  }

  snapshot(): { v: 1; labels: ReturnType<SubLabels['snapshot']>; agents: [string, AgentRec][] } {
    return { v: 1, labels: this.labels.snapshot(), agents: [...this.agents] }
  }

  // A tracker read back from the store after a restart: a run left open then is closed at the last time the
  // agent was heard, so the time the process was down is not counted as running time.
  closeStaleRuns(): void {
    for (const a of this.agents.values()) {
      if (a.startedAt === null) continue
      a.activeMs += Math.min(MAX_RUN_MS, Math.max(0, a.seenAt - a.startedAt))
      a.startedAt = null
      this.isDirty = true
    }
  }

  // The `sub<N>` label of an agent in this session's numbering, numbering it now when new: the cache line
  // asks here so that both lines name an agent alike. Undefined for an id that cannot be numbered.
  labelOf(id: unknown): string | undefined {
    return this.labels.label(id)
  }

  private agent(id: unknown, nowMs: number): AgentRec | undefined {
    if (typeof id !== 'string' || id === '' || id.length > MAX_ID_CHARS || !Number.isFinite(nowMs)) return undefined
    const known = this.agents.get(id)
    if (known !== undefined) {
      known.seenAt = nowMs
      return known
    }
    // Numbered first: an id the numbering has no room for is not tracked, and must not evict one that is.
    if (this.labels.label(id) === undefined) return undefined
    if (this.agents.size >= MAX_AGENTS && !this.evict()) return undefined
    const made = emptyAgent(nowMs)
    this.agents.set(id, made)
    this.isDirty = true
    return made
  }

  // Drops the longest-unheard agent that has no run in progress.
  private evict(): boolean {
    let oldest: string | undefined
    for (const [id, a] of this.agents) {
      if (a.startedAt === null && (oldest === undefined || a.seenAt < (this.agents.get(oldest) as AgentRec).seenAt)) oldest = id
    }
    if (oldest === undefined) return false
    this.agents.delete(oldest)
    return true
  }

  // A run of the agent starts. A start with the previous run still open closes that one at the last time the
  // agent was heard, so a lost end does not make one run of two.
  runStart(id: unknown, nowMs: number): void {
    const known = typeof id === 'string' ? this.agents.get(id) : undefined
    if (known !== undefined && known.startedAt !== null) {
      known.activeMs += Math.min(MAX_RUN_MS, Math.max(0, known.seenAt - known.startedAt))
    }
    const a = this.agent(id, nowMs)
    if (a === undefined) return
    a.startedAt = nowMs
    a.runs += 1
    this.isDirty = true
  }

  // The run ends (an aborted one too). An end heard with no start of this agent in the process (its spawn was
  // nested in the turn) adds the turn's own duration, once: only for an agent with no run counted yet.
  runEnd(id: unknown, nowMs: number, durationMs: unknown): void {
    const a = this.agent(id, nowMs)
    if (a === undefined) return
    if (a.startedAt !== null) {
      a.activeMs += Math.min(MAX_RUN_MS, Math.max(0, nowMs - a.startedAt))
      a.startedAt = null
    } else if (a.runs === 0) {
      const ms = bounded(durationMs, MAX_RUN_MS)
      if (ms !== undefined) a.activeMs += ms
      a.runs = 1
    }
    this.isDirty = true
  }

  // One finished model request of the agent. `usage` null (no response) counts nothing.
  step(id: unknown, usage: unknown, stepMs: unknown, type: unknown, nowMs: number): void {
    if (typeof usage !== 'object' || usage === null) return
    const a = this.agent(id, nowMs)
    if (a === undefined) return
    const u = usage as Record<string, unknown>
    const count = (key: string) => Math.trunc(bounded(u[key], MAX_TOKENS) ?? 0)
    a.input += count('input_tokens')
    a.output += count('output_tokens')
    a.cacheRead += count('cache_read_input_tokens')
    a.cacheCreation += count('cache_creation_input_tokens')
    a.steps += 1
    a.modelMs += bounded(stepMs, MAX_STEP_MS) ?? 0
    a.model = modelClass(u.model)
    const cls = typeClass(type)
    if (cls !== 'unknown') a.type = cls
    this.isDirty = true
  }

  // The cost of one `api_request` that carried the agent's id.
  cost(id: unknown, usd: unknown, nowMs: number): void {
    const value = bounded(usd, MAX_COST_USD)
    if (value === undefined) return
    const a = this.agent(id, nowMs)
    if (a === undefined) return
    a.costUsd += value
    a.isCosted = true
    this.isDirty = true
  }

  hasOpenRun(): boolean {
    for (const a of this.agents.values()) if (a.startedAt !== null) return true
    return false
  }

  // The alerts that fire now, each marked fired so it does not fire again. `id` is returned for the caller's
  // bookkeeping only and is in no text. `only` limits the check to one agent (a step end).
  due(limits: Limits, nowMs: number, only?: string): Firing[] {
    const out: Firing[] = []
    for (const [id, a] of this.agents) {
      if (only !== undefined && id !== only) continue
      const label = this.labels.label(id)
      if (label === undefined) continue
      // The row's flag counts too: its mark may have left the newest-MAX_FIRED list while the agent is tracked.
      if (!a.isSizeFired && !this.marks.has(id, 'size') && tokensOf(a) > limits.sizeCutoff) {
        a.isSizeFired = true
        this.marks.add(id, 'size')
        out.push({ id, kind: 'size', text: sizeText(label, a, limits) })
      }
      if (!a.isTimeFired && !this.marks.has(id, 'time') && activeMs(a, nowMs) > limits.hours * 3_600_000) {
        a.isTimeFired = true
        this.marks.add(id, 'time')
        out.push({ id, kind: 'time', text: timeText(label, a, limits, nowMs) })
      }
    }
    if (out.length > 0) this.isDirty = true
    return out
  }

  // One line per running subagent that has fired an alert.
  paneLines(limits: Limits, nowMs: number): string[] {
    const out: [number, string][] = []
    for (const [id, a] of this.agents) {
      const isSize = this.marks.has(id, 'size')
      const isTime = this.marks.has(id, 'time')
      if (a.startedAt === null || (!isSize && !isTime)) continue
      const label = this.labels.label(id)
      if (label === undefined) continue
      const parts: string[] = []
      if (isSize) parts.push(`over the size limit, ${formatTokens(tokensOf(a))} tokens`)
      if (isTime) parts.push(`over the time limit, running ${formatDuration(activeMs(a, nowMs))}`)
      out.push([Number(label.slice(3)), `${who(label, a)}: ${parts.join('; ')}`])
    }
    return out.sort((x, y) => x[0] - y[0]).map(([, line]) => line)
  }

  // Read-only view for tests and the pane: the tally of one agent.
  get(id: string): Readonly<AgentRec> | undefined {
    return this.agents.get(id)
  }
}

function sanitizeAgent(raw: Record<string, unknown>): AgentRec {
  const a = emptyAgent(0)
  const num = (v: unknown, top: number) => bounded(typeof v === 'string' ? NaN : v, top) ?? 0
  a.input = Math.trunc(num(raw.input, 1e13))
  a.output = Math.trunc(num(raw.output, 1e13))
  a.cacheRead = Math.trunc(num(raw.cacheRead, 1e13))
  a.cacheCreation = Math.trunc(num(raw.cacheCreation, 1e13))
  a.steps = Math.trunc(num(raw.steps, 1e9))
  a.modelMs = num(raw.modelMs, 1e13)
  a.costUsd = num(raw.costUsd, 1e9)
  a.isCosted = raw.isCosted === true
  a.runs = Math.trunc(num(raw.runs, 1e9))
  a.activeMs = num(raw.activeMs, 1e13)
  a.seenAt = num(raw.seenAt, 1e15)
  const started = typeof raw.startedAt === 'number' ? bounded(raw.startedAt, 1e15) : undefined
  a.startedAt = started ?? null
  a.type = typeof raw.type === 'string' && (raw.type === 'custom' || raw.type === 'unknown' || (TYPE_CLASSES as readonly string[]).includes(raw.type)) ? raw.type : 'unknown'
  a.model =
    typeof raw.model === 'string' && (raw.model === 'other' || raw.model === 'unknown' || (MODEL_CLASSES as readonly string[]).includes(raw.model)) ? raw.model : 'unknown'
  a.isSizeFired = raw.isSizeFired === true
  a.isTimeFired = raw.isTimeFired === true
  return a
}
