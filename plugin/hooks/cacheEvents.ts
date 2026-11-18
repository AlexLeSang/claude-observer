// Feeds the cache tracker (cache.ts) from the engine's events and paints its line (cacheLine.ts) with
// `$.ui.status`. It observes: every feed catches its own failure and returns nothing, observer.ts passes each
// event on unchanged, and there is no `ui.notify`, no toast, no network and no `$.fs.read`: the only other
// side effect is the read-only `tail` child process of lifetime.ts.
//
// The engine refuses a second unmatched handler of one event in the plugin, so (as alertEvents.ts does) every
// feed is a top-level function and observer.ts and panel.tsx call them from their own handlers; the only
// handler registered here is the `config.set` of the switch, which has a matcher.
//
// One repaint timer (module state): started by a repaint that finds a clock running, cancelled by a repaint
// that does not and by every new session, never started twice.

import type { On } from 'claude-code'
import type { Feed } from './feed'
import { CacheTracker, MAIN_KEY, REPAINT_SECONDS, agentKey, type StepUsage } from './cache'
import { DEFAULT_BUDGET, needsClock, renderLine } from './cacheLine'
import { alertLabel } from './alertEvents'
import { processTail, readLifetime, subagentTranscript, type TailRead } from './lifetime'
import { SubLabels } from './subLabels'

export const SWITCH_KEY = 'usage-observer.cacheLine'

type Engine = Feed

type CacheCtx = {
  tracker: CacheTracker
  // A clock in seconds for a test; in production the engine's `$.clock.now()` (milliseconds since the epoch).
  now: (() => number) | undefined
  // The tail read for one engine; the default runs `tail -c` through `$.process.run`.
  tailFor?: () => TailRead
  // The line's width budget, in characters.
  budget: number
  isEnabled: boolean
  // The main transcript's path, from a classic hook input. Only reading needs it; it is in no view.
  mainPath: string | undefined
  // Labels for the case that the alerts' numbering is not there to ask.
  ownLabels: SubLabels
  timer: { cancel: () => void } | undefined
  // Seconds (the `now` clock) the timer was set and the last tick came: a timer the engine refused hands back a
  // handle that never ticks, and the next repaint then sets it again.
  timerSetAt: number
  lastTickAt: number | undefined
}

// Session starts that begin a new conversation; `compact` keeps the session going.
const NEW_SESSION = new Set(['startup', 'resume', 'clear', 'fork'])

// The module's context, set by `registerCacheLine`; each feed below does nothing before that.
let active: CacheCtx | undefined

function makeCacheCtx(
  options: Readonly<Record<string, unknown>>,
  deps: { tracker?: CacheTracker; now?: () => number; tailFor?: () => TailRead; budget?: number } = {},
): CacheCtx {
  return {
    tracker: deps.tracker ?? new CacheTracker(),
    now: deps.now,
    tailFor: deps.tailFor,
    budget: deps.budget ?? DEFAULT_BUDGET,
    isEnabled: options.cacheLine !== false && options.cacheLine !== 'false',
    mainPath: undefined,
    ownLabels: new SubLabels(),
    timer: undefined,
    timerSetAt: 0,
    lastTickAt: undefined,
  }
}

function stopTimer(ctx: CacheCtx) {
  const timer = ctx.timer
  ctx.timer = undefined
  try {
    timer?.cancel()
  } catch {
    // Observation only.
  }
}

// A timer that has not ticked in more than two periods past its last sign of life was refused (the engine hands
// back a handle that does nothing): it is set again by the next repaint.
function isTimerSilent(ctx: CacheCtx, nowSeconds: number): boolean {
  return nowSeconds - (ctx.lastTickAt ?? ctx.timerSetAt) > 2 * REPAINT_SECONDS
}

// The time now in seconds: the engine's clock (a test can hold it), else this machine's, which is the same epoch.
async function secondsNow($: Engine, ctx: CacheCtx): Promise<number> {
  if (ctx.now !== undefined) return ctx.now()
  try {
    const ms = await $.clock.now()
    if (Number.isFinite(ms)) return ms / 1000
  } catch {
    // A refused read: this machine's clock.
  }
  return Date.now() / 1000
}

