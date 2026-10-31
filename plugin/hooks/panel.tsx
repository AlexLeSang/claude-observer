// The /observer pane: "This session" (live totals from $.state) and "Project"
// (the Python reports' output). It opens only when the person types /observer
// and never draws unasked. Names, counts and sizes only, no prompt or tool content.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'

import type { HistoryReport, LiveTotals, ObserverView, TokenTally } from '../types'
import {
  cacheHitRate,
  compact,
  emptyLive,
  sparkline,
  sumTokens,
  topTools,
  totalTally,
} from './live.ts'
import { MIN_PYTHON, findPython, pythonNames } from './python.ts'

export const PANE = 'usage-observer'
const TITLE = 'Observer'
const TOP = 5
const REPORT_TIMEOUT_MS = 30000
const MAX_REPORT_CHARS = 20000

const liveAtom = atom({ plugin: 'usage-observer', key: 'live' } as const, emptyLive())
const viewAtom = atom(
  { plugin: 'usage-observer', key: 'view' } as const,
  { tab: 'live', isLoading: false } as ObserverView,
)

export type ProjectPaths = { home: string; capture: string }

// Where the reports run and read, set at session.start (a reload runs it again).
let projectPaths: ProjectPaths = { home: '.', capture: 'logs/agent-telemetry.mod.jsonl' }

export function setProjectPaths(paths: ProjectPaths) {
  projectPaths = paths
}

function pad(text: string, width: number) {
  return text.length >= width ? text : text + ' '.repeat(width - text.length)
}

function tokenLine(name: string, t: TokenTally) {
  return `${pad(name, 28)} steps ${pad(String(t.steps), 5)} in ${pad(compact(t.input), 7)} out ${pad(compact(t.output), 7)} cache r ${pad(compact(t.cacheRead), 7)} w ${compact(t.cacheCreation)}`
}

// The live tab as plain lines, so a test can read them without a surface.
export function liveLines(live: LiveTotals): string[] {
  const total = totalTally(live.models)
  const hit = cacheHitRate(total)
  const lines: string[] = ['Tokens by model']
  const models = Object.entries(live.models)
  if (models.length === 0) lines.push('  no model requests yet')
  for (const [model, t] of models) lines.push('  ' + tokenLine(model, t))
  lines.push(
    `Total  ${compact(sumTokens(total))} tokens in ${total.steps} requests`,
    `Cache hit rate  ${hit === null ? '-' : `${Math.round(hit * 100)}%`}`,
    `Cost  ${live.costUsd === null ? '-' : `$${live.costUsd.toFixed(2)}`}`,
    `Context  ${live.contextPercent === null ? '-' : `${Math.round(live.contextPercent)}%`}` +
      (live.contextTokens !== null && live.contextWindow !== null
        ? ` (${compact(live.contextTokens)} of ${compact(live.contextWindow)})`
        : ''),
  )
  if (live.fill.length > 1) lines.push(`Context fill  ${sparkline(live.fill)}`)

  lines.push('Subagents')
  const subagents = Object.entries(live.subagents)
  if (subagents.length === 0) lines.push('  none')
  for (const [type, t] of subagents) {
    lines.push(`  ${pad(type, 28)} steps ${pad(String(t.steps), 5)} tokens ${compact(sumTokens(t))}`)
  }

  lines.push('Most used tools')
  const used = topTools(live, 'calls', TOP)
  if (used.length === 0) lines.push('  none')
  for (const [tool, row] of used) lines.push(`  ${pad(tool, 28)} ${row.calls} calls`)
  lines.push('Most failing tools')
  const failing = topTools(live, 'failures', TOP)
  if (failing.length === 0) lines.push('  none')
  for (const [tool, row] of failing) lines.push(`  ${pad(tool, 28)} ${row.failures} of ${row.calls} failed`)

  lines.push('Skills used')
  const skills = Object.entries(live.skills).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  if (skills.length === 0) lines.push('  none')
  for (const [skill, n] of skills) lines.push(`  ${pad(skill, 28)} ${n}x`)
  return lines
}

// The Python reports behind the Project tab: stay Python, run by argv. The tools report reads the
// project= label from the settings in `paths.home`; without one it reads the whole capture, which
// is this project's own file, instead of exiting 2 "no project label found". When it does so it
// prints its all-projects warning line, which stays in the pane: the scope is then the file.
export function reportCommands(root: string, paths: ProjectPaths, python: readonly string[]) {
  return [
    {
      title: 'Tools (report --tools)',
      argv: [...python, '-B', `${root}/scripts/agent_telemetry.py`, 'report', '--tools', '--all-projects-if-unlabelled', '--source', paths.capture],
    },
    {
      title: 'Usage (agent_usage_report.py)',
      argv: [...python, '-B', `${root}/scripts/agent_usage_report.py`, '--project-root', paths.home],
    },
  ]
}

