// usage-observer: the claude-observer collector, run inside Claude Code.
//
// Observes the session (model requests, tool calls, subagents, skills, context,
// cost) and mirrors the OTel log events Claude Code sends to its collector,
// then pipes batches to scripts/mod_capture_append.py (run by whichever of python3, python
// and py -3 is 3.10 or newer: python.ts), which appends them to
// the project's own capture (0600, rotated, identity dropped). Nothing leaves
// the machine: no $.http, and records carry sizes and names, never prompt or
// tool content. Every hook only observes: it calls next(e) unchanged and a
// failure inside it is caught, so the session never depends on this plugin.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'
import type { LiveTotals } from '../types'
import { applyMeasure, applySkill, applyStep, applyTool, emptyLive } from './live.ts'
import { registerPanel, setProjectPaths } from './panel.tsx'
import { MIN_PYTHON, findPython, pythonNames } from './python.ts'
import {
  IDENTITY_ATTRIBUTES,
  agentFields,
  cleanAgentType,
  attachmentRecord,
  compactionRecord,
  composeRecord,
  contextRecord,
  describeRecord,
  hookEvent,
  hookTool,
  AgentMisses,
  HookRuns,
  mcpServer,
  offerRecord,
  otelRecord,
  pluginRegisterRecord,
  pluginSetRecord,
  promptSubmitRecord,
  skillRecord,
  spawnRecord,
  stepRecord,
  toolRecord,
  type ObserverRecord,
} from './records.ts'

const FLUSH_MS = 5000
const MAX_BUFFER = 2000
// $.store is shared by every session on the machine, so the key names the capture.
const HEARTBEAT_PREFIX = 'heartbeat:'
// Per capture, the ids of the sessions announced and not yet ended, in $.state: a reload resets the
// module's variables but not $.state, so this keeps it from announcing the same session twice. $.state
// ends with the process (the reference's "Keep state" table), so a process that crashed leaves no mark
// for the next one to trust; in $.store, which outlasts it, a stale mark skipped the announce.
const ANNOUNCED_KEEP = 20
const APPEND_TIMEOUT_MS = 10000
// The appender imports the rest when it loads, so a copy that has lost any of them cannot write.
// A package test derives this list from the appender's imports and fails when it drifts.
const APPENDER_SCRIPTS = [
  'mod_capture_append.py',
  'agent_telemetry.py',
  'capture_exclude.py',
  'capture_paths.py',
  'quota_latest.py',
  'instant_util.py',
  'safe_name.py',
]
// The appender's one-line notice that git may still list the capture (it could not write .git/info/exclude).
const GIT_NOTICE = /^mod_capture_append: git-exclude: (.*)$/m
// A machine with no interpreter is probed again this often, not at every flush.
const REPROBE_MS = 60000
// All session.end hooks share one short wall-clock bound (1.5 s by default).
const END_MARGIN_MS = 250
const MIN_END_TIMEOUT_MS = 200
// The final flush's append is cut at its timeout, and a cut after the write requeues a batch that is already on
// disk. The appender's exclude step (up to 1 s of its helper's calls) gets the timeout less this margin for the
// interpreter start and the write; 0 skips it, and the next session's flush does it instead.
const EXCLUDE_MARGIN_MS = 700
// A record waits this long for its session's announce; a hung engine call must not hold every later one.
const ANNOUNCE_WAIT_MS = 1000