// Paints the line now and starts or stops the timer by whether a clock is running. Off: nothing.
async function paint($: Engine, ctx: CacheCtx) {
  if (!ctx.isEnabled) return
  const nowSeconds = await secondsNow($, ctx)
  // The switch may have turned off while the clock was read.
  if (!ctx.isEnabled) return
  let isClockNeeded = false
  try {
    const views = ctx.tracker.view(nowSeconds)
    $.ui.status(renderLine(views, { budget: ctx.budget }))
    isClockNeeded = needsClock(views)
  } catch {
    // Observation only: the last text stays.
  }
  try {
    if (isClockNeeded && ctx.timer !== undefined && isTimerSilent(ctx, nowSeconds)) stopTimer(ctx)
    if (isClockNeeded && ctx.timer === undefined) {
      ctx.timerSetAt = nowSeconds
      ctx.lastTickAt = undefined
      ctx.timer = $.clock.every(REPAINT_SECONDS * 1000, () => {
        void cacheTick($).catch(() => undefined)
      })
    } else if (!isClockNeeded) {
      stopTimer(ctx)
    }
  } catch {
    // Observation only.
  }
}

function tailOf(ctx: CacheCtx, $: Engine): TailRead {
  return (
    ctx.tailFor?.() ??
    processTail(async (argv, timeoutMs) => {
      const ran = await $.process.run(argv, { timeoutMs })
      return { exitCode: ran.exitCode, stdout: ran.stdout, isStdoutTruncated: ran.isStdoutTruncated }
    })
  )
}

function rememberPath(ctx: CacheCtx, input: unknown) {
  const path = (input as { transcript_path?: unknown } | null)?.transcript_path
  if (typeof path === 'string') ctx.mainPath = path
}

// The label of a subagent: the alerts' numbering when there is one (so both lines name an agent alike),
// else this module's own.
// `feed` is not spelled `$`: it goes to an imported function, and the engine's scan follows `$` only within a file.
async function labelFor(feed: Engine, ctx: CacheCtx, id: string): Promise<string | undefined> {
  let shared: string | undefined | null = null
  try {
    shared = await alertLabel(feed, id)
  } catch {
    shared = null
  }
  return shared === null ? ctx.ownLabels.label(id) : shared
}

type SessionStartInput = {
  source?: unknown
  transcript_path?: unknown
  prompt_cache_likely_expired?: unknown
  estimated_cache_write_usd?: unknown
}

// A new conversation resets the tracker, the timer and the numbering, seeds a resume, and paints.
export async function cacheSessionStart($: Engine, e: SessionStartInput) {
  const ctx = active
  if (ctx === undefined) return
  try {
    if (typeof e.source === 'string' && NEW_SESSION.has(e.source)) {
      stopTimer(ctx)
      ctx.tracker.reset()
      ctx.ownLabels = new SubLabels()
      ctx.mainPath = undefined
      rememberPath(ctx, e)
      // The fields exist on a resume or fork only.
      if (e.source === 'resume' || e.source === 'fork') ctx.tracker.seedResume(e)
    }
  } catch {
    // Observation only.
  }
  await paint($, ctx)
}

export async function cacheUserPrompt(e: unknown) {
  if (active === undefined) return
  try {
    rememberPath(active, e)
  } catch {
    // Observation only.
  }
}

// The prompt's own time is taken before the engine runs it: never later than its first request.
export async function cachePromptSubmit($: Engine) {
  const ctx = active
  if (ctx === undefined) return
  try {
    ctx.tracker.promptSubmitted(await secondsNow($, ctx))
  } catch {
    // Observation only.
  }
  await paint($, ctx)
}

// The parent's spawn call: `cacheSpawnTime` before the engine runs it, `cacheSpawned` with its answer.
export async function cacheSpawnTime($: Engine): Promise<number | undefined> {
  return active === undefined ? undefined : secondsNow($, active)
}

export async function cacheSpawned(at: number | undefined, result: { agentId?: unknown }) {
  const ctx = active
  if (ctx === undefined || at === undefined) return
  try {
    if (typeof result.agentId === 'string') ctx.tracker.noteBegin(agentKey(result.agentId), at)
  } catch {
    // Observation only.
  }
}

// The clock the feeds use, in milliseconds: observer.ts takes a step's end from it, so the tracker has one clock.
export async function cacheClockMs($: Engine): Promise<number> {
  return active === undefined ? Date.now() : (await secondsNow($, active)) * 1000
}

// True when the engine says `path` exists and is not a regular file (a FIFO or a directory): a `tail` on a FIFO
// with no writer blocks until its timeout (3 s a step). A failed stat answers false, so a missing
// or unreadable file is left to the `tail` read, which already answers 'unknown' for it.
async function isNotFile($: Engine, path: string | undefined): Promise<boolean> {
  if (path === undefined) return false
  try {
    const stat = await $.fs.stat(path)
    return stat.kind !== 'file'
  } catch {
    return false
  }
}

