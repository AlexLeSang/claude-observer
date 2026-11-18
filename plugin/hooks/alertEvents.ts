// Feeds the subagent alerts (alerts.ts) from the engine's events and shows what they return: one transcript
// line through `$.ui.log` when an alert fires, and the lines of the pane's live tab through $.state. It
// observes: nothing here stops, blocks or throttles a subagent, a failure inside is caught, and there is no
// `ui.notify`, no `ui.notice`, no toast and no network. The tally and the fired marks live in $.store under
// the session's id, so /clear, /resume and /branch do not reset them; the state atom holds only the pane's
// text lines.
//
// The engine's static scan wants `$` passed only to functions declared at the top of the file, and refuses a
// second unmatched handler of one event in a module. So everything is a top-level function taking the state
// object `ctx`, and observer.ts calls the feeds from its own turn and telemetry handlers. It hands them a `Feed`
// (feed.ts) built from `$` in its own file, because Claude Code 2.1.287 follows `$` only within one file.

import type { On } from 'claude-code'
import type { Feed } from './feed'
import { AlertTracker, FiredMarks, MAX_CALIBRATION_BYTES, MAX_ID_CHARS, TIME_CHECK_MS, defaultLimits, parseCalibration, type Limits } from './alerts'

export const SWITCH_KEY = 'usage-observer.subagentAlerts'
// $.store is shared by every session on the machine and holds 4 MiB in all: one key per session, and an
// index that keeps the newest few.
const STORE_PREFIX = 'subagentAlerts:'
const INDEX_KEY = 'subagentAlerts:index'
// The fired marks, by agent id alone and shared by every session: /clear gives a new session id, and an agent
// that fired must not fire again (spec A3). No colon, so no session id can name it.
const FIRED_KEY = 'subagentAlertsFired'
const KEEP_SESSIONS = 20
const KEEP_SLOTS = 4
const SEEN_REQUESTS = 500
const CALIBRATION_FILE = 'alert-calibration.json'
const GIT_TIMEOUT_MS = 3000

type Slot = { sid: string; tracker: AlertTracker; chain: Promise<void>; isIndexed: boolean }

export type AlertCtx = {
  nowMs: () => number
  isEnabled: boolean
  limits: Limits
  loading: Map<string, Promise<Slot>>
  // The fired marks, read once from the store before the first tally is loaded.
  marks: FiredMarks
  marksLoaded: Promise<void> | undefined
  current: Slot | undefined
  lastCheck: number
  lastPane: string | undefined
  isTicking: boolean
  seenRequests: string[]
}

type AgentEvent = { agentId?: unknown }

// The context of the module's registration; observer.ts reaches it through the alert* functions below.
let active: AlertCtx | undefined

export function activeAlerts(): AlertCtx | undefined {
  return active
}

export function makeAlertCtx(options: Readonly<Record<string, unknown>>, nowMs: () => number = () => Date.now()): AlertCtx {
  return {
    nowMs,
    isEnabled: options.subagentAlerts !== false && options.subagentAlerts !== 'false',
    limits: defaultLimits(),
    loading: new Map(),
    marks: new FiredMarks(),
    marksLoaded: undefined,
    current: undefined,
    lastCheck: 0,
    lastPane: undefined,
    isTicking: false,
    seenRequests: [],
  }
}

async function loadSlot($: Feed, ctx: AlertCtx, sid: string): Promise<Slot> {
  let raw: unknown
  try {
    raw = await $.store.get(STORE_PREFIX + sid)
  } catch {
    // No tally readable: start from none.
  }
  const tracker = new AlertTracker(raw, ctx.marks)
  if (raw !== undefined) tracker.closeStaleRuns()
  return { sid, tracker, chain: Promise.resolve(), isIndexed: raw !== undefined }
}

