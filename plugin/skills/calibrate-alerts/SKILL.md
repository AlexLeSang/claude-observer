---
name: calibrate-alerts
description: Sets the subagent alert limits (size and time) from this project's own past subagents. Use when the user asks to calibrate or tune the subagent alerts, or when the plugin hints that the alerts still use defaults or an old calibration.
---

# Calibrate the subagent alerts

The subagent alerts fire when one subagent run passes a size limit (tokens) or a time limit (active hours). Without a calibration they use 10,000,000 tokens and 2 hours. This skill shows what other limits would have flagged in this project's own history, and saves the pair the user agrees to. One script does the work; never read the capture or the calibration file yourself.

## Run it

Run from the project directory, with the absolute plugin path, unquoted:

`<python> -B ${CLAUDE_PLUGIN_ROOT}/scripts/agent_alert_calibration.py`

`<python>` is `python3`; if that is missing or older than 3.10, `python`, then `py -3`. Optional flags:

- `--capture PATH`: only a path the user gave you in this conversation, never one found by searching.
- `--count-only`: print only the subagent count, the age of the calibration and a hint. Use it for a quick check.
- `--size-percentile P --hours H --write`: save the pair. See below; never without the user's yes.

## Read the output

Lines are `key: value`. Quote the numbers as printed.

- `past subagents: N from S sessions, D1 to D2`: what the figures rest on. `left out: ... last 10 minutes` counts runs that may still be going.
- `size pNN` and `active pNN`: the spread of past subagents. `size cutoff pNN: C: flags F (X per week)` is how many past runs would have been flagged by a size limit of C tokens; `active over H h: flags F (...)` is the same for a time limit.
- `in force:` is what the alerts use today.
- `recommend:` is the pair that would have flagged at most about 1% of past subagents. Lead with it.
- `not enough history (N subagents, 100 needed)`: say so, say the defaults stay, and stop. Nothing can be saved.

## Ask before you write

1. Give the user the recommended pair first, then the nearby alternatives with their flag counts, so the choice is theirs.
2. Ask which size percentile and which hours limit they want. Do not run `--write` until they have named both or said yes to the recommended pair in this conversation.
3. Run the same command again with `--size-percentile P --hours H --write` using exactly the values they agreed to. Quote the `wrote alert-calibration.json: ...` line.
4. Say the file sits next to the capture, is kept out of git (a copy that git tracks is ignored by the alerts, and the doctor warns), and that the alerts pick it up at the next session start. A new calibration is worth making again when the history has doubled or after 30 days.

## When it stops

- Exit 3 with a message saying no plugin capture was found, or that the file is not a plugin capture (the message names no path): the plugin's `capture` option may be customised. Ask the user for the capture path and pass `--capture PATH`. Exit 3 saying Python is too old: quote it and stop.
- Exit 2: an argument was rejected (the percentile must be from 1 up to but not including 100, hours from 0.1 to 1000, the size limit at least 1 token, and `--write` needs both values). Quote stderr and fix the arguments with the user.
- Exit 1: the file could not be written, or it is tracked by git or a link (the message says which). Quote stderr and stop; for a tracked file the user has to untrack it first (the message names the git step).

## Answer rules

- Use only this project's data and cite the `source:` line for the numbers.
- A flag count is how many past subagents a limit would have flagged, not a prediction.
- Never advise switching models or effort.
