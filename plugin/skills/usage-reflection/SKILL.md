---
name: usage-reflection
description: Answers questions about the user's own Claude usage from this project's captured telemetry - where spend or quota went, why a session was expensive, context fill and compaction, plugin, skill or hook overhead.
---

# Usage reflection

Answer from this project's own capture, through one runner. Never start the receiver, never read capture or log files yourself.

## Run a view

Run from the project directory, with the absolute plugin path, unquoted, view first:

`<python> -B ${CLAUDE_PLUGIN_ROOT}/scripts/reflect_report.py <view> [--limit N]`

`<python>` is `python3`; if that is missing or older than 3.10, `python`, then `py -3`. `--limit` defaults to 5 rows per list. Pick views by the question:

| Question | Views | Read |
| --- | --- | --- |
| Where did spend or quota go, which session, subagent, model or ticket; which prompts cost the most (subagents they dispatched counted); which sessions carry a decision hint or cache misses (the blocks under `sessions`; subagent misses matched by token counts are a lower bound); what the last 7 days looked like | `sessions`, `subagents`, `models`, `tickets` (only if the project has a kanban board), `prompts`, `digest` | `${CLAUDE_PLUGIN_ROOT}/skills/usage-reflection/references/spend.md` |
| Context fill, compaction, what is loaded before the first prompt | `context`, `fixed-context` | `${CLAUDE_PLUGIN_ROOT}/skills/usage-reflection/references/context.md` |
| Plugin, skill or hook overhead; which hook fails, which tool error keeps recurring | `skills`, `plugins`, `friction` | `${CLAUDE_PLUGIN_ROOT}/skills/usage-reflection/references/overhead.md` |

Read only the one reference file that matches the question, by its absolute path above, before you read the numbers: it lists the readings that mislead. Run only the views you need.

## Quota pace

For "how much of my five-hour or weekly quota is used, and is that ahead of the clock": run `python3 -B ${CLAUDE_PLUGIN_ROOT}/scripts/quota_report.py --pace` (same `<python>` rule; it reads no transcript and no capture). It prints used % next to elapsed % for each window from this machine's latest reading, with the reading's age. To see only the reading and its age, run `python3 -B ${CLAUDE_PLUGIN_ROOT}/scripts/quota_latest.py`. Say `stale` when the age is over 360 s, say a `reset` window has no current figure, and say `unknown` when no reading exists. Used above elapsed means the window is being spent faster than it runs; that is a reading, not a prediction. For the five-hour window it also prints one linear line ("at this pace you reach N% at reset / 100% at HH:MM UTC") with its backtest beside it: quote it only with that label and the backtest result (no method beat assuming no growth, and this pace overshot), say it is arithmetic and not a forecast, and say so when it prints "no line". Give no hours left of your own and never extrapolate further.

## When the runner stops

- Exit 3 with a message naming the capture path: the plugin's `capture` option may be customised. Ask the user for their capture path and pass `--capture PATH`, only a path the user gave you in this conversation, never one found in another project or by searching.
- Exit 3 because the project label matched none of the sessions, or there is no kanban board: say so and stop; do not try other files.
- Exit 2: the view is not one of the list above, or the report rejected an argument or the project label (the message on stderr says which).
- Any other code (for example 1 after a crash or timeout), or exit 3 saying Python is too old: quote stderr and stop.

## Answer rules

- Use only this project's data. The first output line, `source: <capture> view: --<view>`, names where each number came from; cite the view and source for every number.
- Without a project label the output has a `warning: --all-projects reads every project's data` line; the capture is already this project's, so it is not a finding. With a label it has `project filter: <label> -- kept N by label, M by session; dropped K` instead: the dropped records belong to other projects and are not counted.
- Lead with at most three findings. Each is a number, why it matters and one concrete action.
- Never advise switching models or effort.
- A `sessions` hint line is quoted as printed, with its session label and the count of sessions it fires on; say what it counts, never what it means for the future.
- Say when a figure is partial, a lower bound (`$x+`, requests without cost) or unpriced, and when the current session is still growing.