async function slotFor($: Feed, ctx: AlertCtx): Promise<Slot> {
  const sid = String(await $.session.id())
  if (ctx.marksLoaded === undefined) {
    ctx.marksLoaded = (async () => {
      try {
        ctx.marks.absorb(await $.store.get(FIRED_KEY))
      } catch {
        // No marks readable now: none is written over the stored ones.
      }
    })()
  }
  await ctx.marksLoaded
  // A failed read is not kept: the next event reads again.
  if (!ctx.marks.isSynced) ctx.marksLoaded = undefined
  let pending = ctx.loading.get(sid)
  if (pending === undefined) {
    pending = loadSlot($, ctx, sid)
    ctx.loading.set(sid, pending)
    for (const key of ctx.loading.keys()) {
      if (ctx.loading.size <= KEEP_SLOTS) break
      if (key !== sid) ctx.loading.delete(key)
    }
  }
  ctx.current = await pending
  return ctx.current
}

function persist($: Feed, slot: Slot) {
  if (!slot.tracker.isDirty) return
  slot.tracker.isDirty = false
  const marks = slot.tracker.marks
  // Not before the stored marks have been read: a write now would erase them.
  const isMarksDirty = marks.isDirty && marks.isSynced
  marks.isDirty = false
  slot.chain = slot.chain.then(async () => {
    if (isMarksDirty) {
      try {
        // The snapshot is taken here too, so a later write never holds fewer marks than an earlier one.
        await $.store.set(FIRED_KEY, marks.snapshot())
      } catch {
        // Observation only: the marks stay in memory.
      }
    }
    try {
      // The snapshot is taken here, so the last write holds the latest tally.
      await $.store.set(STORE_PREFIX + slot.sid, slot.tracker.snapshot())
      if (!slot.isIndexed) {
        slot.isIndexed = true
        const listed = await $.store.get(INDEX_KEY)
        const ids = (Array.isArray(listed) ? listed : []).filter((v): v is string => typeof v === 'string' && v !== slot.sid)
        ids.push(slot.sid)
        for (const old of ids.splice(0, Math.max(0, ids.length - KEEP_SESSIONS))) await $.store.delete(STORE_PREFIX + old)
        await $.store.set(INDEX_KEY, ids)
      }
    } catch {
      // Observation only: the tally stays in memory.
    }
  })
}

async function refreshPane($: Feed, ctx: AlertCtx, slot: Slot) {
  const lines = ctx.isEnabled ? slot.tracker.paneLines(ctx.limits, ctx.nowMs()) : []
  const key = JSON.stringify(lines)
  if (key === ctx.lastPane) return
  try {
    await $.state.set({ plugin: 'usage-observer', key: 'alerts' } as const, { lines })
    ctx.lastPane = key
  } catch {
    // Observation only.
  }
}

// Both alerts, for one agent (a step or run end) or all of them (the tick). The marks are set before the
// line is written, so a refused write cannot make the same alert fire again.
async function evaluate($: Feed, ctx: AlertCtx, slot: Slot, only?: string) {
  if (ctx.isEnabled) {
    const at = ctx.nowMs()
    ctx.lastCheck = at
    for (const firing of slot.tracker.due(ctx.limits, at, only)) {
      try {
        $.ui.log(firing.text)
      } catch {
        // Observation only.
      }
    }
  }
  persist($, slot)
  await refreshPane($, ctx, slot)
}

// The feeds, called from observer.ts's own handlers. Each catches its own failures and returns nothing.
export async function turnStart($: Feed, ctx: AlertCtx, e: AgentEvent, at: number) {
  try {
    if (typeof e.agentId === 'string') {
      const slot = await slotFor($, ctx)
      slot.tracker.runStart(e.agentId, at)
      persist($, slot)
      await refreshPane($, ctx, slot)
    }
  } catch {
    // Observation only.
  }
}

// `type` is the agent's type name as observer.ts resolved it; only its class is ever kept or shown.
export async function stepEnd($: Feed, ctx: AlertCtx, e: AgentEvent, usage: unknown, startedAt: number, type: unknown) {
  try {
    if (typeof e.agentId === 'string') {
      const slot = await slotFor($, ctx)
      const at = ctx.nowMs()
      slot.tracker.step(e.agentId, usage, at - startedAt, type, at)
      await evaluate($, ctx, slot, e.agentId)
    }
  } catch {
    // Observation only.
  }
}