const buffer: ObserverRecord[] = []
const hookRuns = new HookRuns()
const agentTypes = new Map<string, string>()
// Ids $.agent.list() did not name (workflow agents, compaction and memory forks): see AgentMisses.
const agentMisses = new AgentMisses()
// agent.offer fires at every listing and dispatch: one record per type and answer a session.
const offered = new Set<string>()
let sessionId: string | undefined
// The session whose session_start and plugin_set were pushed. /clear gives the process a new id
// without a session.start, and a reload runs session.start again under the old one.
let announcedId: string | undefined
// The announce for announcedId while it runs: records of the same session wait for it.
let announcing: Promise<void> | undefined
// Bumped to cancel an announce still under way (it gave up waiting, or its session ended): a
// cancelled announce pushes nothing, so a late one cannot add a session_start under a wrong id.
let announceGen = 0
// Ids whose session_start is in the buffer (or already flushed) for the current run of the session:
// an announce that hung after pushing it and was retried must not push another.
const startPushed = new Set<string>()
// Sessions that have ended in this process: a flush that finishes later must not mark them.
const endedIds = new Set<string>()
// What session.start was told; a session first seen later (after /clear) has no such event.
let startInfo: { interactive?: boolean; surface?: string | null } = {}
let promptId: string | undefined
let capturePath: string | undefined
// One toast per kind of failure and process: a later, different failure must still be shown.
const warned = new Set<string>()
let scriptsMissing: string | undefined
// Whether the flush timer ever fired. The engine answers a refused clock.every with a handle that does
// nothing, neither throwing nor rejecting, so watchTimer looks for the missing ticks.
let tickSeen = false
// Flushes run one at a time: the timer's and session.end's must not split a batch between them.
let flushing: Promise<void> = Promise.resolve()
let python: readonly string[] | undefined
let probedAt = 0
// The state scan reads an atom only where it is written: panel.tsx declares the same reference.
const liveAtom = atom({ plugin: 'usage-observer', key: 'live' } as const, emptyLive())
const announcedAtom = atom({ plugin: 'usage-observer', key: 'announced' } as const, {} as Record<string, string[]>)

// Every record leaves push() with this load's nonce and the next sequence number. A batch re-queued
// after a timeout keeps its stamps, so when the appender had written it anyway, the readers drop the
// repeat by (run, seq).
const RUN = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
let seq = 0

function push(record: ObserverRecord) {
  if (buffer.length >= MAX_BUFFER) buffer.shift()
  buffer.push({ ...record, run: RUN, seq: seq++ })
}

function now() {
  return new Date().toISOString()
}

async function base($: EngineInterface, kind: string) {
  sessionId ??= await $.session.id()
  // The first record under a session id brings its session_start and plugin_set.
  if (kind !== 'session_start' && kind !== 'plugin_set') await announce($)
  return { v: 1, source: 'mod', kind, ts: now(), session: sessionId, prompt: promptId }
}

async function announcedIds($: EngineInterface, path: string): Promise<string[]> {
  const all = await read($, announcedAtom)
  const ids = all[path]
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : []
}

async function setAnnounced($: EngineInterface, path: string, ids: string[]) {
  await update($, announcedAtom, all => ({ ...all, [path]: ids }))
}

// Pushes session_start and plugin_set once for the current session id, whichever record comes first.
// A caller that finds the announce under way waits for it, so no record lands before its session_start.
async function announce($: EngineInterface) {
  sessionId ??= await $.session.id()
  const id = sessionId
  if (announcedId !== id) {
    announcedId = id
    endedIds.delete(id)
    const mine = pushAnnounce($, id, ++announceGen).finally(() => {
      if (announcing === mine) announcing = undefined
    })
    announcing = mine
  }
  const waiting = announcing
  if (waiting === undefined) return
  // A hung announce is given up on: the record goes ahead without it, and the next record starts
  // the announce again (the abandoned one is cancelled, so it cannot add a second session_start).
  const timeout = Symbol()
  const result = await Promise.race([waiting, $.clock.sleep(ANNOUNCE_WAIT_MS).then(() => timeout)])
  if (result === timeout && announcing === waiting) {
    announceGen++
    announcedId = undefined
    announcing = undefined
  }
}

