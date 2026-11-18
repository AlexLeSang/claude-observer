# Changelog

All notable changes to the `usage-observer` plugin. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are the `version` in
`plugin/.claude-plugin/plugin.json`; release tags are `usage-observer--vX.Y.Z`.

## [Unreleased]

## [0.7.0] - 2026-11-18

A cache line that shows how long each prompt cache stays warm and what a cold return costs, and alerts for runaway
subagents, both on by default and both only observing. Captures no longer keep the full text of tool and API errors.
No record shape was removed or changed: existing captures keep working.

### Added

- A cache line, the plugin's own status line under the prompt (seen as a row above your own `statusLine` with `fullscreenLayout` on; only the terminal was checked): for
  the main session and up to three subagents, how long each prompt cache stays warm and what a cold return costs
  (`cache main 1h, 59m left, cold $1.00 | sub1 5m, 2:10 left`). A cache turns urgent (`URGENT 0:55 left`) 1 minute
  before a 5-minute cache expires and 10 minutes before a 1-hour cache expires, and reads `EXPIRED` with the price
  after. The lifetime is read again after every step, so a drop from 1 hour to 5 minutes is followed; an unknown
  lifetime counts as 5 minutes, and while a cache is on 5 minutes and you are at or past a plan limit the line says so. It is on by default; the `cacheLine`
  option turns it off. It is built from fixed words, the labels `main` and `sub<N>`, and numbers only, and it only
  observes. On Claude Code 2.1.322 with `fullscreenLayout` on, the harness puts a warning glyph and the plugin's name in front of it and hides
  it in the detailed transcript view and under a dialog; that the option applies without a reload, and that the row
  then clears, is designed but not verified on a live session.
- Subagent alerts: one line in the transcript and one in the `/observer` pane when a subagent passes a size limit
  (tokens used) or a time limit (active time), each alert at most once per subagent (a restart before the tally is saved can repeat it). Until calibrated the limits are 10,000,000
  tokens and 2 hours. The `subagentAlerts` option turns them off. Nothing is stopped, killed or sent.
- The `calibrate-alerts` skill and `scripts/agent_alert_calibration.py` show what other limits would have flagged
  among your own past subagents and, after you agree to the numbers, write `alert-calibration.json` next to the
  capture (0600; it needs at least 100 past subagents). A timestamp without a zone counts as UTC, and values that are
  impossible (a percentile outside 1 to under 100, fewer than 0.1 hour, or a size limit below 1 token) are ignored.
  The doctor gains a check of that file (a huge number does not crash it), and the Project tab a hint when the
  alerts still use the defaults or an old calibration.
- The `usage-reflection` skill explains the cache line and the subagent alerts from the text you paste, and hands
  limit changes to `calibrate-alerts`.
