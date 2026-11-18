// The text of the cache line: a pure function of the tracker's views. It builds the text from a fixed
// vocabulary, the labels `main` and `sub<N>`, and numbers; no id, path, model name, prompt or tool text is
// ever passed in (a CacheView holds none), so a planted value cannot reach it. No engine call here.

import type { CacheView, Reason } from './cache'

// The budget of the whole text, in characters, as a parameter of `renderLine` (D14).
export const DEFAULT_BUDGET = 120
// Subagents shown by name; the rest go into `+N more` (D3).
export const MAX_SUBAGENTS = 3
// The width the engine adds in front of a mod's status line (a warning glyph and the plugin's name,
// seen on Claude Code 2.1.322; the exact width is UNVERIFIED and no other version was checked). The
// spec's budget counts the plugin's own text, so this is 0 until the operator's width probe (P3) measures it; `renderLine`
// subtracts it from the budget.
export const HARNESS_PREFIX_WIDTH = 0
const PREFIX = 'cache '
export const NOTHING_TEXT = 'cache: no request yet'
const SEPARATOR = ' | '
// The label of a subagent whose number is not known (a full numbering).
const NO_LABEL = 'sub?'

const REASON_TEXT: Record<Reason, string> = {
  'plan-limit': 'past plan limit: 5m cache',
  unknown: 'lifetime unknown: 5m assumed',
}
// The order the reasons are written in.
const REASON_ORDER: Reason[] = ['plan-limit', 'unknown']

const STATE_RANK = { expired: 0, urgent: 1, warm: 2 } as const

// Subagents (not main) in the order of the line: expired first by first-seen order, then urgent, then
// warm, each by least time left (first-seen order breaks a tie).
export function orderSubagents(views: readonly CacheView[]): CacheView[] {
  return views
    .filter(v => !v.isMain)
    .sort((a, b) => {
      const byState = STATE_RANK[a.state] - STATE_RANK[b.state]
      if (byState !== 0) return byState
      if (a.state === 'expired') return a.order - b.order
      return a.secondsLeft - b.secondsLeft || a.order - b.order
    })
}

function mainSegment(v: CacheView): string {
  const life = v.lifeLabel === undefined ? '' : `${v.lifeLabel}, `
  return `main ${life}${v.timeText}, cold ${v.priceText}`
}

function subSegment(v: CacheView): string {
  const base = `${v.label ?? NO_LABEL} ${v.lifeLabel ?? '5m'}, ${v.timeText}`
  return v.state === 'warm' ? base : `${base}, cold ${v.priceText}`
}

type RenderOptions = {
  // The most characters the text may take; the main segment and the reasons are never dropped.
  budget?: number
  // Characters the engine puts in front of the line; taken off the budget.
  prefixWidth?: number
}

// The line for the views at one repaint. Order: main, the reasons, up to 3 subagents, `+N more`. Too long:
// subagents go from the last into `+N more`, then the `cache ` prefix.
export function renderLine(views: readonly CacheView[], options: RenderOptions = {}): string {
  const budget = (options.budget ?? DEFAULT_BUDGET) - (options.prefixWidth ?? HARNESS_PREFIX_WIDTH)
  const main = views.find(v => v.isMain)
  const subs = orderSubagents(views)
  if (main === undefined && subs.length === 0) return NOTHING_TEXT
  const capped = subs.slice(0, MAX_SUBAGENTS)
  const reasons = new Set<Reason>()
  for (const v of main === undefined ? capped : [main, ...capped]) if (v.reason !== undefined) reasons.add(v.reason)
  const head = [...(main === undefined ? [] : [mainSegment(main)]), ...REASON_ORDER.filter(r => reasons.has(r)).map(r => REASON_TEXT[r])]
  const build = (shown: number, hasPrefix: boolean): string => {
    const parts = [...head, ...capped.slice(0, shown).map(subSegment)]
    if (subs.length > shown) parts.push(`+${subs.length - shown} more`)
    return (hasPrefix ? PREFIX : '') + parts.join(SEPARATOR)
  }
  for (let shown = capped.length; shown >= 0; shown--) {
    const text = build(shown, true)
    if (text.length <= budget) return text
  }
  return build(0, false)
}

// Whether the line's text can still change with time alone: a warm or urgent cache has a clock, and a
// stopped subagent goes at its expiry. Expired caches wait for the next event.
export function needsClock(views: readonly CacheView[]): boolean {
  return views.some(v => v.state !== 'expired' || v.isStopped)
}
