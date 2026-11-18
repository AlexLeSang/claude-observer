# Security policy

## Supported versions

Only the latest release, as carried by the `stable` branch, is supported. Fixes go into a new release; older
releases are not patched. The current release is named in [CHANGELOG.md](CHANGELOG.md).

## Reporting a vulnerability

Please report privately, not in a public issue, through GitHub private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**.

Describe the shape of the problem without real values: do not send a capture, a transcript, a prompt or a
path. If you found a way for prompt text, tool input or output, or an account or repository identity to reach
the capture, say which record kind and which field, with invented sample values. You will get an answer from
the maintainer; there is no fixed response time.

## What the plugin reads and writes

This list was checked against the code in this repository. Line numbers move with every edit, so the paths and
functions are named here instead.

**While a session runs (the hooks module `plugin/hooks/observer.ts`):**

- It is handed Claude Code's events in memory, including prompt text and tool input and output, and records
  only names, sizes, counts and token usage from them (`plugin/hooks/records.ts`, for example `toolRecord`,
  `spawnRecord`, `stepRecord`). It makes the engine calls `session.id`, `session.model`, `session.root`,
  `session.version`, `settings.read` (only the list of enabled plugin ids is kept), `env.get` of
  `CLAUDE_CODE_REMOTE`, `fs.exists` on the plugin's own scripts, `fs.stat` of a transcript path
  and of `alert-calibration.json`, `fs.read` of `alert-calibration.json`, `agent.list`, `process.run`, `state`, `store` (`get`, `set`, `delete`), `clock`
  (`now`, `every`, `sleep`), `command.register` and `ui` (`open`, `resolve`, `toast`, `log`, `status`) calls.
  `claude plugin validate --strict plugin` prints the full list of calls.
- `process.run` starts only: `git rev-parse --path-format=absolute --git-common-dir` (to find the main
  checkout), the Python probe (`python3`, `python`, `py -3`, each with `--version`), the bundled appender
  `scripts/mod_capture_append.py`, `tail -c <bytes> -- <transcript>` (below), `git -c core.fsmonitor=false
  ls-files -z -- :(literal,icase)alert-calibration.json` (below) and, from the `/observer` pane, the bundled report
  scripts.
- It reads your session transcripts during a session, which hold prompt text and tool output. After a model
  step it runs `tail -c <bytes> -- <path>` on the main transcript and, for a subagent, on
  `<session folder>/subagents/agent-<id>.jsonl` (an absolute path ending in `.jsonl`, an agent id of letters,
  digits, `_` and `-` only), but only after `fs.stat` says it is a regular file (a pipe or a directory is not
  tailed), with a 3 second timeout, a first window of 64 KiB doubled up to 4 MiB. A link at
  that path is followed. The text is searched for the cache lifetime of the last response and only the class
  `5m`, `1h` or `unknown` is kept; the text itself is not stored, logged or written.
- It reads `alert-calibration.json` from the capture's directory once at session start, with `fs.stat` and
  `fs.read`: only a regular file (not a link) of at most 64 KiB, and only its numeric limits are used. Before
  reading it, the mod runs the `git ls-files` command above in that directory (3 second timeout, once, only when
  such a file is there; the repository's `core.fsmonitor` is switched off so git starts no program from its
  config). If git lists the file, it is tracked by a repository (a clone could ship one) and is ignored: the
  defaults apply. Git missing, failing or not a repository counts as not tracked.
- It keeps counts and labels in the engine's `$.state` and in the machine-wide `$.store`: per session a
  tally of each subagent (token and time sums, a cost sum, an allowlisted type class and model class, the
  `sub<N>` labels, fired marks), keyed by the session id and the agent id; an index of the last 20 session
  ids (older entries are deleted with `store.delete`); a capture health note keyed by the capture path; and
  the ids of the sessions announced per capture path. No prompt, description or tool text is in them.
- It shows the cache line with `ui.status` (the labels `main` and `sub<N>`, cache states and numbers, no id,
  path or model name) and, when a subagent passes its alert limit, one `ui.log` line such as
  `sub1 (Explore, opus) has used ... tokens`: the label, the allowlisted type and model class, counts,
  durations and a dollar figure. Both are shown by Claude Code to you; the plugin does not send them elsewhere.
- It writes, through the appender: the capture `logs/agent-telemetry.mod.jsonl` in the project's main
  checkout (or the path in the `capture` option) as a 0600 file in a 0700 directory it creates; its
  `.lock` sidecar and the rotated copies `<capture>.N` (rotated at 100 MiB, deleted after 90 days);
  the project's `.git/info/exclude` (appends the capture's name when git does not already ignore it; no
  tracked file is touched); and, one file per machine, `~/.claude/observer/quota-latest.json` (mode 0600, rate-limit
  percentages and reset times only) with its `.lock` sidecar.

**Only when you run a report, the doctor or a script by hand:**

- Reports read the capture and its rotated copies. Some also read this project's session transcripts under
  `~/.claude/projects/`, `~/.claude/plugins/installed_plugins.json`, `~/.claude/settings.json`, the project's
  `.claude/settings.json` and `.claude/settings.local.json`, `~/.claude/observer/quota-latest.json` and, if
  present, `~/.claude/observer/projects.json`; the `tickets` view reads the `tasks/*.md` files of a task board
  folder when the project has one. They print counts and names. The exception is `report --failures`, which
  prints tool input and error text from your transcripts: treat that output as session text.
- The doctor (`scripts/doctor.py`) reads the end of the capture, the capture's file mode and git status, runs
  `claude --version`, `git` and the Python probe, and writes a scrubbed excerpt to a private 0600 file (in a
  new 0700 directory under your temporary directory, or the `--out` path you give; it never overwrites).
- `agent_telemetry.py report --digest` writes `agent-digest.md` next to the capture (and excludes it from git);
  `scripts/quota_readings.py add` appends to `~/.claude/observer/quota-readings.jsonl`; `scrub_capture.py` and
  `split_capture.py` rewrite or split captures you name; `agent_alert_calibration.py --write` creates
  `alert-calibration.json` next to the capture (0600, the two limits you agreed and counts from the capture's
  subagent records; it adds the file to `.git/info/exclude`, and it refuses to write when the file is a link or
  is tracked by git).

## What the plugin never does

- It makes no network call: the hooks module has no `http` call, and no bundled Python script imports
  `socket`, `http`, `ssl`, `requests` or a similar module (`urllib.parse` is used for decoding text only).
  The reports that touch your Claude files print counts and send nothing anywhere.
- It does not alter an event or a result: every hook calls `next(e)` with the event unchanged. It does work around a
  call, and a session end waits for the final write to finish; the delay is not measured here.
- It does not record, in its own records, prompt text, tool input or output, a subagent's prompt or description, a hook's command or
  output, file contents, or the plugin's own install directory. The mirrored OpenTelemetry events are the one
  place the filter is by attribute name only: an attribute under a name the plugin does not know is kept (a test showed `command`, `file_path`
  and `project` kept as given; `error` is dropped by name; whether Claude Code puts such attributes on its events is not verified). See
  "Privacy" in [README.md](README.md).
- Text you ask Claude about through the `usage-reflection` and `doctor` skills enters your Claude session and
  so reaches the model like any other session text.

## Local-only

The recording path writes only to the local files above. This was verified by reading the hooks module, the
appender and the helpers it imports; it was not verified on macOS or Windows, and it assumes the Claude Code
that loads the plugin does not itself send the plugin's records anywhere.