- The plugin reads the end of the session transcript (and a subagent's own) after a model step, only to find the
  cache lifetime; it keeps the class `5m`, `1h` or `unknown` and nothing of the text. `SECURITY.md` and the plugin
  README list every engine call and file read.
- The reports that price usage know Claude Fable 5.2 and Claude Mythos 5.2 ($8 per million input tokens, $0.20 for
  cache reads). The usage and quota reports also price their fast mode at twice the standard rate. Before this, their
  messages were listed as models without a price and left out of every total.

### Fixed

- The cache line no longer holds up the step that triggered it: the transcript tail read now runs in the background,
  and a transcript path that is not a regular file (a FIFO, a directory) is not read.
- An `alert-calibration.json` that git tracks is ignored: the alert limits stay at the defaults, `/doctor` warns, and
  `scripts/agent_alert_calibration.py --write` refuses a tracked file or a symbolic link. The check runs `git ls-files`
  once per session start, only when a regular file is there.
- The plugin now passes `claude plugin validate --strict` on Claude Code 2.1.323, and its hooks load on 2.1.287, the
  declared minimum.
- The mirror and the appender now drop the OTel `error` attribute (the full error message of a failed tool
  call or API error, which Claude Code adds when `OTEL_LOG_TOOL_DETAILS` is set) by name, like the other content
  attributes. The `error_type` attribute and the plugin's own boolean `error` field stay. `/doctor` now flags older captures that hold `error` entries; run
  `scripts/scrub_capture.py` to remove them.
- Cross-file dedupe no longer crashes on a transcript line whose token count is an integer too large for a
  float; the count ranks as 0.
- The report scripts that read a subagent's description file (`*.meta.json`) now refuse a FIFO, a link or an
  oversized file instead of hanging, following the link or crashing, and the ticket report reads transcripts without
  following a session or subagents folder swapped for a link. A subagent with no agent id counts as the main loop in
  the iteration, token and context reports too.
- `reflect_report.py` prints a `--capture` path escaped, so control characters in it cannot reach your terminal, and
  its refusal for a missing task board no longer prints an absolute path.
- The reasoning report now prices a fast-mode message at twice the standard rate on the models that have fast mode
  (Fable 5.2, Mythos 5.2, Opus 5.5, Opus 5 and Opus 4.8), as the usage and quota reports already did. Its estimated
  reasoning cost was half the real figure on those messages.
- The cache-miss report now prices a miss at twice the standard figure when the request that missed ran in fast mode
  (its `api_request` `speed` is exactly `fast`, on a model that has fast mode); its dollar figure was half the real one
  on those misses.
- The skill-overhead report now says, under its table, that it prices every request at the standard rate, so its
  dollar figures are a lower bound on a fast-mode request (the plugin's step record documents no speed). The figures
  themselves are unchanged.

## [0.6.0] - 2026-11-05

New views for deciding how to use Claude: your costliest prompts, failing hooks and tool errors, per-session decision
hints with a cache-miss breakdown, and a quota pace line. The plugin records two new things, each prompt's origin kind
and the latest rate-limit reading, and never their text. No record shape was removed or changed: existing captures
keep working.

### Added

- `report --prompts` (and a `prompts` view for the `usage-reflection` skill) ranks your costliest prompts with the
  cost of the subagents each one dispatched rolled up, instead of charging a background subagent to whatever you
  typed next. Subagent cost that cannot be tied to a prompt is shown apart, with coverage, a per-version table and
  two checks. `--json` prints the same figures with full ids; it never prints prompt text.
- The plugin records the origin kind of each submitted prompt (`composer`, `sdk`, `task-notification`,
  `scheduled-trigger`, `peer` or `other`; never the prompt or the rest of its origin) as a `prompt_submit`
  record, so a prompt you did not type can be labelled.
- `report --friction` lists failing hook executions per hook and recurring tool error signatures (tool name and
  error kind, never error text), and reads your transcripts to name the plugin behind each failed hook run and to
  compare its count with the capture's. A transcript that cannot be read, or that ends on a cut-off line, is
  counted and noted, never skipped silently. `--json` prints the same figures and `--limit N` caps the signature
  table.
- `report --sessions` prints decision hints under the table: cache reads short of the cache, gaps longer than the
  cache lifetime, a low cache-read share, and a large side-request or subagent share of cost. Each hint says what
  it counted.
- The plugin records the account's rate-limit percentages and reset times in `~/.claude/observer/quota-latest.json`
  (percentages and times only). `scripts/quota_latest.py` prints them with their age and `scripts/quota_report.py
  --pace` shows used share against elapsed share of each window. For the five-hour window it adds one plain linear
  line, labelled as arithmetic ("at this pace you reach N% at reset / 100% at HH:MM UTC"), with the backtest result
  beside it: on 37 five-hour windows no method beat assuming no growth, and this pace overshot, so on them it was worse
  than no growth. It prints "no line" where the arithmetic would be wrong.

- `report --sessions` ends with a "Cache misses" block: requests that read short of the previous request's cache
  read plus write, per loop (the main thread, and one loop per subagent `agent_id`), with what each miss followed (a
  model switch, a gap past the cache lifetime, an effort change, else unexplained), counts, tokens and an estimated
  dollar shortfall. A subagent request without an `agent_id` is matched to the one native step with the same token
  counts within 120 seconds; those misses are labelled a lower bound and kept out of the totals, and requests that
  match no step are counted as not attributable.

### Changed

- `report --json` is refused (exit 2) when `--sessions`, `--tickets` or `--digest` would override the view.

- The `sessions` hint block says when a session has a subagent compaction, because the hints excuse every loop of
  such a session and the cache-miss block only that agent.

### Fixed

- `report --friction` refuses a transcript that is a symlink or not a regular file (a FIFO no longer hangs it) and
  counts it as unreadable. An error count it cannot read now counts as unreadable instead of being skipped silently.
- The plugin's install check names every script the appender imports (`quota_latest.py`, `instant_util.py` and
  `safe_name.py` were missing), so a partial install is reported.
- `report --sessions` counts a mirrored request once, not twice.
- `quota_report.py --pace` counts elapsed time and the linear line from the time of the reading, not from now, and
  `--json` output of `report --prompts` rounds its figures to six places.
- The `prompts` spawn-depth legend states that a broken chain counts the spawns found.

## [0.5.0] - 2026-10-31

First public release, under the MIT license. Numbering stays at 0.x for now; 1.0.0 will come with a
compatibility promise for the record shapes and the report flags. The `stable` branch carries released
versions and is the repository's default branch, so `claude plugin marketplace add` without a ref follows
releases. No record shape was removed: existing captures keep working.

### Added

- A `doctor` skill and script for bug reports. It checks your setup and your capture without changing
  them (Claude Code and Python versions, the capture file, whether git ignores it, cloud or WSL, leftover
  content or identity attributes) and writes a short excerpt of recent records to a private file for you
  to read and paste. The excerpt keeps names, counts and timings only, with your own names replaced by
  pseudonyms. Run `python3 -B <plugin root>/scripts/doctor.py` or ask Claude to run the `doctor` skill.
- Cloud sessions (Claude Code on the web) capture nothing. The plugin shows one toast, and `/observer`
  still registers.
- The capture is kept out of git. If git does not already ignore it, the plugin adds it to the
  repository's local `.git/info/exclude`. No tracked file is changed. If that fails, a toast tells you.
- The `digest` view prints its Markdown summary to the terminal and writes no file.
- MIT `LICENSE`, in the repository and in the installed plugin.

### Changed

- 'Persona' is now 'subagent type', Claude Code's own word, in the reports and the `usage-reflection`
  skill. The new view is `subagents`; the old `personas` view and flag still work as aliases with the
  same output.
- Stricter capture scrub. Content attributes (prompt, response, tool input and output) and identity
  attributes (email, account and organization ids, repository owner, name and URL) are now also dropped
  when they are nested inside a value, written in another letter case, or spelled with escapes or
  invisible characters. A capture line that cannot be read is replaced by a marker.
- Skill and subagent type names are cleaned and capped at 64 characters, so a capture cannot inject
  terminal escapes or extra lines into a report.
- Side requests made by a prompt hook are labelled in the `sessions` and iteration reports, with their
  share of cost.

### Fixed

- Reports find their session transcripts for projects with non-ASCII or very long paths. The project
  folder name now matches Claude Code's.
- Reports no longer crash on a corrupt or cut-off capture line, invalid UTF-8 or huge numbers. They
  count such lines and say so in a note.
- The project label is read only from a `.claude/settings.json` that you own and that is not group- or
  world-writable.
- The `plugins` view no longer counts headless runs without a model request as sessions.
- The `digest` view marks a session cut by the time window as clipped instead of misreporting it.
- A session resumed after a crash records its start again.

### Removed

- The legacy OTLP receiver and its service installer. The plugin mirrors Claude Code's OTel events
  in-process and does not need the OTel export. Reports and the scrub still read captures the receiver
  wrote.

### Known caveats

- The capture scrub works by attribute name. It is a filter, not a guarantee: an attribute under
  another name is kept, and free text in an unusual shape is written as it is. Treat the capture as
  private and read anything you share.
- The `doctor` excerpt, which you may paste into an issue, copies numbers as written in your capture
  (integers up to 10^12, and up to 12 digits inside model and version strings). Only a program running
  as you could plant a number there. Read the excerpt before pasting.

## Earlier versions

0.1.0 to 0.4.0: pre-public development; not published.