// The mark in $.state is written by markAnnounced once an appender run took the records: stored
// now, a reload before the flush would lose the buffer and then hide the session for good.
async function pushAnnounce($: EngineInterface, id: string, gen: number) {
  if (capturePath !== undefined) {
    try {
      if ((await announcedIds($, capturePath)).includes(id)) return
    } catch {
      // No mark readable: at worst a reload announces the session again.
    }
  }
  if (!startPushed.has(id)) {
    try {
      const version = await $.session.version()
      const start = {
        ...(await base($, 'session_start')),
        version: version.version,
        model: await $.session.model(),
        interactive: startInfo.interactive,
        surface: startInfo.surface,
      }
      if (gen !== announceGen) return
      startPushed.add(id)
      push(start)
    } catch {
      // Observation only.
      callRefused($, 'session.version or session.model', 'session_start is not recorded')
    }
  }
  if (gen !== announceGen) return
  try {
    const set = { ...(await base($, 'plugin_set')), ...pluginSetRecord(await $.settings.read()) }
    if (gen !== announceGen) return
    push(set)
  } catch {
    // Observation only.
    callRefused($, 'settings.read', 'plugin_set is not recorded')
  }
}

// After a successful run: the sessions whose session_start it carried are now in the capture.
// A session whose end is in the same batch is not marked, since its end clears the mark anyway.
async function markAnnounced($: EngineInterface, path: string, batch: ObserverRecord[]) {
  const ended = new Set(batch.filter(r => r.kind === 'session_end').map(r => r.session))
  const started = batch
    .filter(r => r.kind === 'session_start' && !ended.has(r.session) && !endedIds.has(String(r.session)))
    .map(r => String(r.session))
  if (started.length === 0) return
  try {
    const ids = await announcedIds($, path)
    const add = started.filter(id => !ids.includes(id))
    if (add.length > 0) await setAnnounced($, path, [...ids, ...add].slice(-ANNOUNCED_KEEP))
  } catch {
    // No mark: at worst a reload announces the session again.
    callRefused($, 'state.get or state.set', 'a reload may record a session twice')
  }
}

async function unannounce($: EngineInterface, id: string | undefined) {
  if (id === undefined || capturePath === undefined) return
  try {
    const ids = await announcedIds($, capturePath)
    if (ids.includes(id)) await setAnnounced($, capturePath, ids.filter(i => i !== id))
  } catch {
    // Observation only.
    callRefused($, 'state.get or state.set', 'a reload may record a session twice')
  }
}

// The project's main checkout: the session root, or the checkout a linked worktree belongs to.
async function resolveHome($: EngineInterface) {
  const root = await $.session.root()
  let home = root
  try {
    // A session in a linked worktree writes to the main checkout's logs/.
    const git = await $.process.run(
      ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
      { cwd: root, timeoutMs: 5000 },
    )
    const common = git.stdout.trim()
    if (git.exitCode === 0 && common.endsWith('/.git')) home = common.slice(0, -'/.git'.length)
  } catch {
    // Not a git checkout, or git missing: the session root is the project.
  }
  return home
}

async function resolveCapture($: EngineInterface, option: string) {
  if (option.startsWith('/')) return option
  return `${await resolveHome($)}/${option}`
}

function missingPython() {
  return `no Python ${MIN_PYTHON.major}.${MIN_PYTHON.minor}+ found (tried ${pythonNames()})`
}

// The interpreter missing is as loud as a failed write, and shown once with it.
function warnOnce($: EngineInterface, kind: string, text: string) {
  if (warned.has(kind)) return
  warned.add(kind)
  // $.ui.toast is fire and forget: it returns nothing and the engine drops a refused one itself (the
  // host logs "$.ui.toast dropped"), so a policy that refuses it cannot break the session from here.
  $.ui.toast(`usage-observer: ${text}`)
}

// A call of the plugin's own was refused or failed (an organisation policy mod can refuse any mods call):
// say once which call and what it costs. The engine's error text is not shown, so nothing
// the policy wrote reaches the toast.
function callRefused($: EngineInterface, call: string, cost: string) {
  warnOnce($, `call:${call}`, `${call} was refused or failed, so ${cost}. An organisation policy may block it.`)
}

// Two periods and a little over after the timer was set it must have ticked; if not, it was refused.
async function watchTimer($: EngineInterface) {
  try {
    await $.clock.sleep(2 * FLUSH_MS + 1000)
    if (!tickSeen) callRefused($, 'clock.every', 'records are written only at session end')
  } catch {
    // Observation only: with no clock at all there is nothing to measure with.
  }
}

