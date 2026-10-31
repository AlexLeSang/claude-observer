---
name: doctor
description: Checks the usage-observer install and capture before a bug report, and writes a scrubbed, pseudonymized excerpt for the user to read and paste. Use when the user wants to report a problem with the plugin or asks if their setup works.
---

# Doctor

Run the plugin's doctor script and relay its checks. The script does the work; this skill only runs it and explains the result. The script itself calls no model and sends nothing anywhere, but on this route its check lines enter the Claude session.

## Run it

Run from the project directory, with the absolute plugin path, unquoted:

`<python> -B ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py`

`<python>` is `python3`; if that is missing or older than 3.10, `python`, then `py -3`. Optional flags, only when the user asks:

- `--capture PATH`: a capture path the user gave you in this conversation, never one found by searching.
- `--excerpt-lines N`: how many recent records the excerpt holds (1 to 50).
- `--out PATH`: where to write the excerpt, which belongs outside the project; it never overwrites a file.

## What it prints

One line per check, each `ok`, `warn`, `fail`, `unknown` or `info` (a check that could not run is `unknown`, never `ok`), and the path of the excerpt file. Exit 0 means every check passed, 1 a `warn` or `fail`, 3 an `unknown`, 2 a bad argument. Quote the lines, say what each means, and say what to do about a `warn` or `fail`.

## The excerpt

- Never read the capture file or the excerpt file yourself, and never print the excerpt's contents: the user is the reviewer, not you. Never ask the user to paste the capture.
- Never read, list or open a kept work directory either, if the user made one: it holds the scrubbed copy of the capture's tail before the excerpt's projection, so it can still hold names. Tell the user not to paste or share it.
- Tell the user to open the excerpt at the path the script printed, read all of it, and paste it into the issue only if they are happy with every line.
- State the caveats plainly: the scrub is a filter, not a guarantee; the excerpt can still hold names or other text; the doctor never says "clean", and no warning is not proof that nothing leaks.
- If the excerpt step reports a refusal, say so: no excerpt was written, and that is the safe outcome. Do not work around it.

## When it stops

- Exit 2, or a message on stderr: quote it and stop.
- A message that Python is too old, or that the checks could not load: quote it. That is itself the finding; the plugin files may be damaged.
