# Contributing

Thanks for helping. Issues and pull requests both go through GitHub.

## Issues

Search the existing issues first, then open one with:

- what you did, what you expected and what happened;
- your Claude Code version (`claude --version`), your operating system and your Python version;
- the doctor's output and excerpt (below), not your raw capture.

## Run the doctor before attaching anything

The capture file holds the names, ids and costs of your work. Do not attach it, and do not paste lines from it
by hand. Run the bug-report doctor instead (the `doctor` skill, or `doctor.py` from the plugin's `scripts/`
directory; the README section "Bug reports and the doctor" has the exact commands) and attach only what it
produces: its check lines, and the scrubbed and pseudonymized excerpt it writes to a file. Open that file and
read all of it before you paste anything; the scrub is a filter, not a guarantee, and the doctor never says
"clean".

If you find a way for content or identity to reach the capture, do not post the example in a public issue.
Describe the shape of it without real values, or use GitHub's private vulnerability reporting if the
repository offers it.

## Pull requests

- Open an issue first for anything larger than a small fix, so the direction is agreed before you write code.
- Keep a pull request to one change. Say what it changes and why.
- Keep the plugin to the standard library at runtime (Python 3.10 or newer); a new dependency needs an issue
  first.
- The plugin records names, sizes and counts only. A change must not make it record prompt text, tool input or
  tool output, and must not add a network call.
- Public CI runs on every pull request: `claude plugin validate --strict` on the marketplace and on `plugin/`,
  and an install from the checkout on Linux with a pinned Claude Code version. It must pass.
- The maintainer also runs a fuller private check suite on the pull request's diff before merging. If it
  finds a problem, the maintainer will say what needs to change.

By contributing you agree that your contribution is licensed under the MIT license in [LICENSE](LICENSE).
