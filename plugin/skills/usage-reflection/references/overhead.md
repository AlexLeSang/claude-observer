# Overhead views: skills, plugins

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
