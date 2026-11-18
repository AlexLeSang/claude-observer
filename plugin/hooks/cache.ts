// The cache tracker: for the main session and each subagent, one record with
// the lifetime in force, the expiry, the cold-return price, and from them a state and the values a
// line would show. Pure and clock-free: every time is a number of seconds the caller passes in, and
// the lifetime comes from a reader the caller passes per step (lifetime.ts). It draws nothing and
// calls no engine API; the status line is slice 2.
//
// Held per cache: the loop's key, a first-seen number, the lifetime class, the expiry, the price in
// cents and whether the lifetime is assumed. No path, id (the key is an opaque map key and is not in a
// view), model name, prompt, tool text or subagent description.

import { coldReturnCents, formatCents, usdToCents } from './prices'
import type { Lifetime } from './lifetime'

export const MAIN_KEY = 'main'
export function agentKey(agentId: string): string {
  return `agent:${agentId}`
}

type Life = '5m' | '1h'
const TTL_SECONDS: Record<Life, number> = { '5m': 300, '1h': 3600 }
// Urgent from this long before expiry, on the cache's own lifetime (D4).
const URGENT_LEAD_SECONDS: Record<Life, number> = { '5m': 60, '1h': 600 }
// The repaint period of the idle clock (D5): every shown value is for the next repaint.
export const REPAINT_SECONDS = 5
// The plan-limit window percentage at which the reason can show (D8 C).
const PLAN_LIMIT_PERCENT = 100

type CacheState = 'warm' | 'urgent' | 'expired'

// Warm, urgent (at most the lead left) or expired (`now` at or past the expiry). A pure function of
// (expiry, lifetime, now).
export function cacheState(expiry: number, life: Life, now: number): CacheState {
  if (now >= expiry) return 'expired'
  return expiry - now <= URGENT_LEAD_SECONDS[life] ? 'urgent' : 'warm'
}

// Whole minutes from 10 minutes up, otherwise m:ss; never rounds up.
export function formatLeft(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  if (whole >= 600) return `${Math.floor(whole / 60)}m`
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}

export type StepUsage = {
  input_tokens: unknown
  output_tokens: unknown
  cache_read_input_tokens: unknown
  cache_creation_input_tokens: unknown
  model?: unknown
  speed?: unknown
}

type Cache = {
  isMain: boolean
  order: number
  life: Life
  isAssumed: boolean
  // A resumed session's seed: expired whatever the clock says, no lifetime known.
  isSeed: boolean
  expiry: number
  priceCents: number | undefined
  // When a subagent's stop was seen; a stopped one is dropped once its cache expires.
  stoppedAt: number | undefined
  // The subagent's `sub<N>` label (subLabels.ts), set by the line's adapter; never an id.
  label: string | undefined
}

export type Reason = 'plan-limit' | 'unknown'

// What a line would show for one cache at one repaint instant. Labels and numbers only.
export type CacheView = {
  isMain: boolean
  // First-seen order among the caches of this session (the label is `label`).
  order: number
  // `5m`, `1h`, `5m?` (assumed), or undefined for a resume seed.
  lifeLabel: '5m' | '1h' | '5m?' | undefined
  // The state at the next repaint: `now` plus the margin.
  state: CacheState
  // The real expiry, and the moment urgent starts, in seconds.
  expiresAt: number
  urgentAt: number
  // Shown seconds left: expiry - now - margin, floored; 0 once expired.
  secondsLeft: number
  // `59m left`, `4:10 left`, `URGENT 1:00 left` or `EXPIRED`.
  timeText: string
  // `$0.63`, or `$?` for a model the table does not list.
  priceText: string
  // `past plan limit: 5m cache` or `lifetime unknown: 5m assumed` applies (the line's wording is slice 2's).
  reason: Reason | undefined
  isStopped: boolean
  // `sub<N>` for a subagent whose label is known; undefined for main and for an unlabelled subagent.
  label: string | undefined
}

