// The cold-return price of a prompt cache: what the next request pays when its
// cache has lapsed. A static copy of plugin/scripts/model_prices.py (list price, no network); a
// test fails when a row, ratio or the fast-mode set drifts, so
// a price change is one edit plus one failing test.
//
// Integer arithmetic only: a price is hundredths of a USD per million tokens, so the cost in cents
// is tokens x price x ratio / 1,000,000 with a whole-number ratio, rounded half up. The largest
// product (40 million tokens x 1,500 x 10) stays far below 2^53, so every step is exact.

type PriceRow = {
  model: string
  // USD per million input tokens, in hundredths (500 is 5.00).
  input: number
  // A prompt of more than `threshold` tokens uses `longInput` (the Haiku 5.5 card).
  threshold?: number
  longInput?: number
}

// Same order and ids as MODEL_PRICES in model_prices.py. One row per line: the parity test reads them.
const MODEL_PRICES: readonly PriceRow[] = [
  { model: 'claude-fable-5-2', input: 800 },
  { model: 'claude-fable-5-1', input: 1000 },
  { model: 'claude-fable-5', input: 1000 },
  { model: 'claude-mythos-5-2', input: 800 },
  { model: 'claude-mythos-5-1', input: 1000 },
  { model: 'claude-mythos-5', input: 1000 },
  { model: 'claude-opus-5-5', input: 400 },
  { model: 'claude-opus-5', input: 500 },
  { model: 'claude-opus-4-8', input: 500 },
  { model: 'claude-opus-4-7', input: 500 },
  { model: 'claude-opus-4-6', input: 500 },
  { model: 'claude-opus-4-5', input: 500 },
  { model: 'claude-opus-4-1', input: 1500 },
  { model: 'claude-opus-4', input: 1500 },
  { model: 'claude-sonnet-5-5', input: 200 },
  { model: 'claude-sonnet-5', input: 200 },
  { model: 'claude-sonnet-4-6', input: 300 },
  { model: 'claude-sonnet-4-5', input: 300 },
  { model: 'claude-sonnet-4', input: 300 },
  { model: 'claude-haiku-5-5', input: 10, threshold: 100_000, longInput: 50 },
  { model: 'claude-haiku-4-5', input: 100 },
  { model: 'claude-haiku-3-5', input: 80 },
]

// FAST_MODE_MODELS in model_prices.py.
const FAST_MODE_MODELS: ReadonlySet<string> = new Set([
  'claude-fable-5-2',
  'claude-mythos-5-2',
  'claude-opus-5-5',
  'claude-opus-5',
  'claude-opus-4-8',
])

// A cache write costs ratio x input: CACHE_WRITE_5M (1.25) and CACHE_WRITE_1H (2.0), as [numerator, denominator].
const CACHE_WRITE_5M: readonly [number, number] = [5, 4]
const CACHE_WRITE_1H: readonly [number, number] = [2, 1]
const FAST_MODE_FACTOR = 2

// A token count the mod trusts: a whole number from 0 to 10,000,000 (the probe's cap on one request).
export const MAX_TOKENS = 10_000_000

// What may follow a table id: nothing, a `-YYYYMMDD` snapshot and a `[...]` suffix (ASCII digits only;
// Python's `\d` also takes other scripts' digits, so an exotic id is `$?` here and priced there).
const ID_TAIL = /^(-[0-9]{8})?(\[[^\n]*\])?$/

export function priceRow(model: unknown): PriceRow | undefined {
  if (typeof model !== 'string') return undefined
  let best: PriceRow | undefined
  for (const row of MODEL_PRICES) {
    if (model.startsWith(row.model) && ID_TAIL.test(model.slice(row.model.length))) {
      if (best === undefined || row.model.length > best.model.length) best = row
    }
  }
  return best
}

type ColdUsage = {
  input: unknown
  cacheRead: unknown
  cacheWrite: unknown
  // The last response's output tokens: the next request sends them back as context (D11).
  output: unknown
}

function tokens(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= MAX_TOKENS ? value : undefined
}

// Half up to the cent: round(n / d) for whole n, d > 0.
function roundHalfUp(n: number, d: number): number {
  return Math.floor((2 * n + d) / (2 * d))
}

// Cents for a cold return, or undefined for a model the table does not list, a count that is not
// a plausible token count, or an unknown lifetime. `isFast` is `usage.speed === 'fast'`.
export function coldReturnCents(
  model: unknown,
  usage: ColdUsage,
  life: '5m' | '1h',
  isFast = false,
): number | undefined {
  const row = priceRow(model)
  const input = tokens(usage.input)
  const cacheRead = tokens(usage.cacheRead)
  const cacheWrite = tokens(usage.cacheWrite)
  const output = tokens(usage.output)
  if (row === undefined || input === undefined || cacheRead === undefined || cacheWrite === undefined || output === undefined) {
    return undefined
  }
  // The rate card follows the prompt length, which is the request's input without the output.
  const prompt = input + cacheRead + cacheWrite
  const rate = row.threshold !== undefined && row.longInput !== undefined && prompt > row.threshold ? row.longInput : row.input
  const [num, den] = life === '1h' ? CACHE_WRITE_1H : CACHE_WRITE_5M
  const fast = isFast && FAST_MODE_MODELS.has(row.model) ? FAST_MODE_FACTOR : 1
  return roundHalfUp((prompt + output) * rate * num * fast, 1_000_000 * den)
}

// The engine's own price (`estimated_cache_write_usd`, a float) in whole cents, half up; undefined
// for anything that is not a plausible amount.
export function usdToCents(usd: unknown): number | undefined {
  if (typeof usd !== 'number' || !Number.isFinite(usd) || usd < 0 || usd > 100_000) return undefined
  return roundHalfUp(Math.round(usd * 1_000_000), 10_000)
}

export function formatCents(cents: number | undefined): string {
  if (cents === undefined) return '$?'
  return `$${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`
}