export async function turnEnd($: Feed, ctx: AlertCtx, e: AgentEvent & { durationMs?: unknown }, at: number) {
  try {
    if (typeof e.agentId === 'string') {
      const slot = await slotFor($, ctx)
      slot.tracker.runEnd(e.agentId, at, e.durationMs)
      await evaluate($, ctx, slot, e.agentId)
    }
  } catch {
    // Observation only.
  }
}

// The cost of a request that carries the agent's id; a request with no id is not attributed.
export async function telemetry($: Feed, ctx: AlertCtx, e: { to?: unknown; event?: unknown; attributes?: Record<string, unknown> }) {
  try {
    if (e.to !== 'collector' || (e.event !== 'api_request' && e.event !== 'claude_code.api_request') || e.attributes === undefined) return
    const agent = e.attributes.agent_id
    const request = e.attributes.request_id
    const isRepeat = typeof request === 'string' && request !== '' && ctx.seenRequests.includes(request)
    if (typeof agent !== 'string' || agent === '' || agent.length > MAX_ID_CHARS || isRepeat) return
    if (typeof request === 'string' && request !== '' && request.length <= MAX_ID_CHARS) {
      ctx.seenRequests.push(request)
      if (ctx.seenRequests.length > SEEN_REQUESTS) ctx.seenRequests.shift()
    }
    const slot = await slotFor($, ctx)
    slot.tracker.cost(agent, e.attributes.cost_usd, ctx.nowMs())
    persist($, slot)
  } catch {
    // Observation only.
  }
}

// The flush timer's tick: checks the time alert at most every TIME_CHECK_MS and keeps the pane lines current.
export async function tick($: Feed, ctx: AlertCtx) {
  if (ctx.isTicking) return
  ctx.isTicking = true
  try {
    const slot = ctx.current
    if (slot === undefined || !ctx.isEnabled || !slot.tracker.hasOpenRun()) return
    if (ctx.nowMs() - ctx.lastCheck >= TIME_CHECK_MS) await evaluate($, ctx, slot)
    else await refreshPane($, ctx, slot)
  } catch {
    // Observation only.
  } finally {
    ctx.isTicking = false
  }
}

// True when git lists alert-calibration.json of `dir` in its index: a file a cloned repository ships, which the
// user never agreed to (a tiny size limit or a huge one would set or silence the alerts), so it is not read
// (same question as capture_exclude.tracked_warning's, with the repository's `core.fsmonitor` off).
// Git missing, refused, timed out or not a repository (its failures) is "not tracked": the project is then the
// user's own, as resolveHome treats it.
//
// The engine's `process.run` can add variables to the child's environment but not remove one, so `env -u` clears
// the ones that choose a repository or change how a pathspec reads (`GIT_LITERAL_PATHSPECS=1` makes the
// `:(literal,icase)` magic below match nothing, so a tracked file would read as untracked): the session's
// `GIT_DIR` would answer for another repository. The same names, in the same order, are `_GIT_ENV_ORDER` in
// plugin/scripts/capture_paths.py; a test in the repository keeps them equal. `GIT_CEILING_DIRECTORIES` stays set,
// as resolveHome honours it. (`env` missing
// is a failure, so "not tracked", like git missing.)
export const GIT_REPO_ENV: readonly string[] = [
  'GIT_DIR',
  'GIT_COMMON_DIR',
  'GIT_WORK_TREE',
  'GIT_INDEX_FILE',
  'GIT_OBJECT_DIRECTORY',
  'GIT_ALTERNATE_OBJECT_DIRECTORIES',
  'GIT_NAMESPACE',
  'GIT_LITERAL_PATHSPECS',
  'GIT_GLOB_PATHSPECS',
  'GIT_NOGLOB_PATHSPECS',
  'GIT_ICASE_PATHSPECS',
]