async function heartbeat($: EngineInterface, path: string, error: string | undefined) {
  try {
    await $.store.set(`${HEARTBEAT_PREFIX}${path}`, { at: now(), ok: error === undefined, error, capture: path })
  } catch {
    // A refused store must not stop the toast or the retry that follow.
    callRefused($, 'store.get or store.set', 'the capture health note is not kept')
  }
}

// Flushes number at most one running and one waiting: a timer tick while any flush is in flight is
// skipped (the running one, or its retry at the next tick, takes the records), so a slow or hung
// appender cannot build a queue of 10 s attempts behind it.
let inFlight = 0

function flush($: EngineInterface, timeoutMs: number = APPEND_TIMEOUT_MS, isFinal = false) {
  const startedAt = Date.now()
  const prior = flushing
  // The final flush waits for one in flight, so the exit does not kill its appender, but only as long
  // as its own budget allows: the wait comes out of the time the append gets, and ends with it.
  const turn = isFinal ? Promise.race([prior, $.clock.sleep(timeoutMs)]) : prior
  inFlight++
  const run = turn.then(() => flushNow($, timeoutMs, isFinal, startedAt))
  flushing = run
    .catch(() => undefined)
    .then(() => {
      inFlight--
    })
  return run
}

async function flushNow($: EngineInterface, timeoutMs: number, isFinal: boolean, startedAt: number) {
  for (const record of hookRuns.drain(now())) push(record)
  if (buffer.length === 0 || capturePath === undefined) return
  if (scriptsMissing !== undefined) {
    // Running the appender would only fail: keep the newest records and say why.
    await heartbeat($, capturePath, scriptsMissing)
    return
  }
  if (python === undefined && (isFinal || Date.now() - probedAt >= REPROBE_MS)) {
    // No interpreter at session.start: look again, since one may have been installed since.
    // The probes share the final flush's budget rather than add to it.
    probedAt = Date.now()
    python = await findPython((argv, probeMs) => $.process.run(argv, { timeoutMs: probeMs }), isFinal ? timeoutMs : undefined)
  }
  if (python === undefined) {
    // The records stay buffered (the newest MAX_BUFFER) until one is found.
    await heartbeat($, capturePath, missingPython())
    return
  }
  const appendMs = Math.max(MIN_END_TIMEOUT_MS, timeoutMs - (Date.now() - startedAt))
  const batch = buffer.splice(0, buffer.length)
  const stdin = batch.map(r => JSON.stringify(r)).join('\n') + '\n'
  let error: string | undefined
  let gitNotice: string | undefined
  try {
    const ran = await $.process.run(
      [
        ...python,
        '-B',
        `${$.plugin.root}/scripts/mod_capture_append.py`,
        '--out',
        capturePath,
        ...(isFinal ? ['--exclude-budget', (Math.max(0, appendMs - EXCLUDE_MARGIN_MS) / 1000).toFixed(3)] : []),
      ],
      { stdin, timeoutMs: appendMs },
    )
    if (ran.exitCode !== 0) error = `appender exited ${ran.exitCode}: ${ran.stderr.slice(0, 200)}`
    else gitNotice = GIT_NOTICE.exec(ran.stderr)?.[1]?.slice(0, 200)
  } catch (e) {
    error = `appender did not run: ${String(e).slice(0, 200)}`
  }
  if (error !== undefined) requeue(batch)
  else await markAnnounced($, capturePath, batch)
  await heartbeat($, capturePath, error)
  if (error !== undefined) warnOnce($, 'write', `capture write failed (${error})`)
  if (gitNotice !== undefined) warnOnce($, 'git', `capture may show in git status: ${gitNotice}`)
}

// A failed run puts its batch back in front of what arrived meanwhile, so the next flush retries
// it in order. The newest MAX_BUFFER stay. After a timeout the appender may have written the
// batch anyway, so a retry can repeat records; losing them would be worse.
function requeue(batch: ObserverRecord[]) {
  buffer.unshift(...batch)
  if (buffer.length > MAX_BUFFER) buffer.splice(0, buffer.length - MAX_BUFFER)
}

