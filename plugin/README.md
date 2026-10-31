# Usage Observer (`usage-observer`)

A Claude Code plugin that records what your sessions spend, as names, sizes and counts, into a file
inside your own project. It answers which models, subagents, skills and plugins are worth their token
cost. Nothing leaves your machine.

## What it collects

Per session, one JSON line each for: session start and end, turns, model requests (model, token counts
by type, cost), tool calls (tool name, outcome, sizes), skill activations, subagent dispatches and
offers (type, model, sizes), compactions, context and prompt sizes, hook time, and the OTel events
Claude Code itself emits, mirrored.

- **Metadata only.** The plugin's own records carry names, sizes, counts, token usage, subagent id and
  type, and the OTel `prompt.id`. They never carry prompt text, tool input or tool output, a
  subagent's prompt or description, or a hook's command or output. The OTel events Claude Code emits
  are mirrored as it sends them, except for these attributes, which are dropped by name:
  `user.email`, `user.id`, `user.account_uuid`, `user.account_id`, `organization.id`,
  `vcs.owner.name`, `vcs.repository.name`, `vcs.repository.url.full`, and `prompt`, `prompt_text`,
  `response`, `tool_parameters`, `tool_input`, `tool_output`, `tool_result_content` (any letter case;
  also dropped when `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS` or `OTEL_LOG_TOOL_CONTENT` is
  set), and any attribute that is not a string, number, boolean or list of strings. An attribute
  under another name is kept, so the capture can hold whatever else Claude Code puts in its events.
- **Local only.** The plugin makes no network call. It appends to
  `logs/agent-telemetry.mod.jsonl` under the project's main checkout (change it with the `capture`
  plugin option). The file is created mode 0600 in a 0700 directory, rotated by size and pruned by
  age. If git does not already ignore it, the plugin adds it to the repository's local
  `.git/info/exclude`; nothing tracked is touched. If that fails, a toast tells you.
- **Reads of your other local files.** The reports (not the recording) read more than the capture.
  They print counts and names and send nothing anywhere, but they do open files that hold your
  conversations:
  - this project's session transcripts under `~/.claude/projects/<project>/` (including each
    subagent's description and first message), for the Project tab's usage report and for the
    `subagents`, `tickets`, `skills` (for activations the capture does not name) and `plugins` views;
  - the Markdown task files of a `kanban` board folder (`tasks/*.md`) for the `tickets` view (the ticket status line), only when the project has
    that board;
  - `~/.claude/plugins/installed_plugins.json`, `~/.claude/settings.json` and the project's
    `.claude/settings.json` and `.claude/settings.local.json` for the `plugins` view.
- **Names it does record:** the plugin's own skill file names it read and the report view name it
  ran; the ticket number when a subagent description starts with `kanban #<number>`.

## Install

```bash
claude plugin marketplace add alexlesang/claude-observer          # once per machine; without a ref it follows the repository's default branch
claude plugin install usage-observer@usage-observer --scope project   # in each project
```

The marketplace entry's source is this `plugin/` directory, so only the plugin is copied.

The default branch is `main` until the maintainer makes `stable` the default (which then carries
only released versions). To follow releases only, or to pin one, add a ref after `#`: a branch or a tag.

```bash
claude plugin marketplace add alexlesang/claude-observer#stable
claude plugin marketplace add alexlesang/claude-observer#usage-observer--v0.5.0
```

Update with `claude plugin update usage-observer@usage-observer`, then `/reload-plugins`.

## Requirements

- Claude Code 2.1.287 or newer. Older versions ignore the hooks module without any message, so
  nothing can warn you. For Claude Code Desktop the maintainer's notes say 2.1.286; that figure comes
  from a ticket title and was not tested on Desktop.
  The plugin calls no method and hooks no event that the mods reference ties to a later release
  (checked against the reference as of 2.1.290; `ui.fault` and the `idle` and `waiting` agent statuses
  need 2.1.289, `tool.check` fields 2.1.290 and later, `skill.prompt` source fields and the `Text`
  `weight`, `size`, `mono` and `tabular` props 2.1.299, none of which it uses). A test
  (a test in the repository) fails if a hook starts using one without the minimum being raised
  here. The running version cannot be read before the module loads, so there is no start-up check; the
  plugin records it in `session_start` (`$.session.version`).
- Python 3.10 or newer as `python3`, `python` or `py -3` (probed in that order at session start). The
  plugin pipes its records to a bundled script, `scripts/mod_capture_append.py`. With none found,
  capture is off and one toast names the interpreters tried.
- `git` on the path, to find the project's main checkout (otherwise records go under the session root).

## What it calls in Claude Code

This is a hooks module (a mod): `hooks/hooks.json` lists only `./observer.ts` and wires no command
hook. Every hook only observes: it calls `next(e)` unchanged and its own body is wrapped so a failure
there never reaches the session. An excerpt of `claude plugin validate --strict plugin` (the full
output also lists a "gating hook without .catch" notice for ten of the hooks; the hook and call lists
below are complete):

