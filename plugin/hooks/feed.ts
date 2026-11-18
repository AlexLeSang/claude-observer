// The engine calls the cache line and the alerts make, as a plain object. The engine's static scan follows `$`
// only into a function declared in the same file (Claude Code 2.1.287 refuses `$` passed to an imported
// function), so observer.ts builds this object from `$.noun.event(...)` spelled out there, and the feeds in
// alertEvents.ts and cacheEvents.ts take it where they used to take `$`. A fake engine fits it as it is.

import type { EngineInterface } from 'claude-code'

export type Feed = {
  store: Pick<EngineInterface['store'], 'get' | 'set' | 'delete'>
  state: { set: (key: { readonly plugin: 'usage-observer'; readonly key: 'alerts' }, value: { lines: string[] }) => Promise<unknown> }
  ui: Pick<EngineInterface['ui'], 'log' | 'status'>
  session: Pick<EngineInterface['session'], 'id'>
  // `read` is the one-argument text form: the engine's own type is overloaded, and a Pick would take the last one.
  fs: Pick<EngineInterface['fs'], 'stat'> & { read: (path: string) => Promise<string> }
  process: Pick<EngineInterface['process'], 'run'>
  clock: Pick<EngineInterface['clock'], 'now' | 'every'>
}