// The interpreter and the scripts the appender needs, checked once at session.start. Each
// failure is shown, and flush then keeps the records instead of running a doomed appender.
async function checkInstall($: EngineInterface, path: string) {
  python = await findPython((argv, timeoutMs) => $.process.run(argv, { timeoutMs }))
  probedAt = Date.now()
  if (python === undefined) {
    warnOnce($, 'python', `capture is off: ${missingPython()}. Install Python or put it on PATH (an organisation policy that refuses process.run gives the same result).`)
    await heartbeat($, path, missingPython())
  }
  scriptsMissing = undefined
  for (const script of APPENDER_SCRIPTS) {
    if (await $.fs.exists(`${$.plugin.root}/scripts/${script}`)) continue
    scriptsMissing = `the install has no scripts/${script}`
    warnOnce($, 'scripts', `capture is off: ${scriptsMissing}. Reinstall the plugin.`)
    await heartbeat($, path, scriptsMissing)
    break
  }
}

async function agentType($: EngineInterface, agentId: string | undefined) {
  if (agentId === undefined) return undefined
  if (!agentTypes.has(agentId) && !agentMisses.isRecent(agentId)) {
    try {
      for (const agent of await $.agent.list()) agentTypes.set(agent.id, agent.type)
    } catch {
      // The record goes without its agent type rather than not at all.
      callRefused($, 'agent.list', 'records carry no agent type')
    }
    // A refused list is a miss too: it is not asked again at every event.
    if (!agentTypes.has(agentId)) agentMisses.add(agentId)
  }
  return cleanAgentType(agentTypes.get(agentId)) ?? undefined
}

// The live totals behind /observer, kept in $.state so a reload keeps them.
// There is no live status line: the operator asked for reports, /observer and a digest.
async function track($: EngineInterface, change: (live: LiveTotals) => LiveTotals) {
  try {
    await update($, liveAtom, change)
  } catch {
    // Observation only.
  }
}

// Claude Code sets CLAUDE_CODE_REMOTE=true in a cloud session and never locally (cloud-environments
// docs). The session's surface is no signal: it is terminal, desktop, vscode, mobile or null. A read
// that fails counts as not cloud, so a refused read never costs a local session its capture.
async function isCloudSession($: EngineInterface) {
  try {
    return (await $.env.get('CLAUDE_CODE_REMOTE')) === 'true'
  } catch {
    return false
  }
}

