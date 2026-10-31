# claude-observer

`usage-observer` is a Claude Code plugin that records what your sessions spend (models, tokens, cost,
context fill, tools, subagents, skills, plugins) as names, sizes and counts, into a file inside your own
project. It then turns that file into reports that help you decide how to use Claude well: which models,
subagents, skills and plugins are worth their token cost. The plugin itself sends nothing off your machine.

- **Local only.** The plugin opens no port and makes no network call. The capture stays in the project's
  `logs/` directory and is kept out of git. Output you ask Claude about through the skills goes to the model
  like other session text (see "Ask Claude" and "Privacy").
- **Metadata only.** The plugin's own records carry names, sizes, counts and token usage, never prompt text,
  tool input or tool output. Events that Claude Code emits are mirrored and filtered by attribute name; see
  "Privacy".
- **Standard library only.** The bundled scripts need Python 3.10 or newer and nothing else.

## Install

You need Claude Code 2.1.287 or newer (older versions ignore the plugin's hooks module without a message, so nothing is captured), Python 3.10 or
newer, and `git`.

```bash
claude plugin marketplace add alexlesang/claude-observer                 # once per machine
claude plugin install usage-observer@usage-observer --scope project      # in each project, from its root
```

`--scope project` turns the plugin on for this project in `.claude/settings.json`, which you can commit. It does
not carry the marketplace: a teammate still runs `marketplace add` once on their machine (or adds the
marketplace to the project's settings). The install may print "1 userConfig option not yet set". That is not a failure:
the default capture path, `logs/agent-telemetry.mod.jsonl`, works without setting it.

Without a ref, `marketplace add` follows the repository's default branch, `stable`, which carries released
versions only (`main` is development). To pin a release or pick a branch, add a ref after `#`:

```bash
claude plugin marketplace add alexlesang/claude-observer#usage-observer--v0.5.0
```

Update with `claude plugin update usage-observer@usage-observer`, then `/reload-plugins`.

## Use

Start a new Claude Code session in the project (or run `/reload-plugins` in an open one) and work as usual.
The plugin writes its records every few seconds and at session end:

```bash
ls -l logs/agent-telemetry.mod.jsonl     # mode -rw------- (0600), in the main checkout's logs/
```

If nothing appears, `claude --debug` logs every skipped hook, and the plugin shows a toast when writing
fails or no Python is found. See "Limits" for sessions that never capture.

### The `/observer` pane

In a session, `/observer` opens a pane with two tabs. *This session* shows this session's tokens by model, cache hit
rate, cost, context fill, subagents and most used tools. *Project* shows the tool report over this project's
capture and a usage report over this project's session transcripts.

### Ask Claude: the `usage-reflection` skill

Ask about your own usage in plain words ("why was that session expensive?", "which plugin costs me the
most?") and the bundled `usage-reflection` skill runs the right report and explains the numbers. The skill
runs the report inside your Claude session, so the output (session ids, plugin, skill and subagent names,
costs) goes to the model like any other session text.

### Report views, by hand

From the project directory, with `<plugin root>` being
`~/.claude/plugins/cache/usage-observer/usage-observer/<version>/`. Find the newest installed version with:

```bash
ls -d ~/.claude/plugins/cache/usage-observer/usage-observer/*/ | grep -E '/[0-9.]+/$' | sort -V | tail -1
```

(`sort -V` puts `0.10.0` after `0.5.0`; the `grep` skips any folder not named like a version.) Then run:

```bash
python3 -B <plugin root>/scripts/reflect_report.py sessions
python3 -B <plugin root>/scripts/reflect_report.py digest
```

The views are:

| View | Shows |
| --- | --- |
| `sessions` | One row per session: cost, tokens, cache hit rate, models and effort, peak context, compactions |
| `subagents` | Usage per subagent type: tokens, cost, iterations, tool use |
| `models` | Model and effort comparison: iterations, output tokens, cost, stop reasons |
| `tickets` | Cost per ticket; needs a task board of Markdown files in the project (`plugin/README.md` names the folder); without one it exits 3 with a message saying so |
| `context` | Context fill over time and what each compaction re-created |
| `fixed-context` | What every request carries before your prompt: system prompt, tools, attachments |
| `skills` | Skill activations and each skill's load overhead |
| `plugins` | Loaded plugins, session cost with and without each, hook overhead |
| `digest` | A Markdown summary of the last 7 days, printed to the terminal |

`personas` is an older name of `subagents` and gives the same output. The first output line names the capture each number came from. A line saying `--all-projects reads every
project's data in the capture` only means no project label was found, so the whole file is read; the file is
already this project's own. Every view takes `--limit N` for the rows per list. Each view measures cost, not
outcome: a cheaper model is not a better one.

## Bug reports and the doctor

Before you attach anything to an issue, run the doctor. It reads your setup and your capture, changes nothing
in them, and reports each check as `ok`, `warn`, `fail`, `unknown` or `info` (a check that could not run is
`unknown`, never `ok`). It is a script in the plugin, plus a thin skill that runs it. The script uses no model
and sends nothing anywhere. If you run it through the skill, the check lines it prints (never the excerpt) appear
in your Claude session, and so go to the API.

From the project directory, with `<plugin root>` as in "Report views, by hand":

```bash
python3 -B <plugin root>/scripts/doctor.py                      # checks, then writes the excerpt
python3 -B <plugin root>/scripts/doctor.py --json               # the same, as one JSON object
python3 -B <plugin root>/scripts/doctor.py --excerpt-lines 20   # records in the excerpt (default 10, at most 50)
python3 -B <plugin root>/scripts/doctor.py --out /tmp/doctor-excerpt.md   # write the excerpt there (never overwrites)
python3 -B <plugin root>/scripts/doctor.py --capture PATH       # a capture other than the plugin's
```

Or ask Claude to run the `doctor` skill ("run the usage-observer doctor"). The skill runs the script, relays
the checks and tells you to read the excerpt yourself; it never opens the capture or the excerpt. Exit code 0
means every check passed, 1 a `warn` or `fail` (or the checks could not be loaded, which also prints a message),
3 an `unknown`, 2 a bad argument or a Python older than 3.10 (with a message). Put `--out` outside your project:
a file inside it, such as `excerpt.md`, is not ignored by git and `git add -A` would commit it.

The checks:

- your Claude Code version against the plugin's minimum;
- that Python 3.10 or newer is found, by the same probe order the plugin uses;
- the capture file: present, mode, writable, age;
- that git ignores the capture;
- whether the session is a cloud session or WSL, where the plugin captures nothing;
- whether the capture still holds any content or identity attributes (counts only).

Then it builds the excerpt: a copy of the end of your capture, scrubbed and checked again, cut down to shape
and failure information (record counts, the Claude Code version, models, the latest records with numeric and
enum fields only), with your own names for sessions, plugins, MCP servers, skills and subagents replaced by
stable pseudonyms such as `skill-1`, and times cut to the minute. It is written to a private file (mode 0600)
and only the file's path is printed. If the check on the scrubbed copy is not clean, no excerpt is produced.

Caveats. The doctor is advisory and never says "clean": the scrub finds key-style spellings only, and the
excerpt is built from a fixed list of fields, which is a design and not a guarantee. A name, a path or other
text can still get through. Open the excerpt, read all of it, and paste it into the issue only if you are
happy with every line. The `--keep` flag leaves the scrubbed, unprojected working copy on disk; never share it.

## Privacy

- **What is captured.** One JSON line per event: session start and end, turns, model requests (model, token
  counts by type, cost), tool calls (tool name, outcome, sizes), skill activations, subagent dispatches (type,
  model, sizes), compactions, context and prompt sizes, hook time, and the OpenTelemetry events Claude Code
  itself emits, mirrored.
- **What is never captured by the plugin's own records.** Prompt text, tool input, tool output, a subagent's
  prompt or description, a hook's command or output. Identity attributes of the mirrored events (email,
  account and organization ids, repository owner, name and URL) are dropped by name.
- **Where it goes.** `logs/agent-telemetry.mod.jsonl` in the project's main checkout, created mode 0600 in a
  0700 directory, rotated by size and pruned by age. If git does not already ignore it, the plugin adds it to
  the repository's local `.git/info/exclude` (never a tracked file). Change the path with the `capture`
  plugin option.
- **Reports read more than the capture.** Some views also open this project's session transcripts under
  `~/.claude/projects/`, and your Claude settings files, to name subagents and plugins. They print counts and
  names and send nothing anywhere.
- **Scrubbing is a filter, not a guarantee.** Records are cleaned by attribute name, in key-style spellings.
  An attribute under another name is kept, so the capture can hold whatever else Claude Code puts in its
  events, and free text that holds a value in an unusual shape is written as it is. Names you chose (session
  ids, plugin and marketplace ids, MCP server names, custom skill and subagent names) stay in the capture
  and identify your work even though they are not content. Treat the capture as private, and read anything
  you share.

The full list of recorded attributes and what the plugin calls in Claude Code is in
[`plugin/README.md`](plugin/README.md).

## Limits

- **claude.ai chat** runs no plugin hooks, so nothing is captured there. Only the `usage-reflection` skill
  applies, and it has no capture to read: it can explain report output you paste in, not produce a report.
- **Cowork** is unverified: nobody has confirmed that the plugin loads or captures there.
- **Cloud sessions (Claude Code on the web)** write no capture. The plugin shows one toast, `capture is off
  in cloud sessions`. Cloud is detected by `CLAUDE_CODE_REMOTE=true`; exporting that variable locally turns
  capture off in that session.
- **Claude Code Desktop in a WSL session:** Anthropic's docs say plugins are not available there, so the
  plugin never loads: no capture, no toast.
- **VS Code extension, `claude -p`, the SDK:** hooks run and the capture is written, but the `/observer` pane
  is not drawn.
- **Platforms:** Linux first. macOS and Windows are not tested.
- **Organisation policy:** if your organisation restricts which marketplaces or plugins may be installed, an
  administrator has to allow `usage-observer` first; a plugin that never loads cannot say so.

## Uninstall

```bash
claude plugin uninstall usage-observer@usage-observer --scope project    # in each project you installed it in
claude plugin marketplace remove usage-observer                           # once per machine, optional
```

To only pause it, `claude plugin disable usage-observer@usage-observer --scope project`. Uninstalling leaves
your captures in place: delete `logs/agent-telemetry.mod.jsonl` and its rotated `.N` files by hand if you no
longer want them, and the lines the plugin added to `.git/info/exclude` are harmless to keep.

## Similar projects

Several community and Anthropic tools also measure Claude Code usage, and many of them do it well.
claude-observer focuses on per-session decisions: fixed-context and plugin overhead, subagents, and model
and effort, with all data kept in the project's own `logs/`.

- [ccusage](https://github.com/ccusage/ccusage): CLI reports of tokens and cost by day, session and 5-hour block.
- [claude-hud](https://github.com/jarrodwatts/claude-hud) and
  [ccstatusline](https://github.com/sirmalloc/ccstatusline): live status lines for context, limits and cost.
- [session-report](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/session-report)
  (Anthropic): a point-in-time HTML report from transcripts.
- [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor): a live monitor of
  the 5-hour window.
- [claude-code-otel](https://github.com/ColeMurray/claude-code-otel),
  [Dash0](https://github.com/dash0hq/dash0-agent-plugin) and
  [Opik](https://github.com/comet-ml/opik-claude-code-plugin): send traces or metrics to an observability
  backend rather than keeping local files.

Claude Code's own `/usage` (last 24 hours or 7 days), `/context` (the current session) and `/insights`
(recent sessions) are the baseline; claude-observer keeps a longer, per-project history.

## Contributing

Issues and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Release notes are in
[CHANGELOG.md](CHANGELOG.md).

## License

MIT, see [LICENSE](LICENSE).