export class CacheTracker {
  private caches = new Map<string, Cache>()
  // Per loop, the earliest time seen for its next request: the previous step's end, the prompt's
  // submission or the parent's spawn call. Absent means the loop is not tracked until its next step.
  private begins = new Map<string, number>()
  private reads = new Map<string, number>()
  // When each subagent's stop was seen: a read in flight when it came must not wipe it, and a first read that
  // lands after the stop has no cache yet to carry it.
  private stops = new Map<string, number>()
  // True once the main loop's turn is over: only then does a prompt's time replace the noted begin.
  private isMainIdle = false
  private readCount = 0
  private nextOrder = 0
  private maxPercent: number | undefined

  // A new session: nothing carries over.
  reset(): void {
    this.caches.clear()
    this.begins.clear()
    this.reads.clear()
    this.stops.clear()
    this.isMainIdle = false
    this.maxPercent = undefined
    this.nextOrder = 0
  }

  // The latest `session.measure`'s windows (percent used each). Not a lifetime: it changes only
  // whether the reason can show (D8 C); one that is not a finite number is ignored.
  setMeasure(percents: readonly unknown[]): void {
    let highest: number | undefined
    for (const p of percents) {
      if (typeof p === 'number' && Number.isFinite(p) && (highest === undefined || p > highest)) highest = p
    }
    this.maxPercent = highest
  }

  // The loop's next request begins no earlier than `at`; the earliest time noted since the last step wins.
  noteBegin(key: string, at: number): void {
    if (Number.isFinite(at) && !this.begins.has(key)) this.begins.set(key, at)
  }

  // The main loop's turn finished: nothing is in flight, so the next prompt starts the next request.
  turnEnded(): void {
    this.isMainIdle = true
  }

  // A prompt entered the main session. While a turn runs (a step may be in flight, the prompt queued) the
  // earliest begin stays: moving it forward would show more time than there is. Once the turn has ended, or
  // when no begin is known, the prompt's time is the next request's begin, and the finished turn's last step
  // end no longer says when it begins.
  promptSubmitted(at: number): void {
    if (!Number.isFinite(at)) return
    if (this.begins.has(MAIN_KEY) && !this.isMainIdle) return
    this.begins.set(MAIN_KEY, at)
    this.isMainIdle = false
  }

  // A step of the loop `key` ended at `endAt`. `usage` is the response's (null: no response, nothing
  // changes); `readLifetime` is read once, after the step, and is the lifetime's only source. A step
  // whose begin was never seen leaves the loop untracked until its next one.
  async stepEnd(key: string, endAt: number, usage: StepUsage | null, readLifetime: () => Promise<Lifetime>): Promise<void> {
    if (!Number.isFinite(endAt)) return
    // A step of the main loop means a turn is running.
    if (key === MAIN_KEY) this.isMainIdle = false
    const begin = this.begins.get(key)
    // The next request begins no earlier than this step's end.
    this.begins.set(key, endAt)
    if (usage === null || begin === undefined) return
    const mine = ++this.readCount
    this.reads.set(key, mine)
    let read: Lifetime
    try {
      read = await readLifetime()
    } catch {
      read = 'unknown'
    }
    // A newer step of this loop, or a reset, took over while the read ran.
    if (this.reads.get(key) !== mine) return
    const isAssumed = read === 'unknown'
    const life: Life = read === '1h' ? '1h' : '5m'
    const isFast = usage.speed === 'fast'
    const priceCents = coldReturnCents(
      usage.model,
      {
        input: usage.input_tokens,
        cacheRead: usage.cache_read_input_tokens,
        cacheWrite: usage.cache_creation_input_tokens,
        output: usage.output_tokens,
      },
      life,
      isFast,
    )
    const before = this.caches.get(key)
    // A stop seen at or after this step's end still holds; a later step (a resumed run) clears it.
    const stop = this.stops.get(key)
    const isStopped = stop !== undefined && stop >= endAt
    if (!isStopped) this.stops.delete(key)
    this.caches.set(key, {
      isMain: key === MAIN_KEY,
      order: before?.order ?? this.nextOrder++,
      life,
      isAssumed,
      isSeed: false,
      expiry: Math.min(begin, endAt) + TTL_SECONDS[life],
      priceCents,
      stoppedAt: isStopped ? stop : undefined,
      label: before?.label,
    })
  }