export const register: Register = (on, options) => {
  const captureOption = String(options.capture ?? 'logs/agent-telemetry.mod.jsonl')

  registerPanel(on)

  on('session.start', async ($, e, next) => {
    // The container, and the capture in it, is lost when the session ends, and an agent there could
    // commit it: write nothing, say so once.
    // With no capture path, no timer and no interpreter probe, no flush ever runs: records buffer
    // (bounded by MAX_BUFFER) and are dropped with the process.
    if (await isCloudSession($)) {
      try {
        warnOnce($, 'cloud', 'capture is off in cloud sessions (the container and its files are lost when the session ends)')
        await $.command.register({
          name: 'observer',
          description: 'Open the usage observer pane (this session and project history)',
        })
      } catch {
        // Observation only.
        callRefused($, 'command.register', '/observer is not available')
      }
      return next(e)
    }
    try {
      capturePath = await resolveCapture($, captureOption)
      setProjectPaths({ home: await resolveHome($), capture: capturePath })
      startInfo = { interactive: e.isInteractive, surface: e.surface }
      await announce($)
      // Timers stop when the module reloads, and session.start fires once per load, not after /clear.
      // A refused clock.every is not seen here: the engine logs it and hands back a handle that does
      // nothing, neither throwing nor rejecting (watchTimer notices the missing ticks).
      await $.clock.every(FLUSH_MS, () => {
        tickSeen = true
        if (inFlight === 0) void flush($).catch(() => undefined)
      })
      void watchTimer($)
    } catch {
      // Observation only. Any of the setup calls can land here (session.id, session.root, process.run),
      // so the toast names the setup, not one call.
      callRefused($, 'session.id, session.root or process.run', 'capture may not be written')
    }
    try {
      // After the timer, so that a failing check never costs the session its capture.
      if (capturePath !== undefined) await checkInstall($, capturePath)
    } catch {
      // Observation only.
    }
    try {
      await $.command.register({
        name: 'observer',
        description: 'Open the usage observer pane (this session and project history)',
      })
    } catch {
      // Observation only: the pane is a convenience.
      callRefused($, 'command.register', '/observer is not available')
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    try {
      if (sessionId !== undefined) endedIds.add(sessionId)
      push({ ...(await base($, 'session_end')), reason: e.reason })
      await flush($, Math.max(MIN_END_TIMEOUT_MS, next.budget.remainingMs - END_MARGIN_MS), true)
    } catch {
      // Observation only.
    }
    // After /clear the process goes on under a new session id. A mark kept for the ended session
    // would hide a resume of it in a new process.
    await unannounce($, sessionId)
    announceGen++
    // A record under this id after the end (a resume in the same process) is a new run: it announces again.
    announcedId = undefined
    announcing = undefined
    if (sessionId !== undefined) startPushed.delete(sessionId)
    sessionId = undefined
    promptId = undefined
    offered.clear()
    return next(e)
  })

  // Every settings-hook event runs through here, so the time `next` takes is the
  // whole chain of hooks configured for it: plugins' and the user's together.
  on('classic.*', async ($, e, next) => {
    const startedAt = Date.now()
    const result = await next(e)
    try {
      hookRuns.add(sessionId ?? (await $.session.id()), hookEvent(e), hookTool(e), Date.now() - startedAt)
    } catch {
      // Observation only.
    }
    return result
  })

  on('plugin.register', async ($, e, next) => {
    const result = await next(e)
    try {
      push({ ...(await base($, 'plugin_register')), ...pluginRegisterRecord(e) })
    } catch {
      // Observation only.
    }
    return result
  })

  on('classic.UserPromptSubmit', async ($, e, next) => {
    promptId = e.prompt_id ?? promptId
    return next(e)
  })

  // The prompt's origin kind, so a prompt nobody typed (a task notification) can be told from one that was.
  // Recorded after `next`: by then the prompt has entered the session and `promptId` is its own.
  on('prompt.submit', async ($, e, next) => {
    const result = await next(e)
    try {
      push({ ...(await base($, 'prompt_submit')), ...promptSubmitRecord(e) })
    } catch {
      // Observation only.
    }
    return result
  })

  on('classic.SubagentStart', async ($, e, next) => {
    agentTypes.set(e.agent_id, e.agent_type)
    return next(e)
  })

  on('agent.spawn', async ($, e, next) => {
    const result = await next(e)
    try {
      if (result.agentId !== undefined) agentTypes.set(result.agentId, e.subagentType)
      push({ ...(await base($, 'agent_spawn')), ...spawnRecord(e, result) })
    } catch {
      // Observation only.
    }
    return result
  })

  on('agent.offer', async ($, e, next) => {
    const result = await next(e)
    try {
      const key = `${e.agent}:${result.isOffered}`
      if (!offered.has(key)) {
        offered.add(key)
        push({ ...(await base($, 'agent_offer')), ...offerRecord(e, result) })
      }
    } catch {
      // Observation only.
    }
    return result
  })

  on('turn.start', async ($, e, next) => {
    const result = await next(e)
    try {
      push({ ...(await base($, 'turn_start')), turn: e.turnId, prompt_chars: e.text.length })
    } catch {
      // Observation only.
    }
    return result
  })

  on('turn.step', async function* ($, e, next) {
    const startedAt = Date.now()
    let thinkingChars = 0
    const stream = next(e)
    while (true) {
      const step = await stream.next()
      if (step.done) {
        try {
          const type = await agentType($, e.agentId)
          push({
            ...(await base($, 'step')),
            ...stepRecord(e, step.value, thinkingChars, Date.now() - startedAt),
            agent_type: type,
          })
          await track($, live => applyStep(live, step.value.usage, e.agentId, type))
        } catch {
          // Observation only.
        }
        return step.value
      }
      if (step.value.kind === 'thinking') thinkingChars += step.value.text.length
      yield step.value
    }
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    try {
      push({
        ...(await base($, 'turn')),
        turn: e.turnId,
        reason: e.reason,
        ms: e.durationMs,
        usage: e.usage ?? null,
        ...agentFields(e.agentId, await agentType($, e.agentId)),
      })
    } catch {
      // Observation only.
    }
    return result
  })

  on('tool.call', async ($, e, next) => {
    const startedAt = Date.now()
    const result = await next(e)
    try {
      const record = toolRecord(e, result, Date.now() - startedAt, $.plugin.root)
      push({
        ...(await base($, 'tool')),
        ...record,
        mcp_server: mcpServer(String(e.tool)),
        agent_type: await agentType($, e.agentId),
      })
      // The record's skill, which only the Skill tool can set.
      await track($, live => applyTool(live, String(e.tool), result.isError === true || result.deny !== undefined, record.skill))
    } catch {
      // Observation only.
    }
    return result
  })

  on('skill.prompt', async ($, e, next) => {
    const result = await next(e)
    try {
      const record = skillRecord(e.skill, result.text.length)
      push({ ...(await base($, 'skill')), ...record })
      // The panel lists the cleaned name too; a name with nothing printable is no skill to list.
      const name = record.skill
      if (name !== null) await track($, live => applySkill(live, name))
    } catch {
      // Observation only.
    }
    return result
  })

  on('prompt.compose', async ($, e, next) => {
    const result = await next(e)
    try {
      push({ ...(await base($, 'prompt_compose')), ...composeRecord(e, result) })
    } catch {
      // Observation only.
    }
    return result
  })

  on('prompt.context', async ($, e, next) => {
    const result = await next(e)
    try {
      push({ ...(await base($, 'prompt_context')), ...contextRecord(result) })
    } catch {
      // Observation only.
    }
    return result
  })

  on('prompt.attachment', async ($, e, next) => {
    const result = await next(e)
    try {
      push({
        ...(await base($, 'prompt_attachment')),
        ...attachmentRecord(e, result),
        agent_type: await agentType($, e.agentId),
      })
    } catch {
      // Observation only.
    }
    return result
  })

  on('tool.describe', async ($, e, next) => {
    const result = await next(e)
    try {
      push({
        ...(await base($, 'tool_describe')),
        ...describeRecord(e, result),
        mcp_server: mcpServer(e.tool),
      })
    } catch {
      // Observation only.
    }
    return result
  })

  on('session.compact', async ($, e, next) => {
    const startedAt = Date.now()
    const result = await next(e)
    // A precompute only computes a summary for later and installs nothing.
    if (e.trigger === 'precompute') return result
    try {
      push({ ...(await base($, 'compaction')), ...compactionRecord(e, result, Date.now() - startedAt) })
    } catch {
      // Observation only.
    }
    return result
  })

  on('session.measure', async ($, e, next) => {
    try {
      push({
        ...(await base($, 'measure')),
        changed: e.changed,
        context_tokens: e.context.tokens ?? null,
        context_window: e.context.window,
        context_percent: e.context.percent ?? null,
        cost_usd: e.cost?.usd ?? null,
        // resets_at is the engine's ISO string as given; the key is absent when the engine sends none.
        rate_limits: e.rateLimits.map(r => ({
          kind: r.kind,
          percent: r.percentUsed,
          ...(typeof r.resetsAt === 'string' ? { resets_at: r.resetsAt } : {}),
        })),
      })
      await track($, live => applyMeasure(live, e))
    } catch {
      // Observation only.
    }
    return next(e)
  })

  on('telemetry.log', { to: 'collector' }, async ($, e, next) => {
    try {
      if (e.to === 'collector') push(otelRecord(e, IDENTITY_ATTRIBUTES))
    } catch {
      // Observation only.
    }
    return next(e)
  })
}