// A step ended at `endedAtMs` (the caller's clock when the response arrived, before its other work).
export async function cacheStepEnd($: Engine, e: { agentId?: string }, usage: StepUsage | null, endedAtMs: number) {
  const ctx = active
  if (ctx === undefined) return
  try {
    const id = e.agentId
    const key = id === undefined ? MAIN_KEY : agentKey(id)
    if (!ctx.isEnabled) {
      // Off: no `tail` child and no cache kept, only the loop's begin (null usage), so a switch back on is right.
      await ctx.tracker.stepEnd(key, endedAtMs / 1000, null, async () => 'unknown')
      ctx.tracker.forget(key)
      return
    }
    const path = id === undefined ? ctx.mainPath : subagentTranscript(ctx.mainPath, id)
    const tail = tailOf(ctx, $)
    await ctx.tracker.stepEnd(key, endedAtMs / 1000, usage, async () =>
      (await isNotFile($, path)) ? 'unknown' : readLifetime(path, tail),
    )
    if (id !== undefined) ctx.tracker.setLabel(key, await labelFor($, ctx, id))
  } catch {
    // Observation only.
  }
  await paint($, ctx)
}

// Reads in flight that `cacheStepEndSoon` started: the line paints when each one resolves.
const pending = new Set<Promise<void>>()

// `cacheStepEnd` without the wait: the begin and end are recorded before this returns, and the transcript read
// (up to 7 `tail` runs of 3 s) and the paint follow on their own, so the model step is never held by them
// (the stale-read guard of the tracker still drops a read a newer step of the loop overtook).
export async function cacheStepEndSoon($: Engine, e: { agentId?: string }, usage: StepUsage | null, endedAtMs: number): Promise<void> {
  const work: Promise<void> = cacheStepEnd($, e, usage, endedAtMs)
    .catch(() => undefined)
    .finally(() => void pending.delete(work))
  pending.add(work)
}

// Resolves when every read `cacheStepEndSoon` started has finished and painted (a test; nothing in production calls it).
export async function cacheIdle(): Promise<void> {
  while (pending.size > 0) await Promise.all([...pending])
}

// The main turn is over: the next prompt, not the last step's end, begins the next request.
export async function cacheTurnComplete(e: { agentId?: unknown }) {
  if (active === undefined) return
  try {
    if (e.agentId === undefined) active.tracker.turnEnded()
  } catch {
    // Observation only.
  }
}

export async function cacheSubagentStop($: Engine, e: { agent_id?: unknown }) {
  const ctx = active
  if (ctx === undefined) return
  try {
    if (typeof e.agent_id === 'string') ctx.tracker.subagentStopped(agentKey(e.agent_id), await secondsNow($, ctx))
  } catch {
    // Observation only.
  }
  await paint($, ctx)
}

export async function cacheMeasure($: Engine, e: { rateLimits: readonly { percentUsed?: unknown }[] }) {
  const ctx = active
  if (ctx === undefined) return
  try {
    ctx.tracker.setMeasure(e.rateLimits.map(r => r.percentUsed))
  } catch {
    // Observation only.
  }
  await paint($, ctx)
}

// A tool call repaints (the status spike's cadence); nothing in the call is read.
export async function cacheToolCall($: Engine) {
  if (active !== undefined) await paint($, active)
}

// The timer's tick.
export async function cacheTick($: Engine) {
  if (active === undefined) return
  active.lastTickAt = await secondsNow($, active)
  await paint($, active)
}

// A `config.set` result for the switch: a denied or non-boolean change leaves it as it was. Off clears the
// line once and stops the timer; on paints again.
async function applySwitch($: Engine, ctx: CacheCtx, result: { value?: unknown; deny?: unknown }) {
  try {
    if (result.deny !== undefined || typeof result.value !== 'boolean' || result.value === ctx.isEnabled) return
    ctx.isEnabled = result.value
    if (ctx.isEnabled) {
      await paint($, ctx)
      return
    }
    stopTimer(ctx)
    // Whether the engine's screen clears on `undefined` is UNVERIFIED (the operator's probe steps 2-4 were not run).
    $.ui.status(undefined)
  } catch {
    // Observation only.
  }
}

// `/config` turns the line on or off without a reload. Returns nothing: the scan refuses a kept result.
export function registerCacheLine(on: On, options: Readonly<Record<string, unknown>>, deps?: Parameters<typeof makeCacheCtx>[1]): void {
  const ctx = makeCacheCtx(options, deps)
  active = ctx
  on('config.set', { key: SWITCH_KEY }, async ($, e, next) => {
    const result = await next(e)
    await applySwitch($, ctx, result)
    return result
  })
}
