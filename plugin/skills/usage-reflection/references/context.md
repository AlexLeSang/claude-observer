# Context views: context, fixed-context

Run through the runner only. Both list the newest `--limit` sessions (default 5).

## Columns

- `context`, fill block: `samples` number of fill measurements; `peak` highest fill as a share of the window (`!` = 50% or more), `peak tokens`, `window`, `last` the latest sample, `compact` main-loop compactions that installed a summary, `fill over time` one character per sample scaled 0-100% of the window. Fill comes from the plugin's `measure` records; a session without them shows no fill.
- `context`, compaction block: per main-loop compaction, `trigger`, `before` and `after` context tokens, `freed` share, and the cache re-created by the next main-loop request: `created` cache tokens it had to write, `read` it could still read, `cost`. Skipped and failed attempts are left out and counted in the notes.
- `fixed-context` (plugin capture only): sizes in characters, about four per token. `system` system prompt sections, `context` context blocks, `tools` listed (not deferred) tool descriptions, `attach` attachments before the first main-loop request, `fixed` their sum, `~tok` = fixed / 4, `input` that first request's input tokens, `fixed%` = `~tok` over `input`. Below the table: mean size per system section, context block and listed tool over the sessions shown, and subagent attachments.

## Traps

- "none recorded" under the compaction block means no compaction among the sessions shown, not none in the project. When the fill block says "N older sessions not shown, raise --limit", rerun with a larger `--limit` before saying a project never compacted.
- A subagent's compaction is not in `compact`, in `context` or in `sessions`: both count main-loop compactions only, because a subagent's does not rebuild the main-loop cache. Both notes say that subagents compacted too ("N subagent compactions not listed" in `context`, "not counted in compact" in `sessions`). Report them as that note, not as main-loop compactions.
- `peak` and `compact` in `sessions` follow the same rule as `context` (fill percent from `measure`, else tokens over window), so they agree even with the OTel mirror off. `-` means no `measure` records for that session ("peak context needs the plugin capture"): missing data, not an empty context. If the two views ever disagree on one session, quote `context` and say so.
- A compaction's `cost` is known only when the following request was a costed one; "cost is not known for requests seen only as native steps" says it is missing, not zero. "without a later main-loop request" means there was nothing to measure yet.
- `fixed%` is a rough ratio of a character estimate to the first request's input tokens; it can exceed 100% and is a hint of where fixed context sits, not an exact share. A resumed session is measured from its first request after the first prompt-compose record; a session with no main-loop request yet is "startup-only" and not listed (a note counts it). Deferred tools are left out: they are not sent until loaded. A session without a system prompt record counts as 0 for it (a note says so).
- The current session is in every table and still growing: its peak and compaction count may rise.
