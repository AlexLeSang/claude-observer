// The Python interpreter behind the appender and the Project tab's reports.
//
// `python3` is not on every PATH: Windows has `python` or the `py -3` launcher, and the
// Microsoft Store stub answers `python3` with a non-zero exit. So the plugin probes
// each candidate by running it with --version, once, and holds the first that is 3.10
// or newer (pyproject.toml's requires-python).

// Runs one argv and answers its exit code and output: the caller closes over `$`, which
// a hooks module may not pass across an import.
export type Run = (argv: string[], timeoutMs: number) => Promise<{ exitCode: number; stdout: string; stderr: string }>

export const PYTHON_CANDIDATES: readonly (readonly string[])[] = [['python3'], ['python'], ['py', '-3']]
export const MIN_PYTHON = { major: 3, minor: 10 }
const PROBE_TIMEOUT_MS = 3000
const MIN_PROBE_MS = 100

let found: readonly string[] | undefined

export function pythonNames() {
  return PYTHON_CANDIDATES.map(argv => argv.join(' ')).join(', ')
}

export function isSupported(versionText: string) {
  const match = /^Python (\d+)\.(\d+)/.exec(versionText.trim())
  if (match === null) return false
  const [major, minor] = [Number(match[1]), Number(match[2])]
  return major > MIN_PYTHON.major || (major === MIN_PYTHON.major && minor >= MIN_PYTHON.minor)
}

// The interpreter's argv prefix (`['python3']`, `['py', '-3']`), or undefined when none
// qualifies. A hit is kept for the process; a miss is probed again on the next call.
// `budgetMs` bounds all the probes together (a hook with a deadline), each one at most 3 s.
export async function findPython(run: Run, budgetMs?: number): Promise<readonly string[] | undefined> {
  if (found !== undefined) return found
  const deadline = budgetMs === undefined ? Infinity : Date.now() + budgetMs
  for (const argv of PYTHON_CANDIDATES) {
    const left = deadline - Date.now()
    if (left < MIN_PROBE_MS) break
    try {
      const ran = await run([...argv, '--version'], Math.min(PROBE_TIMEOUT_MS, left))
      // Python 3 prints the version on stdout; Python 2 and some builds use stderr.
      if (ran.exitCode === 0 && isSupported(ran.stdout || ran.stderr)) {
        found = argv
        return found
      }
    } catch {
      // Not installed, or not runnable: try the next one.
    }
  }
  return undefined
}