  // The loop's cache is not kept (the line is off): its begin stays, so the first step after a switch back on is
  // tracked. A read in flight for it is dropped.
  forget(key: string): void {
    this.caches.delete(key)
    this.reads.delete(key)
    this.stops.delete(key)
  }

  // The label the shared numbering gave this subagent (only `sub` and digits is kept). A cache that is
  // not tracked yet or is the main one takes none.
  setLabel(key: string, label: string | undefined): void {
    const cache = this.caches.get(key)
    if (cache === undefined || cache.isMain) return
    cache.label = label !== undefined && /^sub[1-9][0-9]{0,6}$/.test(label) ? label : undefined
  }

  // The engine said the subagent stopped. It stays until its cache expires (a resumed run can read it).
  subagentStopped(key: string, at: number): void {
    if (key === MAIN_KEY || !Number.isFinite(at)) return
    this.stops.set(key, at)
    const cache = this.caches.get(key)
    if (cache !== undefined && !cache.isMain) cache.stoppedAt = at
  }

  // D12: a resumed or forked session whose input says its prompt cache is likely expired shows the
  // main cache as expired with the engine's own price; anything else (false, absent, not a boolean)
  // shows nothing. The caller passes the classic SessionStart input's two fields.
  seedResume(fields: { prompt_cache_likely_expired?: unknown; estimated_cache_write_usd?: unknown }): void {
    if (fields.prompt_cache_likely_expired !== true) return
    this.caches.set(MAIN_KEY, {
      isMain: true,
      order: this.nextOrder++,
      life: '5m',
      isAssumed: false,
      isSeed: true,
      expiry: -Infinity,
      priceCents: usdToCents(fields.estimated_cache_write_usd),
      stoppedAt: undefined,
      label: undefined,
    })
  }

  // The caches a line would show at `now`, main first and then in first-seen order; the ordering and
  // the cap of the line are slice 2's. A stopped subagent whose cache has expired is gone.
  view(now: number, margin: number = REPAINT_SECONDS): CacheView[] {
    if (!Number.isFinite(now) || !Number.isFinite(margin)) return []
    const out: CacheView[] = []
    for (const cache of this.caches.values()) {
      if (cache.stoppedAt !== undefined && now >= cache.expiry) continue
      const state = cache.isSeed ? 'expired' : cacheState(cache.expiry, cache.life, now + margin)
      const secondsLeft = state === 'expired' ? 0 : Math.max(0, Math.floor(cache.expiry - now - margin))
      const left = formatLeft(secondsLeft)
      let reason: Reason | undefined
      if (cache.isAssumed) reason = 'unknown'
      else if (!cache.isSeed && cache.life === '5m' && this.maxPercent !== undefined && this.maxPercent >= PLAN_LIMIT_PERCENT) {
        reason = 'plan-limit'
      }
      out.push({
        isMain: cache.isMain,
        order: cache.order,
        lifeLabel: cache.isSeed ? undefined : cache.isAssumed ? '5m?' : cache.life,
        state,
        expiresAt: cache.expiry,
        urgentAt: cache.expiry - URGENT_LEAD_SECONDS[cache.life],
        secondsLeft,
        timeText: state === 'expired' ? 'EXPIRED' : state === 'urgent' ? `URGENT ${left} left` : `${left} left`,
        priceText: formatCents(cache.priceCents),
        reason,
        isStopped: cache.stoppedAt !== undefined,
        label: cache.label,
      })
    }
    return out.sort((a, b) => Number(b.isMain) - Number(a.isMain) || a.order - b.order)
  }
}