export function trackedArgv(): string[] {
  return [
    'env',
    ...GIT_REPO_ENV.flatMap(name => ['-u', name]),
    'git',
    '-c',
    'core.fsmonitor=false',
    'ls-files',
    '-z',
    '--', `:(literal,icase)${CALIBRATION_FILE}`,
  ]
}

async function isGitTracked($: Feed, dir: string): Promise<boolean> {
  try {
    const ran = await $.process.run(
      trackedArgv(),
      { cwd: dir, timeoutMs: GIT_TIMEOUT_MS },
    )
    return ran.exitCode === 0 && typeof ran.stdout === 'string' && ran.stdout.length > 0
  } catch {
    return false
  }
}

// Reads alert-calibration.json from `dir` (at session start); anything wrong gives the defaults.
export async function loadCalibration($: Feed, ctx: AlertCtx, dir: string) {
  ctx.limits = defaultLimits()
  try {
    const path = `${dir}/${CALIBRATION_FILE}`
    const stat = await $.fs.stat(path)
    // The engine's FsStat: `kind` is what a link leads to ('file', 'dir' or 'other'), `isLink` says the path
    // itself is a link. A link, a directory or a file past the cap is not read, and neither is an answer that
    // does not say plainly it is none of them (`isLink` must be false, not merely absent): the defaults apply.
    if (stat.kind === 'file' && stat.isLink === false && stat.size <= MAX_CALIBRATION_BYTES && !(await isGitTracked($, dir))) {
      ctx.limits = parseCalibration(await $.fs.read(path))
    }
  } catch {
    // No file, or not readable: the defaults apply.
  }
}

// A `config.set` result for the switch: a refused change leaves it as it was.
async function applySwitch($: Feed, ctx: AlertCtx, result: { value?: unknown; deny?: unknown }) {
  try {
    if (result.deny === undefined && typeof result.value === 'boolean') {
      ctx.isEnabled = result.value
      if (ctx.current !== undefined) await evaluate($, ctx, ctx.current)
    }
  } catch {
    // Observation only.
  }
}

// Resolves when every write to the store that is under way has finished.
export async function alertsIdle(ctx: AlertCtx) {
  for (const pending of ctx.loading.values()) await (await pending).chain
}

// `/config` turns the alerts on or off without a reload. Returns nothing: the scan refuses a kept result.
export function registerAlerts(on: On, options: Readonly<Record<string, unknown>>, nowMs?: () => number): void {
  const ctx = makeAlertCtx(options, nowMs)
  active = ctx
  on('config.set', { key: SWITCH_KEY }, async ($, e, next) => {
    const result = await next(e)
    await applySwitch($, ctx, result)
    return result
  })
}

// What observer.ts calls: each does nothing before registration.
export async function alertTurnStart($: Feed, e: AgentEvent, at: number) {
  if (active !== undefined) await turnStart($, active, e, at)
}

export async function alertStepEnd($: Feed, e: AgentEvent, usage: unknown, startedAt: number, type: unknown) {
  if (active !== undefined) await stepEnd($, active, e, usage, startedAt, type)
}

export async function alertTurnEnd($: Feed, e: AgentEvent & { durationMs?: unknown }, at: number) {
  if (active !== undefined) await turnEnd($, active, e, at)
}

export async function alertTelemetry($: Feed, e: { to?: unknown; event?: unknown; attributes?: Record<string, unknown> }) {
  if (active !== undefined) await telemetry($, active, e)
}

// The shared `sub<N>` label of an agent in the current session's numbering: null when the alerts are not
// registered (the caller numbers for itself), undefined when the numbering has no label for the id.
export async function alertLabel($: Feed, id: string): Promise<string | undefined | null> {
  if (active === undefined) return null
  return (await slotFor($, active)).tracker.labelOf(id)
}

export async function alertTick($: Feed) {
  if (active !== undefined) await tick($, active)
}

export async function alertLoadCalibration($: Feed, dir: string) {
  if (active !== undefined) await loadCalibration($, active, dir)
}
