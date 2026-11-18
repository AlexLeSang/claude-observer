// Where a loop's cache lifetime comes from (slice 1, LS D1-D4, D10): the last response's
// `usage.cache_creation` split in the loop's own transcript, read again after every step and never
// kept. The rule is the engine's: any 5-minute write is 5m, else any 1-hour write is 1h, else unknown.
// The transcript is read from its end only, by a boundary the caller injects (`TailRead`): the
// production one runs `tail -c` through `$.process.run` (UNVERIFIED that a mod may spawn it, U1) and
// there is no whole-file read here, so the 4 MiB `$.fs.read` bar never applies.
//
// What is kept from a row is three numbers' worth of class: no prompt, tool text, path or id.

export type Lifetime = '5m' | '1h' | 'unknown'

// A count is a finite number from 0 to 10,000,000 (the probe's cap on one request's tokens); anything
// else (a string, `true`, `null`, a negative or larger number) counts as absent, that is as 0. On
// purpose unlike the engine's JavaScript comparison, where `true > 0` and `"5000" > 0` hold.
const MAX_COUNT = 10_000_000

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= MAX_COUNT ? value : 0
}

export function classifySplit(fiveMinute: unknown, oneHour: unknown): Lifetime {
  if (count(fiveMinute) > 0) return '5m'
  return count(oneHour) > 0 ? '1h' : 'unknown'
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

// The class of one transcript line, or undefined when it is not a complete assistant row carrying a
// `usage.cache_creation` object (torn, not JSON, another kind of row, no split to read).
function lifetimeOfLine(line: string): Lifetime | undefined {
  // Cheap filter first: a tail is mostly tool results and user rows, which are never parsed.
  if (!line.includes('"assistant"')) return undefined
  let row: unknown
  try {
    row = JSON.parse(line)
  } catch {
    return undefined
  }
  if (!isObject(row) || row.type !== 'assistant' || !isObject(row.message)) return undefined
  const usage = row.message.usage
  const split = isObject(usage) ? usage.cache_creation : undefined
  if (!isObject(split)) return undefined
  return classifySplit(split.ephemeral_5m_input_tokens, split.ephemeral_1h_input_tokens)
}

// The last complete assistant row's class in `text`, the end of a transcript; undefined when none
// carries a split. `isCut`: the text starts mid-file, so its first line may be torn and is not read.
export function lifetimeFromTail(text: string, isCut: boolean): Lifetime | undefined {
  const lines = text.split('\n')
  for (let i = lines.length - 1; i >= (isCut ? 1 : 0); i--) {
    const found = lifetimeOfLine(lines[i] as string)
    if (found !== undefined) return found
  }
  return undefined
}

export const FIRST_WINDOW = 64 * 1024
export const LAST_WINDOW = 4 * 1024 * 1024

// The last `bytes` bytes of the file at `path` as text, or undefined when it could not be read
// (missing, refused, cut short by the output cap, timed out). The boundary a test replaces.
export type TailRead = (path: string, bytes: number) => Promise<string | undefined>

const encoder = new TextEncoder()

// The lifetime in force for the loop whose transcript is `path`, from the last response's split alone:
// a 64 KiB tail, doubled up to 4 MiB while no complete row with a split is in it. Never throws: a
// failed read or a transcript with no readable split is 'unknown', not the previous answer.
export async function readLifetime(path: string | undefined, tail: TailRead): Promise<Lifetime> {
  if (path === undefined) return 'unknown'
  for (let window = FIRST_WINDOW; ; window *= 2) {
    let text: string | undefined
    try {
      text = await tail(path, window)
    } catch {
      return 'unknown'
    }
    if (typeof text !== 'string') return 'unknown'
    // A text shorter than the window is the whole file, so its first line is not torn. Invalid UTF-8
    // decodes to longer text, never shorter, so a doubt only drops one line.
    const isCut = encoder.encode(text).length >= window
    const found = lifetimeFromTail(text, isCut)
    if (found !== undefined) return found
    if (!isCut || window >= LAST_WINDOW) return 'unknown'
  }
}

// Runs one argv and answers its exit code and output: the caller closes over `$`.
export type Run = (
  argv: string[],
  timeoutMs: number,
) => Promise<{ exitCode: number; stdout: string; stderr?: string; isStdoutTruncated?: boolean }>

const TAIL_TIMEOUT_MS = 3000

// An absolute path (POSIX or Windows drive) to a .jsonl file, no NUL: the only thing handed to `tail`.
function isReadablePath(path: string): boolean {
  return (/^\//.test(path) || /^[A-Za-z]:[\\/]/.test(path)) && path.endsWith('.jsonl') && !path.includes('\0')
}

// `tail -c <bytes> -- <path>` through the injected runner. Anything but a clean, whole answer (a refused
// or failed run, a non-zero exit, output cut at the engine's cap, a path that is not a .jsonl) is undefined.
export function processTail(run: Run): TailRead {
  return async (path, bytes) => {
    if (!isReadablePath(path)) return undefined
    try {
      const ran = await run(['tail', '-c', String(bytes), '--', path], TAIL_TIMEOUT_MS)
      if (ran.exitCode !== 0 || ran.isStdoutTruncated === true || typeof ran.stdout !== 'string') return undefined
      return ran.stdout
    } catch {
      return undefined
    }
  }
}

const AGENT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/

// `<session folder>/subagents/agent-<id>.jsonl` beside the main transcript `<session folder>.jsonl`
// (LS D10). UNVERIFIED on a live session that the step's agent id is the file's `<id>` (U4), so a
// caller may replace this join; an id that is not a plain token gets no path (never a `..`).
export function subagentTranscript(mainPath: string | undefined, agentId: unknown): string | undefined {
  if (mainPath === undefined || !mainPath.endsWith('.jsonl') || typeof agentId !== 'string' || !AGENT_ID.test(agentId)) {
    return undefined
  }
  return `${mainPath.slice(0, -'.jsonl'.length)}/subagents/agent-${agentId}.jsonl`
}
