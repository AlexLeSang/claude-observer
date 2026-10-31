// Live totals for /observer's "This session" tab: pure
// reducers over the same facts the capture records (usage per model request,
// tool calls, skills, context and cost), kept apart so tests can call them
// without an engine. Names, counts and sizes only, never prompt or tool content.
// The same counting rules as scripts/mod_coverage.py: a step counts when it
// carries usage, a tool call fails when it errored or was denied.

import type { LiveTotals, TokenTally } from '../types'

export const SPARK_POINTS = 60

export function emptyTally(): TokenTally {
  return { input: 0, output: 0, cacheRead: 0, cacheCreation: 0, steps: 0 }
}

export function emptyLive(): LiveTotals {
  return {
    models: {},
    subagents: {},
    tools: {},
    skills: {},
    contextPercent: null,
    contextTokens: null,
    contextWindow: null,
    costUsd: null,
    fill: [],
  }
}

type StepUsage = {
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
  model: string
}

// One finished model request. `agentType` is set for a subagent's request.
export function applyStep(
  live: LiveTotals,
  usage: StepUsage | null,
  agentId: string | undefined,
  agentType: string | undefined,
): LiveTotals {
  if (usage === null) return live
  const add = (tally: TokenTally | undefined): TokenTally => {
    const t = tally ?? emptyTally()
    return {
      input: t.input + usage.input_tokens,
      output: t.output + usage.output_tokens,
      cacheRead: t.cacheRead + usage.cache_read_input_tokens,
      cacheCreation: t.cacheCreation + usage.cache_creation_input_tokens,
      steps: t.steps + 1,
    }
  }
  const next = { ...live, models: { ...live.models, [usage.model]: add(live.models[usage.model]) } }
  if (agentId !== undefined) {
    const type = agentType ?? 'unknown'
    next.subagents = { ...live.subagents, [type]: add(live.subagents[type]) }
  }
  return next
}

// One finished tool call: a denied or errored call is a failure.
export function applyTool(live: LiveTotals, tool: string, failed: boolean, skill: string | null): LiveTotals {
  const row = live.tools[tool] ?? { calls: 0, failures: 0 }
  const next = {
    ...live,
    tools: { ...live.tools, [tool]: { calls: row.calls + 1, failures: row.failures + (failed ? 1 : 0) } },
  }
  // A Skill tool call names its skill; the activation itself counts in applySkill.
  if (skill !== null && live.skills[skill] === undefined) next.skills = { ...live.skills, [skill]: 0 }
  return next
}

export function applySkill(live: LiveTotals, skill: string): LiveTotals {
  return { ...live, skills: { ...live.skills, [skill]: (live.skills[skill] ?? 0) + 1 } }
}

type Measure = {
  context: { tokens?: number; window?: number; percent?: number }
  cost?: { usd?: number }
}

export function applyMeasure(live: LiveTotals, e: Measure): LiveTotals {
  const percent = e.context.percent ?? null
  const fill = percent === null ? live.fill : [...live.fill, percent].slice(-SPARK_POINTS)
  return {
    ...live,
    contextPercent: percent,
    contextTokens: e.context.tokens ?? null,
    contextWindow: e.context.window ?? null,
    costUsd: e.cost?.usd ?? live.costUsd,
    fill,
  }
}

export function sumTokens(t: TokenTally) {
  return t.input + t.output + t.cacheRead + t.cacheCreation
}

export function totalTally(rows: Record<string, TokenTally>): TokenTally {
  const total = emptyTally()
  for (const t of Object.values(rows)) {
    total.input += t.input
    total.output += t.output
    total.cacheRead += t.cacheRead
    total.cacheCreation += t.cacheCreation
    total.steps += t.steps
  }
  return total
}

// Cache reads over everything the requests read in: the share served from cache.
export function cacheHitRate(t: TokenTally): number | null {
  const read = t.input + t.cacheRead + t.cacheCreation
  return read === 0 ? null : t.cacheRead / read
}

const BLOCKS = '▁▂▃▄▅▆▇█'

// Context fill over the session as one line of block characters (0-100 %).
export function sparkline(fill: readonly number[]): string {
  return fill
    .map(p => BLOCKS[Math.min(BLOCKS.length - 1, Math.max(0, Math.floor((p / 100) * BLOCKS.length)))])
    .join('')
}

export function topTools(live: LiveTotals, by: 'calls' | 'failures', n: number) {
  return Object.entries(live.tools)
    .filter(([, row]) => row[by] > 0)
    .sort((a, b) => b[1][by] - a[1][by] || a[0].localeCompare(b[0]))
    .slice(0, n)
}

export function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${Math.round(n / 1000)}k`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}
