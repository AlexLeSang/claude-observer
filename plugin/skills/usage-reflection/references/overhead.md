# Overhead views: skills, plugins, friction

Run through the runner only. `--limit` (default 5) bounds `skills`' session list only; `plugins` ignores it.

## Columns

- `skills`: activations per skill and source (`acts`, `sessions`, `prompts`, triggers `user-slash`, `claude-proactive`, `nested-skill`); tokens and `cost_usd` of the prompts that activated a skill; load overhead per skill (`loads`, `tokens`, `rereads`, `reread_tokens`, `cost_usd`); the sessions with activations. Claude Code redacts user and project skills to `custom_skill`; names are recovered locally where possible.
- `plugins`: plugins loaded per session (`sessions`, `share`, `first_seen`, `last_seen`, `versions`), installs, session cost with and without each plugin (`with`, `without` session counts, mean tokens and USD), hook registrations, per-plugin hook overhead from local transcripts, and skill activations per plugin. Non-official plugins are redacted to `third-party` and named only when local settings leave one candidate.

## Traps

- The "tokens in prompts that activated the skill" table is work done while the skill was active, split across the skills a prompt activated. It is not what the skill costs. Skill cost is the "Skill load overhead" table.
- Load overhead is an estimate: the skill's recorded prompt size / 4 as tokens, one cache write at the loading agent's next request (5-minute rate, 1.25x input) plus one cache read per later request until the run ends or compacts. This is the one place the static model price table is used. A model missing from it adds a "WARNING: models without a price row:" line and leaves its requests out: the load's cost shows `+?` after the figure (a lower bound) or `-` when nothing could be priced; `-` also means no later request of the loading agent yet. Every other cost in these views is Claude Code's own `cost_usd`; requests without one are left out of that cost.
- `plugins` "with and without" is a correlation, not the plugin's own cost: sessions differ in length and task, and a group needs 3 sessions or the means show `-`.
- `plugins` is not bounded by `--limit`: every plugin is listed whatever you pass, so the output can be long.
- Hook overhead and redacted-name recovery read local transcripts. The runner runs from the main checkout, also in a linked worktree, so they read the main checkout's transcripts and its `<slug>--claude-worktrees-*` sibling folders (the worktrees under `.claude/worktrees`); if none are found, "no transcripts found for this project" appears. That is missing data, not zero overhead.
- The plugin capture mirrors `plugin_loaded` and hook registrations for interactive sessions only, not `claude -p` runs; absent plugins in a session can mean it was not interactive.
- A skill that was never activated in the captured window does not appear; that is not the same as costing nothing, since its listing line is in the fixed context (see context.md, `fixed-context`).
- The current session is in every table and still growing.

`friction` ignores `--limit`. Its first line of the output is a scope line (`friction: ...`): quote its counts with every finding.

## `friction` columns

- Failing hooks: `hook_name` (kept as recorded, `<event>:<tool>` or the plain event name), `failing` as `failures of executions` (executions of the failing session-hook pairs), `blocking`, `sessions` (sessions where the hook failed), `clean_sessions` (sessions that ran the same hook without one failure; `every run` is a per-session reading, so read it next to this count), `failing_ms` (summed duration of the failing executions), `fix?` (`every run` when some session failed 100% of at least 2 executions) and the first and last day seen.
- Failed hook runs by source (transcripts): `plugin` (an installed plugin's name, `(not a plugin)` or `(ambiguous)`), `exit code` kind, `runs`, `sessions`; plus an agreement line against the mirror.
- Recurring tool error signatures: `tool_name` and `error_type` (never the error text), `sessions`, `failures`, share of sessions; at most 10 rows, then `... and K more`.

## `friction` traps

- "no failing hooks seen" means none in these captures, not that hooks work: a hook that never runs is invisible. Never say "healthy".
- Only non-blocking errors count as failing. A blocking hook is a guard doing its job; it appears as one count line and in the `blocking` column.
- The hook half has had no real failure in the project's own data yet; a row is worth checking against the hook's script before acting.
- A recurring signature is not proof of a config problem: the top one is often ordinary task behaviour. Say "recurring", not "misconfigured".
- The hook table names the hook by `hook_name`, not by plugin or settings file. The separate "Failed hook runs by source (transcripts)" table gives the plugin (or `(not a plugin)` or `(ambiguous)`) and the exit-code kind, but is not joined to a hook row: look the hook up yourself before advising a change.
- The transcript table is UNVERIFIED on real data (no real failed run seen yet). If the agreement line shows sessions that differ, say the two sources disagree; do not pick one.