async function runReports($: EngineInterface, paths: ProjectPaths): Promise<HistoryReport[]> {
  const python = await findPython((argv, timeoutMs) => $.process.run(argv, { timeoutMs }))
  if (python === undefined) {
    const text = `no Python ${MIN_PYTHON.major}.${MIN_PYTHON.minor}+ found (tried ${pythonNames()}); the reports need one`
    return [{ title: 'Reports', text, ok: false }]
  }
  const out: HistoryReport[] = []
  for (const report of reportCommands($.plugin.root, paths, python)) {
    try {
      const ran = await $.process.run(report.argv, { cwd: paths.home, timeoutMs: REPORT_TIMEOUT_MS })
      const text = ran.exitCode === 0 ? ran.stdout : `exit ${ran.exitCode}\n${ran.stderr}`
      out.push({ title: report.title, text: text.slice(0, MAX_REPORT_CHARS), ok: ran.exitCode === 0 })
    } catch (e) {
      out.push({ title: report.title, text: `did not run: ${String(e).slice(0, 200)}`, ok: false })
    }
  }
  return out
}

export function registerPanel(on: On) {
  // /clear, /resume and /branch reset $.state: seed it again so the pane reads a
  // fresh session rather than an empty slot, and the Project tab reloads.
  on('classic.SessionStart', async ($, e, next) => {
    try {
      if (e.source === 'clear' || e.source === 'resume' || e.source === 'fork') {
        await $.state.set({ plugin: 'usage-observer', key: 'live' } as const, emptyLive())
        await $.state.set({ plugin: 'usage-observer', key: 'view' } as const, { tab: 'live', isLoading: false })
      }
    } catch {
      // Observation only.
    }
    return next(e)
  })

  on('command.run', { command: 'observer' }, async $ => {
    // Under VS Code, `claude -p` and the SDK nothing draws: the pane stays open but undrawn.
    try {
      // OpenResult is `{ isPlaced, reason? } | void`: an empty answer counts as placed.
      const opened: { isPlaced?: boolean; reason?: string } | void = await $.ui.open({ id: PANE, title: TITLE, focus: true, closeOnEscape: true })
      if (opened !== undefined && opened.isPlaced === false) {
        return { text: `Observer pane is open but not drawn (${opened.reason ?? 'no reason given'}).` }
      }
    } catch (e) {
      return { text: `Observer pane did not open: ${String(e).slice(0, 200)}` }
    }
    return { text: 'Observer pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Code } = $.ui.resolve(e)
    const view = await read($, viewAtom)
    const live = await read($, liveAtom)
    const columns = e.props.bodyColumns

    const showProject = async () => {
      await update($, viewAtom, v => ({ ...v, tab: 'project', isLoading: true }))
      const history = await runReports($, projectPaths)
      await update($, viewAtom, v => ({ ...v, history, isLoading: false }))
    }

    const tabs = (
      <Box flexDirection="row" gap={1}>
        <Button
          key="tab-live"
          label="This session"
          hotkey="1"
          variant={view.tab === 'live' ? 'primary' : 'secondary'}
          onPress={() => update($, viewAtom, v => ({ ...v, tab: 'live' }))}
        />
        <Button
          key="tab-project"
          label="Project"
          hotkey="2"
          variant={view.tab === 'project' ? 'primary' : 'secondary'}
          onPress={showProject}
        />
      </Box>
    )

    if (view.tab === 'live') {
      return (
        <Box flexDirection="column">
          {tabs}
          {liveLines(live).map((line, i) => (
            <Text key={`live-${i}`} dimColor={line.startsWith('  ')}>
              {line.slice(0, columns)}
            </Text>
          ))}
        </Box>
      )
    }
    return (
      <Box flexDirection="column">
        {tabs}
        {view.isLoading && <Text dimColor>Running the reports...</Text>}
        {(view.history ?? []).map((report, i) => (
          <Box key={`report-${i}`} flexDirection="column">
            <Text bold>{report.title}</Text>
            <Code source={report.text} wrap="truncate-end" />
          </Box>
        ))}
        <Button key="refresh" label="Run again" hotkey="r" plain onPress={showProject} />
      </Box>
    )
  })
}
