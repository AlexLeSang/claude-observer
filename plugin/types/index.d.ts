export type TokenTally = {
  input: number
  output: number
  cacheRead: number
  cacheCreation: number
  steps: number
}

export type LiveTotals = {
  // Model requests by model, and a subagent's requests by agent type.
  models: Record<string, TokenTally>
  subagents: Record<string, TokenTally>
  tools: Record<string, { calls: number; failures: number }>
  // Activations by skill name; 0 for a skill seen only as a Skill tool call.
  skills: Record<string, number>
  contextPercent: number | null
  contextTokens: number | null
  contextWindow: number | null
  costUsd: number | null
  // Context fill (percent) at each measurement, newest last.
  fill: number[]
}

// The "Project" tab: the Python reports' output, run when the tab opens.
export type HistoryReport = {
  title: string
  text: string
  ok: boolean
}

export type ObserverView = {
  tab: 'live' | 'project'
  // Absent until the Project tab ran the reports.
  history?: HistoryReport[]
  isLoading: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'usage-observer': {
      live: LiveTotals
      view: ObserverView
      // Per capture path, the ids of the sessions whose session_start is in the capture and not ended.
      announced: Record<string, string[]>
    }
  }
}