```
./observer.ts hooks: classic.SessionStart, command.run{command=observer}, ui.render{component=Pane, requestId=usage-observer}, session.start, session.end, classic.*, plugin.register, classic.UserPromptSubmit, classic.SubagentStart, agent.spawn, agent.offer, turn.start, turn.step, turn.complete, tool.call, skill.prompt, prompt.compose, prompt.context, prompt.attachment, tool.describe, session.compact, session.measure, telemetry.log{to=collector}
./observer.ts calls: $.agent.list (via agentType), $.clock.every, $.clock.sleep (via announce, flush), $.command.register, $.env.get (via isCloudSession), $.fs.exists (via checkInstall), $.process.run (via checkInstall, flushNow, resolveHome, runReports), $.session.id, $.session.model (via pushAnnounce), $.session.root (via resolveHome), $.session.version (via pushAnnounce), $.settings.read (via pushAnnounce), $.state.get, $.state.set, $.store.get (via announcedIds), $.store.set (via heartbeat, markAnnounced, unannounce), $.ui.open, $.ui.resolve, $.ui.toast (via warnOnce)
./observer.ts env writes: nothing
./observer.ts env reads: CLAUDE_CODE_REMOTE
./observer.ts hooks telemetry for: collector
./observer.ts state writes: usage-observer.live, usage-observer.view
./observer.ts state reads: usage-observer.live, usage-observer.view
```

In plain words: it reads the session's id, model, version and root, and the `enabledPlugins` list of
the merged settings (to record which plugins were enabled; nothing else of the settings). The hooks module
reads one environment variable, `CLAUDE_CODE_REMOTE`, and writes none (the bundled scripts also read
`CLAUDE_OBSERVER_CAPTURE`, to override the capture path for reports, and `CLAUDE_CONFIG_DIR`). It starts processes only for `git
rev-parse` (to find the main checkout), the Python probe, the bundled appender and the bundled report
scripts. It has no `$.http` call.

## Surfaces

- **`/observer`** opens a pane with two tabs. *This session*: tokens by model, cache hit rate, cost, context
  fill, subagents and most used tools for the current session. *Project*: the tool report over this
  project's capture, and the usage report over this project's session transcripts (see "Reads of your
  other local files").
- **Reports**, from the bundled `usage-reflection` skill or by hand from the project directory:
  `python3 -B <plugin root>/scripts/reflect_report.py <view> [--limit N]`, where `<view>` is one of
  `sessions`, `subagents`, `models`, `tickets` (needs a kanban board), `context`, `fixed-context`,
  `skills`, `plugins`, `digest`. (`personas` is the old name of `subagents` and still runs.) The first
  output line names the capture each number came from. A line `warning: --all-projects reads every
  project's data in the capture` only means no project label was found, so the whole file is read;
  the file is already this project's own.

## Limits

- **Cloud sessions (Claude Code on the web):** no capture is written. The plugin shows one toast,
  `capture is off in cloud sessions`, and still registers `/observer`. Cloud is detected by
  `CLAUDE_CODE_REMOTE=true`; exporting it locally turns capture off in that session. Anthropic's docs
  say a cloud session installs no plugin from `enabledPlugins`, so the plugin may not load there.
- **Claude Code Desktop in a WSL session:** per Anthropic's desktop docs, plugins are not available in
  WSL sessions. The plugin never loads: no capture, no toast.
- **VS Code extension, `claude -p`, the SDK:** hooks run and the capture is written, but the
  `/observer` pane is not drawn; the command answers that the pane is open but not drawn.
- **Organisation policy:** if your organisation restricts which marketplaces or plugins may be
  installed or enabled (`allowManagedModsOnly`, a policy mod reading `plugin.register`), an
  administrator has to allow `usage-observer` first. The plugin cannot override that, and a module
  that never loads cannot say so. A policy mod may also let the plugin load and refuse single calls;
  the calls it makes are the `$.` list above (the `plugin.register` hook only observes). Each is
  tested against a stub policy that refuses it (the plugin's own tests, "organisation
  policy"). What a refusal costs:

  | Refused call | What happens |
  | --- | --- |
  | `process.run` | No capture (no appender, no interpreter probe): one toast, `capture is off`, which names the policy as a cause. Records stay buffered. |
  | `session.id`, `session.root` | No capture: one toast naming the setup (`session.id, session.root or process.run`). |
  | `clock.every` | One toast; the 5 second flush is gone, records are written only at session end (a killed session loses them all). The engine does not report this refusal to the plugin, so the toast comes about 11 seconds in, when no tick has run. |
  | `settings.read` | One toast; every other record is written, `plugin_set` is not. |
  | `session.version`, `session.model` | One toast; `session_start` is not written, the rest is. |
  | `store.get`, `store.set` | One toast; capture goes on. A reload may record a session twice, and the health note is not kept. |
  | `command.register` | One toast; `/observer` is missing, capture goes on. |
  | `agent.list` | One toast; records carry no agent type. |
  | `fs.exists` | No toast, no loss: the install check is skipped, capture goes on. |
  | `env.get` | No toast, no loss: the session counts as local. |
  | `ui.toast` | The toasts above cannot show; capture goes on. The host drops a refused toast itself and logs it. |

  A toast names the call, and the first cost met, once per process (a reload starts afresh). It never repeats the policy's own text.
- Records are buffered in memory (newest 2,000 kept) and flushed every 5 seconds and at session end.
  A session killed between flushes loses the unflushed tail.

## License

MIT. See `LICENSE`.
