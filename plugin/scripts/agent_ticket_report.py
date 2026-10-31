"""One row per kanban ticket for `agent_telemetry.py report --tickets`.

Headless executors (`query_source=sdk`) each work one ticket, and `report
--sessions` shows their cost per session but not which ticket a session was.
This view attributes sessions to tickets and sums them, so the operator can
compare ticket types and choose a model and effort per type.

Sessions come from the same capture and data layer as `report --sessions`
(`agent_session_report`): cost, requests, iterations, models, effort and the
first and last timestamp. Costs are the capture's own `cost_usd`; no price is
computed here.

Attribution, per session that made at least one `sdk` request, from the
session's local transcript (the project's transcript directory and its
`.claude/worktrees/*` siblings), first match wins and the winner is kept on
the row as the attribution `source`:

1. `prompt`: the first user prompt names the ticket. Strong signals first
   (`task/<id>-...`, `ticket <id>`, `kanban #<id>`; earliest wins), then a bare
   `#<id>` (not `C#1`, `&#123;`, hex colours or digit runs) as a last resort. Ids
   are 1 to 5 digits everywhere. Only the real prompt text counts
   (`real_prompt_text`): injected `<system-reminder>` and other context blocks
   are removed first, here and for the phase (nested and upper-case tags too; a slash
   command's `<command-args>` text is kept). A strong reference with an id of more than five
   digits rejects the prompt as a whole: the bare `#N` fallback does not stand in for it. Bare
   `#N` (five digits) is accepted as an id.
2. `branch`: the transcript's `gitBranch` is `task/<id>-...`.
3. `commit`: Bash `git commit` commands whose text carries `(kanban #<id>)`;
   the most frequent id wins.

A session with no sdk request (interactive) is attributed by 2 and 3 only, marked `interactive-branch` /
`interactive-commit` in `source`: a person's chat names tickets in passing, so its prompt never counts.

Every other session goes into an `(unattributed...)` row, split by why so a missing transcript shows, and the rows add up
to `report --sessions`: `(unattributed: interactive)` (no sdk request, no branch or commit matched),
`(unattributed: sdk, no transcript)` (no transcript was found for it) and `(unattributed)` (an sdk transcript that
names nothing). Wall time is still first-to-last request; an active-time column is not added.

Phase (per session, from the first user prompt only; `classify_phase`): `fix`
when it asks to fix review findings, `review` when it is an adversarial or
review-round prompt or a slash command with `review` in its name (`/code-review`; the name is never
printed), else `build` (the executor prompt of the drain script, since removed, is always
build). Only the first 400 characters of the real prompt text are searched. Each ticket row shows the
cost and requests per phase (`$cost/requests`, `$x+` or `-` as in the cost column) next to the total, so a ticket's
review cost sits beside its build cost. Rows of `(unattributed)` are not split.

Subagent runs: the Agent tool's subagents export under the launching session's id, so an
orchestrator session holds executors and reviewers of many tickets. Their requests are tied to the
subagent transcript `<session>/subagents/agent-<id>.jsonl` (an `api_request`'s `request_id` equals a
transcript record's `requestId`; a native step carries the agent id). The run's ticket is, first match
wins: its `.meta.json` `description`, its dispatch prompt (first user message), a `task/<id>-` or
`kanban-<id>` `gitBranch` on its records; the same `explicit_ticket` rule as `--iterations` for the first two. Each run becomes its
own piece of the ticket (`runs` column, source `description`, `dispatch` or `sub-branch`), and its phase is
`review` for a subagent whose type name has `review` in it, `fix` for a `fix/r<N>` branch, else the
dispatch's own words (`classify_phase`), else the launching session's phase. A run with no ticket of its
own in an attributed (sdk) session joins that session's ticket. The plugin's `agent_spawn` records add the ticket id and agent type of a native step's
subagent when no transcript is read (source `spawn`; a transcript's own ticket and type win). Requests that cannot be tied to a
transcript, side requests of a subagent (summaries) and runs naming no ticket stay in their session
(unattributed for an interactive one). No cwd is read: a worktree directory name carries no
documented ticket format.

Outcome status: with `--board <kanban dir>` the `status:` value (quoted or not,
multi-word) of each `tasks/<id>-*.md` front matter; without it, or for an id the
board does not list, `?`. At most 4096 bytes are read per file; a file must start
with `---` (a UTF-8 BOM is accepted); symlinks and non-regular files are skipped.
When two files carry the same id (`043-a.md`, `43-b.md`) the first in filename
order that has a status wins. A trailing ` # comment` is dropped from the status; a `--board` path
without a `tasks` directory prints a note on stderr.

Transcripts are read defensively: a symlinked session directory, `subagents` directory, worktree sibling,
transcript or `.meta.json` is not followed, a line over MAX_LINE_BYTES is skipped unread, at most
MAX_TRANSCRIPT_BYTES of a transcript and META_MAX_BYTES of a `.meta.json` are read.
A dropped line (over the cap, or not JSON) before the first user record means that record is not known to be the
first prompt, so the session or run has no prompt rather than a later one. A `kanban-<id>` subagent branch
whose id is followed by another number (`kanban-2024-10-x`) is a date, not a ticket.

Privacy: only the extracted ticket id leaves the transcript. Prompt text, the
branch name and commit text are never printed, and neither are session ids,
project labels or paths.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import re
import stat
import sys
from array import array
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_iteration_report import format_table, plural
from agent_session_report import Capture, Request, Session, Spawn
from agent_session_report import build_report as build_session_report
from safe_name import clean_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

UNATTRIBUTED: Final[str] = "(unattributed)"  # an sdk session whose transcript was read and names nothing
UNATTRIBUTED_INTERACTIVE: Final[str] = "(unattributed: interactive)"
UNATTRIBUTED_NO_TRANSCRIPT: Final[str] = "(unattributed: sdk, no transcript)"
INTERACTIVE_PREFIX: Final[str] = "interactive-"
UNKNOWN_STATUS: Final[str] = "?"
DEFAULT_TICKET_LIMIT: Final[int] = 20
NONE: Final[str] = "-"
MODEL_PREFIX: Final[str] = "claude-"
SDK_SOURCE: Final[str] = "sdk"


def is_unattributed(ticket: str) -> bool:
    return ticket.startswith(UNATTRIBUTED[:-1])


def ticket_sort_key(ticket: str) -> tuple[bool, int, str]:
    """Attributed tickets first, by number, then by name. ASCII digits only: `str.isdigit` also accepts `\u00b2`,
    which `int` rejects."""
    numeric = ticket.isascii() and ticket.isdigit()
    return is_unattributed(ticket), int(ticket) if numeric else 0, ticket


# Ids are 1 to 5 digits (a ticket number never needs more, and `int()` of a long digit run raises).
# ASCII digits only: `\d` would take `\u0663` (Arabic-Indic three) and `int()` would turn it into 3.
# Every use of `_ID` is followed by `-`, `)` or `(?!\w)`, so no `(?!\d)` is needed here.
# A bare `#N` is accepted as a ticket id: five digits is indistinguishable from a small id.
_ID: Final[str] = r"([0-9]{1,5})"
MAX_ID_DIGITS: Final[int] = 5
# Every explicit reference to a ticket, in one pass so each position counts once: `task/N-`,
# `ticket N` and `kanban N` (these capture the whole digit run, so one of 6+ digits is seen and
# rejected as a whole) and a bare `#N` that is not `C#1`, `&#39;`, a hex colour or a longer run.
_REF_RE: Final[re.Pattern[str]] = re.compile(
    rf"task/([0-9]+)-|\bticket\s+#?([0-9]+)(?!\w)|\bkanban\s+#?([0-9]+)(?!\w)|(?<![\w#&])#{_ID}(?!\w)",
    re.IGNORECASE,
)
# A dispatch prompt names its ticket up front: at most this many words (none with a `#`, which
# would be a colour, `C#1` or another reference) come before it. A `/` is fine: a path or URL is a word.
PROMPT_LEAD_WORDS: Final[int] = 8
_BARE_GROUP: Final[int] = 4  # the group of `_REF_RE` that is a bare `#N`
_LEAD_RE: Final[re.Pattern[str]] = re.compile(rf"(?:[^\s#]+\s+){{0,{PROMPT_LEAD_WORDS}}}")
# `ticket 5-6`, `Ticket 5,6`, `#N/6`: a range or list, so not one ticket (the `#N` form is two ids already).
_RANGE_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:ticket|kanban)\s+#?[0-9]+[-\u2013,/]#?[0-9]", re.IGNORECASE)
# A reviewer subagent: a name with a word starting `review` (`code-reviewer`, `pr-review-toolkit:x`), not one
# that merely contains it (`preview-builder`). An underscore or a digit before it does not start a word. A
# capital `R` after a lower-case letter starts one too (`codeReviewer`); `PreviewBuilder` does not.
_REVIEWER_RE: Final[re.Pattern[str]] = re.compile(r"(?<![^\W_])(?i:review)|(?<=[a-z])Review")
_BRANCH_RE: Final[re.Pattern[str]] = re.compile(rf"^task/{_ID}-")
# A subagent's own branch may also be `kanban-<id>` or `kanban-<id>-...` (`kanban-133-fix2`).
# `kanban-2024-10-x` is a date, not ticket 2024: an id followed by another number is no id.
_SUB_BRANCH_RE: Final[re.Pattern[str]] = re.compile(rf"^(?:task/{_ID}-|kanban-{_ID}(?:-(?![0-9]+(?:-|$))|$))")
# A fix round's branch (`fix/r2-...`): a subagent working there is in the fix phase. Its name lists
# several tickets at most, so it never names one.
_FIX_BRANCH_RE: Final[re.Pattern[str]] = re.compile(r"^fix/r[0-9]+(?:-|$)")
_COMMIT_RE: Final[re.Pattern[str]] = re.compile(rf"\(kanban #{_ID}\)")
STATUS_READ_BYTES: Final[int] = 4096
# Caps on what is read from a transcript: a longer line is skipped without being held in memory, and
# reading stops after this many bytes. A `.meta.json` over its cap is ignored.
MAX_LINE_BYTES: Final[int] = 8 * 1024 * 1024
MAX_TRANSCRIPT_BYTES: Final[int] = 512 * 1024 * 1024
META_MAX_BYTES: Final[int] = 64 * 1024
_READ_CHUNK: Final[int] = 64 * 1024
STATUS_MAX_CHARS: Final[int] = 32
# Blocks Claude Code injects into a user message besides what the user typed.
_CONTEXT_TAGS: Final[str] = (
    r"system-reminder|command-name|command-message|command-args|local-command-\w+|ide_\w+|user-prompt-submit-hook"
)
_OPEN_RE: Final[re.Pattern[str]] = re.compile(rf"<({_CONTEXT_TAGS})\b", re.IGNORECASE)
# One pass over the tags of a block: a complete opener (`<name ...>`, attributes allowed) or a
# closer (`</name >`). A mention in prose (`<name` with no `>`, `<name-old>`) is neither.
_TAG_RE: Final[re.Pattern[str]] = re.compile(
    rf"<(?:/({_CONTEXT_TAGS})(?![\w-])\s*>|({_CONTEXT_TAGS})(?![\w-])[^<>]{{0,200}}>)", re.IGNORECASE
)
_NOT_SPACE_RE: Final[re.Pattern[str]] = re.compile(r"\S")
# The command a slash-invoked session starts with; only the review test reads it, nothing prints it.
_COMMAND_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:<command-message>[^<]{0,200}</command-message>\s*)?<command-name>\s*/?([\w:.-]{1,100})\s*</command-name>",
    re.IGNORECASE,
)
_REVIEW_COMMAND_RE: Final[re.Pattern[str]] = re.compile(r"(?<![a-z0-9])review(?![a-z0-9])", re.IGNORECASE)
# The user's own words inside a slash command: only the wrapper tags are removed.
_KEEP_CONTENT_TAG: Final[str] = "command-args"
_KEEP_CLOSE_RE: Final[re.Pattern[str]] = re.compile(rf"</{_KEEP_CONTENT_TAG}\s*>", re.IGNORECASE)
BUILD: Final[str] = "build"
REVIEW: Final[str] = "review"
FIX: Final[str] = "fix"
PHASES: Final[tuple[str, ...]] = (BUILD, REVIEW, FIX)
ACTIVE_GAP_SECONDS: Final[float] = 300.0  # a gap between requests longer than this is idle
# The prompt `drain_tickets.py` rendered starts with this; it is always a build session, whatever
# the ticket body says. The script has since been removed; the marker stays for sessions in old captures.
EXECUTOR_MARKER: Final[str] = "You are the executor for kanban ticket"
PHASE_WINDOW: Final[int] = 400
_FIX_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:fix|address|resolve)\s+(?:the\s+|all\s+)?(?:adversarial\s+|review\s+)?findings\b", re.IGNORECASE
)
_REVIEW_RE: Final[re.Pattern[str]] = re.compile(
    r"\badversarial\b|\breview\s+round\b|\breview\s+the\b|\breviewer\b|\breview\s+of\b", re.IGNORECASE
)
_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"(?:\A|\s+)#.*\Z")
_TASK_FILE_RE: Final[re.Pattern[str]] = re.compile(rf"^{_ID}-.*\.md$")


# --- attribution (pure) ------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Attribution:
    ticket: str
    source: str  # "prompt", "branch" or "commit"


def explicit_ticket(text: str | None, *, anchored: bool) -> str | None:
    """The ticket `text` names explicitly, as a plain digit string, or None.

    The one rule every report uses (usage, iterations, tickets). An explicit reference
    is `task/N-`, `ticket N`, `kanban N` / `kanban #N` or a bare `#N`. The text must name
    exactly one ticket: two different ids (`#N`), an id of more than five
    digits or the id 0 leave it unattributed; `5` and `05` are one id, and a range or list after the
    keyword (`ticket 5-6`, `ticket 5,6`) names no single ticket. A leading BOM is ignored. Digits are ASCII `0-9` only. `anchored` (a dispatch prompt, which quotes plenty of
    other text) also requires the reference to come first, after at most
    `PROMPT_LEAD_WORDS` words (any text, but none with a `#`), or a bare `#N` to be the first thing; a dispatch description is short and not anchored.
    """
    if not text:
        return None
    text = text.lstrip("\ufeff")  # a BOM would count as a word before a first-thing `#N`
    if _RANGE_RE.search(text):
        return None
    found = list(_REF_RE.finditer(text))
    raw = {next(g for g in m.groups() if g) for m in found}
    if any(len(d) > MAX_ID_DIGITS for d in raw):
        return None
    ids = {_plain(d) for d in raw}  # `5` and `05` are one ticket
    if len(ids) != 1:
        return None
    digits = ids.pop()
    if digits == "0":  # board ids start at 1
        return None
    if anchored:
        first = found[0]
        head = text[: first.start()].lstrip()
        # A bare `#N` counts only as the very first thing (`#N: fix it`): mid-sentence it is as
        # likely a colour (`#N`) or a PR number as a ticket.
        if not _LEAD_RE.fullmatch(head) or (first.lastindex == _BARE_GROUP and head):
            return None
    return _plain(digits)


def branch_ticket(branch: str | None, *, sub: bool = False) -> str | None:
    """The ticket a branch name carries (`task/<id>-...`; for a subagent also `kanban-<id>`), as a plain
    digit string, or None. The id 0 is none (board ids start at 1)."""
    match = (_SUB_BRANCH_RE if sub else _BRANCH_RE).match(branch) if branch else None
    if not match:
        return None
    digits = next(g for g in match.groups() if g)
    return None if int(digits) == 0 else _plain(digits)


def attribute_ticket(prompt: str | None, branch: str | None, commits: Iterable[str]) -> Attribution | None:
    """The ticket a session worked and the evidence that says so, or None.

    `prompt` is the first user prompt, `branch` the session's git branch and
    `commits` the Bash commands that ran `git commit`. Prompt (by `explicit_ticket`,
    anchored) beats branch beats commit text; ids are returned as plain digit strings,
    without leading zeros.
    """
    explicit = explicit_ticket(strip_context(prompt) if prompt else "", anchored=True)
    if explicit:
        return Attribution(explicit, "prompt")
    from_branch = branch_ticket(branch)
    if from_branch:
        return Attribution(from_branch, "branch")
    ids = Counter(_plain(m) for command in commits for m in _COMMIT_RE.findall(command))
    if ids:
        return Attribution(ids.most_common(1)[0][0], "commit")
    return None


def classify_phase(prompt: str | None, command: str | None = None) -> str:
    """`build`, `review` or `fix` for a session, from its first user prompt.

    The executor prompt of `drain_tickets.py` (since removed) is build. Otherwise, within the first
    PHASE_WINDOW characters, a fix-the-findings request is `fix` (checked first, since it
    usually mentions the review), an adversarial / review-round / "review the" / reviewer /
    "review of" prompt is `review`, and anything else (the default) is `build`. `command` is the
    slash command the session started with (`slash_command`): a name with the word `review` in it
    (`/code-review`, `/security-review`, `/pr-review-toolkit:review-pr`) is `review`, since the
    command name itself is stripped from the prompt text.
    """
    head = strip_context(prompt or "").lstrip()[:PHASE_WINDOW]
    if head.startswith(EXECUTOR_MARKER):
        return BUILD
    if _FIX_RE.search(head):
        return FIX
    if _REVIEW_RE.search(head) or (command and _REVIEW_COMMAND_RE.search(command)):
        return REVIEW
    return BUILD


def _plain(digits: str) -> str:
    return str(int(digits))


class _NameTags:
    """The tags of one name in a text: where each starts and ends, and whether it opens (+1) or closes (-1).

    Depth is the prefix sum of those steps, `walk[k]` after the first `k` tags. A block that starts before tag
    `i` ends at the first `k > i` with `walk[k] < walk[i]` (`below[i]`); if the depth never gets that low it
    ends at the first closer that reaches the lowest depth after `i` (`low[i]`). Both are computed once, in linear
    time, so the cost of a block no longer depends on how much text follows it.
    """

    __slots__ = ("_ready", "below", "ends", "low", "starts", "steps", "walk")

    def __init__(self) -> None:
        self.starts = array("q")
        self.ends = array("q")
        self.steps = array("b")
        self._ready = False
        self.walk = array("q")
        self.below = array("q")
        self.low = array("q")

    def _prepare(self) -> None:
        count = len(self.steps)
        walk = self.walk = array("q", bytes(8 * (count + 1)))
        for k, step in enumerate(self.steps):
            walk[k + 1] = walk[k] + step
        below = self.below = array("q", bytes(8 * (count + 1)))
        low = self.low = array("q", bytes(8 * (count + 2)))
        stack: list[int] = []  # indices with strictly increasing walk values, scanned right to left
        for i in range(count, -1, -1):
            while stack and walk[stack[-1]] >= walk[i]:
                stack.pop()
            below[i] = stack[-1] if stack else -1
            stack.append(i)
        low[count + 1] = -1
        for i in range(count, -1, -1):  # the first closer index at which the suffix `walk[i:]` is lowest
            later = low[i + 1]
            closes = i > 0 and self.steps[i - 1] == -1
            low[i] = i if closes and (later < 0 or walk[i] <= walk[later]) else later
        self._ready = True

    def block_end(self, start: int) -> int:
        """Index after the closer that ends a block whose content starts at `start`, or -1."""
        if not self._ready:
            self._prepare()
        i = bisect_left(self.starts, start)
        k = self.below[i]
        if k < 0:
            k = self.low[i + 1]  # never balanced: the first closer at the lowest depth
        return self.ends[k - 1] if k > 0 else -1


class _Tags:
    """Every context tag of a text, found in one pass; `block_end` answers for any block in O(log n)."""

    def __init__(self, text: str) -> None:
        self._names: dict[str, _NameTags] = {}
        for tag in _TAG_RE.finditer(text):
            closing, opening = tag.groups()
            name = (opening if opening is not None else closing).lower()
            entry = self._names.get(name)
            if entry is None:
                entry = self._names[name] = _NameTags()
            entry.starts.append(tag.start())
            entry.ends.append(tag.end())
            entry.steps.append(1 if opening is not None else -1)

    def block_end(self, start: int, name: str) -> int:
        """Index after the `</name>` that closes a block opened just before `start`, or -1.

        Same-name blocks nested inside are balanced (depth count) when they are complete tags. If the depth
        never returns to zero (a prose mention such as "ignore <system-reminder> blocks" plus a real nested
        block), the block ends at the first closer that reaches the lowest depth, so the rest of the reminder does not
        leak out as the user's words. No closer at all: -1.
        """
        entry = self._names.get(name)
        return entry.block_end(start) if entry else -1


def strip_context(text: str) -> str:
    """`text` without injected context blocks (`<system-reminder>`, command and IDE blocks).

    Tag names are case-insensitive and same-name blocks nest (balanced scan; an inner opener counts
    only as a complete tag, so a prose mention of the name inside a block does not). A slash
    command's `<command-args>` keeps its content: that is what the user typed. An unterminated
    block, or an opening tag with no `>`, is dropped to the end (a missed ticket is better than a
    wrong one), so a prompt that merely mentions a context tag in prose loses what follows it.
    Not handled: a closing tag of a different name inside a block ends nothing, and a stray
    closing tag outside any block stays in the text. Time is linear in the text.
    """
    out: list[str] = []
    pos = 0
    tags: _Tags | None = None
    while True:
        opener = _OPEN_RE.search(text, pos)
        if opener is None:
            out.append(text[pos:])
            break
        out.append(text[pos : opener.start()])
        gt = text.find(">", opener.end())
        if gt < 0:
            break
        name = opener.group(1).lower()
        if name == _KEEP_CONTENT_TAG:
            pos = gt + 1
            continue
        if tags is None:
            tags = _Tags(text)
        end = tags.block_end(gt + 1, name)
        if end < 0:
            break
        pos = end
    return _KEEP_CLOSE_RE.sub("", "".join(out))


def real_prompt_text(content: object) -> str | None:
    """What the user typed in a message `content` (a string or a list of blocks), or None.

    Claude Code puts CLAUDE.md, memory and git-status reminders into the same first user message;
    they are removed, so neither the ticket nor the phase is read from them. Used for both.
    """
    if isinstance(content, str):
        return strip_context(content)
    if isinstance(content, list):
        texts = (b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
        return "\n".join(t for t in (strip_context(text).strip() for text in texts) if t)
    return None


def _skip_space(text: str, pos: int) -> int:
    """Index of the first character at or after `pos` that is not whitespace (`str.lstrip` without the copy)."""
    match = _NOT_SPACE_RE.search(text, pos)
    return match.start() if match else len(text)


def slash_command(content: object) -> str | None:
    """The slash command name a message `content` starts with (after leading reminders), or None."""
    if isinstance(content, str):
        texts: list[str] = [content]
    elif isinstance(content, list):
        texts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    else:
        return None
    for text in texts:
        pos = _skip_space(text, 0)
        tags: _Tags | None = None
        while text[pos : pos + 16].lower() == "<system-reminder":
            gt = text.find(">", pos)
            if gt < 0:
                break
            if tags is None:
                tags = _Tags(text)
            end = tags.block_end(gt + 1, "system-reminder")
            if end < 0:
                break
            pos = _skip_space(text, end)
        match = _COMMAND_NAME_RE.match(text, pos)
        if match:
            return match.group(1)
    return None


# --- transcripts -------------------------------------------------------------------


@dataclasses.dataclass
class Evidence:
    prompt: str | None = None
    command: str | None = None
    branch: str | None = None
    commits: list[str] = dataclasses.field(default_factory=list)


def _prompt_text(message: object) -> str | None:
    return real_prompt_text(message.get("content") if isinstance(message, dict) else None)


def _open_regular(path: Path) -> int | None:
    """A read-only fd for a regular, non-symlink file, else None. Never blocks on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _read_records(path: Path) -> Iterator[dict[str, object]]:
    """The JSON objects of a JSONL transcript, like `ti.read_jsonl` but bounded: `_scan_records`
    without its gap markers."""
    return (record for record in _scan_records(path) if record is not None)


def _scan_records(path: Path) -> Iterator[dict[str, object] | None]:
    """The JSON objects of a JSONL transcript, bounded, and `None` for each line dropped on the way.

    A symlink or non-regular file reads as empty. A line over MAX_LINE_BYTES is dropped and never held whole
    (a 300 MB single line took 737 MB); reading stops after MAX_TRANSCRIPT_BYTES. A line that is not JSON (bad
    UTF-8, a torn line) is dropped too. Each drop yields `None`, so a reader of "the first X" knows a line it
    never saw may have been it. A blank line and a JSON line that is not an object are skipped silently."""
    fd = _open_regular(path)
    if fd is None:
        return
    total = 0
    try:
        with os.fdopen(fd, "rb") as fh:
            while total < MAX_TRANSCRIPT_BYTES:
                line = fh.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                total += len(line)
                if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                    while line and not line.endswith(b"\n"):  # drop the rest of the line, a chunk at a time
                        line = fh.readline(_READ_CHUNK)
                        total += len(line)
                    yield None
                    continue
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line.decode("utf-8", errors="replace"))
                except (ValueError, RecursionError):
                    yield None
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:
        return


def _read_meta(path: Path) -> dict[str, object]:
    """The `.meta.json` next to a subagent transcript: {} for a symlink, non-regular file, oversized,
    unreadable, invalid or non-object file."""
    fd = _open_regular(path)
    if fd is None:
        return {}
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(META_MAX_BYTES + 1)
        if len(raw) > META_MAX_BYTES:
            return {}
        meta = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return meta if isinstance(meta, dict) else {}


def read_evidence(path: Path) -> Evidence:
    """First user prompt, first git branch and `git commit` commands of one transcript."""
    evidence = Evidence()
    seen_prompt = False
    dropped = False  # a line was dropped before any user record: the next user record may not be the first
    for record in _scan_records(path):
        if record is None:
            dropped = True
            continue
        branch = record.get("gitBranch")
        if evidence.branch is None and isinstance(branch, str) and branch:
            evidence.branch = branch
        if record.get("type") == "user" and not seen_prompt:
            seen_prompt = True
            if dropped:
                continue
            message = record.get("message")
            evidence.prompt = _prompt_text(message)
            evidence.command = slash_command(message.get("content") if isinstance(message, dict) else None)
        elif record.get("type") == "assistant":
            message = record.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            for block in blocks if isinstance(blocks, list) else []:
                if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash"):
                    continue
                tool_input = block.get("input")
                command = tool_input.get("command") if isinstance(tool_input, dict) else None
                if isinstance(command, str) and "git commit" in command and command not in evidence.commits:
                    evidence.commits.append(command)
    return evidence


def _project_directories(project_dir: Path) -> list[Path]:
    """The project directory and its worktree siblings that exist; a symlinked sibling is not followed."""
    siblings = sorted(project_dir.parent.glob(f"{project_dir.name}--claude-worktrees-*"))
    return [d for d in [project_dir, *(s for s in siblings if not s.is_symlink())] if d.is_dir()]


def transcript_index(project_dir: Path | None) -> dict[str, Path]:
    """session id -> transcript path, over the project directory and its worktree siblings."""
    if project_dir is None:
        return {}
    index: dict[str, Path] = {}
    for directory in _project_directories(project_dir):
        for path in sorted(directory.glob("*.jsonl")):
            if not path.is_symlink():
                index.setdefault(path.stem, path)
    return index


@dataclasses.dataclass
class SubRun:
    """What the transcript of one Agent-tool subagent says about its run. Ids and signals only."""

    ticket: str | None = None
    source: str | None = None  # "description", "dispatch" or "sub-branch"
    agent_type: str | None = None  # `.meta.json` `agentType`
    phase: str | None = None  # REVIEW or FIX when the run says so, else None
    type_from_spawn: bool = False  # `agent_type` came from an `agent_spawn` record, not a transcript


def is_reviewer_type(agent_type: str | None) -> bool:
    return isinstance(agent_type, str) and _REVIEWER_RE.search(agent_type) is not None


def _sub_phase(agent_type: str | None, description: str | None, prompt: str | None, branches: list[str]) -> str | None:
    """`review` for a reviewer subagent, `fix` for a `fix/r<N>` branch or a fix-the-findings dispatch,
    `review` for a review dispatch; None when nothing says so (the run is then build, or its session's phase)."""
    if is_reviewer_type(agent_type):
        return REVIEW
    if any(_FIX_BRANCH_RE.match(b) for b in branches):
        return FIX
    # The dispatch prompt first, so an executor prompt (EXECUTOR_MARKER at its start) stays build even
    # though it talks about a reviewer; the description speaks only when the prompt gives no phase.
    if prompt and strip_context(prompt).lstrip().startswith(EXECUTOR_MARKER):
        return None
    for text in (prompt, description):
        phase = classify_phase(text)
        if phase != BUILD:
            return phase
    return None


def _meta_str(meta: dict[str, object], key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) else None


def _subagent_transcripts(directory: Path, sessions: set[str]) -> Iterator[tuple[str, Path]]:
    """(session id, path) of `<session>/subagents/agent-*.jsonl` under `directory` for `sessions`, sorted.
    A symlinked session directory, `subagents` directory or transcript is skipped, not followed."""
    for subagents in sorted(directory.glob("*/subagents")):
        session_dir = subagents.parent
        if session_dir.name not in sessions or session_dir.is_symlink() or subagents.is_symlink():
            continue
        for path in sorted(subagents.glob("agent-*.jsonl")):
            if not path.is_symlink():
                yield session_dir.name, path


def read_subagent_runs(
    project_dir: Path | None, sessions: set[str]
) -> tuple[dict[tuple[str, str], SubRun], dict[tuple[str, str], tuple[str, str]]]:
    """(session, agent id) -> SubRun, and request id -> (session, agent id), for the subagent
    transcripts `<session>/subagents/agent-<id>.jsonl` of `sessions` (project directory and its worktree
    siblings). The ticket is the dispatch description's, else the dispatch prompt's (`explicit_ticket`,
    the rule of `--iterations`), else a `task/<id>-` branch on its records. A request id found in two
    transcripts of one session belongs to the first one read: the project directory before its worktree
    siblings (sorted), transcripts in sorted order, so the choice is deterministic and a request is never
    counted twice."""
    runs: dict[tuple[str, str], SubRun] = {}
    by_request: dict[tuple[str, str], tuple[str, str]] = {}
    if project_dir is None or not sessions:
        return runs, by_request
    for directory in _project_directories(project_dir):
        for session_id, transcript_path in _subagent_transcripts(directory, sessions):
            key = (session_id, transcript_path.stem.removeprefix("agent-"))
            if key in runs:
                continue
            meta = _read_meta(transcript_path.with_suffix(".meta.json"))
            description = _meta_str(meta, "description")
            prompt: str | None = None
            branches: list[str] = []
            seen_prompt = False
            dropped = False  # as in `read_evidence`: no dispatch prompt after a dropped line
            for record in _scan_records(transcript_path):
                if record is None:
                    dropped = True
                    continue
                branch = record.get("gitBranch")
                if isinstance(branch, str) and branch and branch not in branches:
                    branches.append(branch)
                request_id = record.get("requestId")
                if record.get("type") == "assistant" and isinstance(request_id, str) and request_id:
                    by_request.setdefault((session_id, request_id), key)
                elif record.get("type") == "user" and not seen_prompt:
                    seen_prompt = True
                    prompt = None if dropped else _prompt_text(record.get("message"))
            # `.meta.json` is untrusted: a non-string `agentType` is no type at all.
            agent_type = clean_name(meta.get("agentType"))
            run = SubRun(agent_type=agent_type, phase=_sub_phase(agent_type, description, prompt, branches))
            from_description = explicit_ticket(description, anchored=False)
            from_prompt = explicit_ticket(prompt, anchored=True)
            from_branch = next((t for b in branches if (t := branch_ticket(b, sub=True))), None)
            if from_description:
                run.ticket, run.source = from_description, "description"
            elif from_prompt:
                run.ticket, run.source = from_prompt, "dispatch"
            elif from_branch:
                run.ticket, run.source = from_branch, "sub-branch"
            runs[key] = run
    return runs, by_request


def merge_spawns(runs: dict[tuple[str, str], SubRun], spawns: dict[tuple[str, str], Spawn], sessions: set[str]) -> None:
    """Fold the plugin's `agent_spawn` records into `runs`, for the subagents of `sessions`.
    A spawn carries the dispatch description's ticket id and the agent type, so a run needs no transcript.
    A transcript's own ticket and type win; the spawn fills what the transcript leaves out, and a run
    only the spawn knows is new. A spawn with no ticket adds no run of its own (whatever its type); it still fills a type the
    transcript's run lacks. A reviewer type (`is_reviewer_type`) sets the review phase of a run with none."""
    for key, spawn in sorted(spawns.items()):
        if key[0] not in sessions:
            continue
        run = runs.get(key)
        if run is None:
            if spawn.ticket is None:
                continue
            run = runs[key] = SubRun()
        if run.ticket is None and spawn.ticket is not None:
            run.ticket, run.source = spawn.ticket, "spawn"
        if run.agent_type is None and spawn.agent_type is not None:
            run.agent_type, run.type_from_spawn = spawn.agent_type, True
            if run.phase is None and is_reviewer_type(spawn.agent_type):
                run.phase = REVIEW


def read_board(board: Path) -> dict[str, str]:
    """ticket id -> `status:` from `<board>/tasks/<id>-*.md` front matter."""
    statuses: dict[str, str] = {}
    tasks = board / "tasks"
    if not tasks.is_dir():
        return statuses
    for path in sorted(tasks.glob("*.md")):
        name = _TASK_FILE_RE.match(path.name)
        if not name:
            continue
        status = _front_matter_status(path)
        if status:
            statuses.setdefault(_plain(name.group(1)), status)  # sorted: the lowest filename wins
    return statuses


def _read_head(path: Path) -> str | None:
    """The first STATUS_READ_BYTES bytes of a regular, non-symlink file, else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, STATUS_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _front_matter_status(path: Path) -> str | None:
    head = _read_head(path)
    if head is None:
        return None
    head = head.removeprefix("\ufeff")
    lines = head.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    for line in lines[1:]:
        if line.rstrip() == "---":
            break
        if line.startswith("status:"):
            value = line.removeprefix("status:").strip()
            if value[:1] in ("'", '"') and value.find(value[0], 1) > 0:
                value = value[1 : value.find(value[0], 1)]  # a quoted value; a trailing comment is dropped
            else:
                value = _COMMENT_RE.sub("", value)
            value = "".join(c for c in value if c.isprintable())
            value = " ".join(value.split())[:STATUS_MAX_CHARS]
            return value or None
    return None


# --- report ------------------------------------------------------------------------


@dataclasses.dataclass
class TicketRow:
    ticket: str
    status: str = UNKNOWN_STATUS
    sessions: int = 0
    runs: int = 0
    requests: int = 0
    iterations: int = 0
    uncosted: int = 0
    cost: float = 0.0
    wall_seconds: float = 0.0
    active_seconds: float = 0.0
    sources: set[str] = dataclasses.field(default_factory=set)
    models: Counter[str] = dataclasses.field(default_factory=Counter)
    efforts: Counter[str] = dataclasses.field(default_factory=Counter)
    phase_cost: defaultdict[str, float] = dataclasses.field(default_factory=lambda: defaultdict(float))
    phase_requests: Counter[str] = dataclasses.field(default_factory=Counter)
    phase_uncosted: Counter[str] = dataclasses.field(default_factory=Counter)

    def add(self, session: Session, source: str | None, phase: str = BUILD, *, run: bool = False) -> None:
        self.phase_cost[phase] += session.cost
        self.phase_requests[phase] += session.requests
        self.phase_uncosted[phase] += session.uncosted
        if run:
            self.runs += 1
        else:
            self.sessions += 1
        self.requests += session.requests
        self.iterations += session.iterations
        self.uncosted += session.uncosted
        self.cost += session.cost
        self.wall_seconds += _span(session)
        self.active_seconds += session.active_seconds(ACTIVE_GAP_SECONDS)
        self.models.update(session.models)
        self.efforts.update(session.efforts)
        if source:
            self.sources.add(source)


@dataclasses.dataclass
class TicketReport:
    rows: list[TicketRow]
    no_transcripts: bool = False

    @property
    def uncosted(self) -> int:
        return sum(r.uncosted for r in self.rows)


def _parse(timestamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _span(session: Session) -> float:
    first, last = _parse(session.first), _parse(session.last)
    if first is None or last is None:
        return 0.0
    try:
        return max((last - first).total_seconds(), 0.0)
    except TypeError:  # one naive, one aware
        return 0.0


@dataclasses.dataclass(frozen=True)
class Placement:
    """Where a subagent run's requests are counted: its ticket, what attributed it and its phase."""

    ticket: str
    source: str
    phase: str


def build_report(capture: Capture, project_dir: Path | None, board: dict[str, str] | None) -> TicketReport:
    sdk_sessions = {r.session for r in capture.requests if r.source == SDK_SOURCE}
    paths = transcript_index(project_dir)
    parents: dict[str, tuple[Attribution | None, str]] = {}

    def parent(session: str) -> tuple[Attribution | None, str]:
        """The attribution and phase of a whole session (sdk sessions with a transcript only)."""
        if session not in parents:
            path = paths.get(session) if session in sdk_sessions else None
            if path is None:
                parents[session] = (None, BUILD)
            else:
                evidence = read_evidence(path)
                parents[session] = (
                    attribute_ticket(evidence.prompt, evidence.branch, evidence.commits),
                    classify_phase(evidence.prompt, evidence.command),
                )
        return parents[session]

    def locate(session: str) -> tuple[str, str | None, str]:
        """The row key, attribution source and phase of a whole session.

        An sdk session is attributed by `parent`. An interactive one (no sdk request) only by its branch or
        ticket commits, never its prompt: a person's chat names tickets in passing. Its source is marked
        `interactive-`. What stays unmatched is split by why: no transcript read for an sdk session, an
        interactive session, or an sdk transcript that names nothing.
        """
        found, phase = parent(session)
        if found:
            return found.ticket, found.source, phase
        path = paths.get(session)
        if session in sdk_sessions:
            return (UNATTRIBUTED if path else UNATTRIBUTED_NO_TRANSCRIPT), None, phase
        if path is None:
            return UNATTRIBUTED_INTERACTIVE, None, phase
        evidence = read_evidence(path)
        phase = classify_phase(evidence.prompt, evidence.command)
        branch_or_commit = attribute_ticket(None, evidence.branch, evidence.commits)
        if branch_or_commit:
            return branch_or_commit.ticket, INTERACTIVE_PREFIX + branch_or_commit.source, phase
        return UNATTRIBUTED_INTERACTIVE, None, phase

    # Subagent requests share the launching session's id. A request whose run can be read from a
    # subagent transcript (native step: by agent id; api_request: by `request_id`) and has a ticket of
    # its own, or sits in an attributed session, becomes a pseudo-session per run; the rest stays put.
    event_prompts = {(r.session, r.prompt) for r in capture.requests if r.loop}
    steps = [st for st in capture.steps if (st.session, st.prompt) not in event_prompts]
    runs, by_request = read_subagent_runs(project_dir, {r.session for r in [*capture.requests, *steps] if r.sub})
    merge_spawns(runs, capture.spawns, {r.session for r in [*capture.requests, *steps] if r.sub})
    placed: dict[str, Placement] = {}
    real_sessions = {r.session for r in [*capture.requests, *steps]}
    pseudo_ids: dict[tuple[str, str], str] = {}
    split_from: set[str] = set()
    run_counter = itertools.count()

    def route(request: Request) -> Request:
        if not request.sub:
            return request
        key = (
            (request.session, request.agent)
            if request.agent
            else by_request.get((request.session, request.request_id))
            if request.request_id
            else None
        )
        if key is None or key[0] != request.session or key not in runs:
            return request
        run = runs[key]
        found = parent(request.session)[0]  # sdk sessions only: a ticketless run never joins an interactive one
        session_phase = locate(request.session)[2]  # but an interactive session's phase still applies
        ticket, source = (
            (run.ticket, run.source) if run.ticket else (found.ticket, found.source) if found else (None, None)
        )
        if ticket is None or source is None:
            return request
        if key not in pseudo_ids:  # an id no real session has: never built from a session id's text
            taken = real_sessions | set(pseudo_ids.values())
            while (candidate := f"\0run{next(run_counter)}") in taken:
                pass
            pseudo_ids[key] = candidate
        pseudo = pseudo_ids[key]
        # The step's own reviewer subagent beats a spawn type that left the run without a phase (a
        # general-purpose spawn); a transcript's own type still wins over the step.
        own = REVIEW if run.phase is None and run.type_from_spawn and is_reviewer_type(request.subagent) else None
        placed[pseudo] = Placement(ticket, source, run.phase or own or session_phase)
        split_from.add(request.session)
        return dataclasses.replace(request, session=pseudo)

    routed = dataclasses.replace(
        capture, requests=[route(r) for r in capture.requests], steps=[route(st) for st in steps]
    )
    rows: dict[str, TicketRow] = {}
    remaining = build_session_report(routed).sessions
    for session in remaining:
        place = placed.get(session.session)
        if place:
            rows.setdefault(place.ticket, TicketRow(place.ticket)).add(session, place.source, place.phase, run=True)
            continue
        ticket, source, phase = locate(session.session)
        rows.setdefault(ticket, TicketRow(ticket)).add(session, source, phase)
    # A session whose every request was split out still is a session of its ticket (totals match --sessions).
    for gone in sorted(split_from - {s.session for s in remaining}):
        ticket = locate(gone)[0]
        rows.setdefault(ticket, TicketRow(ticket)).sessions += 1
    for ticket, row in rows.items():
        if not is_unattributed(ticket) and board is not None:
            row.status = board.get(ticket, UNKNOWN_STATUS)
    ordered = sorted(
        rows.values(),
        key=lambda r: ticket_sort_key(r.ticket),
    )
    return TicketReport(ordered, no_transcripts=project_dir is None)


# --- formatting --------------------------------------------------------------------

HEADER = [
    "ticket",
    "status",
    "sessions",
    "runs",
    "reqs",
    "iters",
    "cost",
    "build",
    "review",
    "fix",
    "wall",
    "active",
    "source",
    "models",
    "effort",
]
LEGEND = [
    "  build / review / fix = cost/requests of the ticket's sessions by phase, from the first prompt (a review or",
    "  fix-the-findings prompt; else build; not split for (unattributed), so the phase totals leave it out);",
    "  a subagent run (Agent tool) is split out of its launching session when its dispatch description or",
    "  prompt, or a task/<id>- branch on its transcript, names a ticket; reviewer subagents and fix/r<N> branches",
    "  put it in review / fix, else the session's phase;",
    "  sessions = sdk executor sessions attributed to the ticket (a session whose requests all went to runs stays counted, with 0 requests);",
    "  runs = subagent runs attributed to it (parallel runs overlap in wall); $x+ = some requests have no cost, so the",
    "  cost is a lower bound; wall = summed first-to-last request time per session, idle gaps included (a",
    "  resumed session overstates the time worked); active = the part of wall made of gaps between consecutive",
    "  requests of at most 5 minutes (idle pauses left out; parallel runs overlap); source = what attributed them (prompt, branch, commit;",
    "  description, dispatch, sub-branch, spawn for runs; interactive-branch / interactive-commit = a non-sdk",
    "  session, attributed by its task/<id>- branch or ticket commits only, never its prompt);",
    "  status needs --board <kanban dir>",
    "  ('?' otherwise); models and effort = of loop requests, most used first; (unattributed) holds every",
    "  session or run that nothing matched, so the total equals report --sessions, split into interactive,",
    "  sdk, no transcript (none was found: check --project-dir) and plain (unattributed), an sdk transcript naming nothing",
]


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _short(names: Counter[str]) -> str:
    ordered = [name.removeprefix(MODEL_PREFIX) for name, _ in names.most_common()]
    return ",".join(ordered) or NONE


def _money(requests: int, uncosted: int, cost: float) -> str:
    if requests <= uncosted:
        return NONE
    return f"${cost:.2f}" + ("+" if uncosted else "")


def _cost(row: TicketRow) -> str:
    return _money(row.requests, row.uncosted, row.cost)


def _phase(row: TicketRow, phase: str) -> str:
    if is_unattributed(row.ticket) or not row.phase_requests[phase]:
        return NONE
    requests = row.phase_requests[phase]
    return f"{_money(requests, row.phase_uncosted[phase], row.phase_cost.get(phase, 0.0))}/{requests:,}"


def _row(row: TicketRow) -> list[str]:
    return [
        row.ticket if is_unattributed(row.ticket) else f"#{row.ticket}",
        row.status,
        f"{row.sessions:,}",
        f"{row.runs:,}",
        f"{row.requests:,}",
        f"{row.iterations:,}",
        _cost(row),
        *(_phase(row, phase) for phase in PHASES),
        _duration(row.wall_seconds),
        _duration(row.active_seconds),
        ",".join(sorted(row.sources)) or NONE,
        _short(row.models),
        _short(row.efforts),
    ]


def _total_row(rows: list[TicketRow]) -> list[str]:
    total = TicketRow("total")
    for r in rows:
        total.sessions += r.sessions
        total.runs += r.runs
        total.requests += r.requests
        total.iterations += r.iterations
        total.uncosted += r.uncosted
        total.cost += r.cost
        total.wall_seconds += r.wall_seconds
        total.active_seconds += r.active_seconds
        if not is_unattributed(r.ticket):
            for phase, cost in r.phase_cost.items():
                total.phase_cost[phase] += cost
            total.phase_requests.update(r.phase_requests)
            total.phase_uncosted.update(r.phase_uncosted)
    out = _row(total)
    out[0], out[1], out[12], out[13], out[14] = "total", NONE, NONE, "", ""
    out[2] = f"{total.sessions:,}"
    return out


def window_label(since: datetime | None, until: datetime | None, since_raw: str | None, until_raw: str | None) -> str:
    """The window as the operator wrote it, for the table header; empty when there is none."""
    parts = []
    if since is not None:
        parts.append(f"since {since_raw}")
    if until is not None:
        parts.append(f"until {until_raw}")
    return " ".join(parts)


def _legend(window: str) -> list[str]:
    if not window:
        return LEGEND
    # A windowed total is not report --sessions: it covers the requests inside the window only.
    return [
        line.replace("so the total equals report --sessions,", "so the total covers the window's requests,")
        for line in LEGEND
    ]


def format_report(report: TicketReport, limit: int | None = None, window: str = "", undated: int = 0) -> str:
    rows = report.rows
    if not rows:
        return "No model requests found."
    cap = limit or DEFAULT_TICKET_LIMIT
    attributed = [r for r in rows if not is_unattributed(r.ticket)]
    shown = attributed[-cap:]
    shown += [r for r in rows if is_unattributed(r.ticket)]
    table = format_table(HEADER, [*(_row(r) for r in shown), _total_row(rows)], left=1, text=(1, 11, 12, 13))
    table.insert(len(table) - 1, "  " + "-" * (len(table[0]) - 2))
    hidden = len(attributed) - cap
    older = (
        f"; {plural(hidden, 'ticket')} not shown (showing the newest, highest ids), raise --limit" if hidden > 0 else ""
    )
    covers = f"total covers every session in the window ({window})" if window else "total covers every session"
    blocks = [[f"Per-ticket summary (one row per ticket, by ticket id{older}; {covers})", *table, *_legend(window)]]
    notes = []
    if report.uncosted:
        notes.append(f"  {plural(report.uncosted, 'request')} without cost (left out of cost)")
    if undated:
        notes.append(
            f"  {plural(undated, 'request')} {'has' if undated == 1 else 'have'} no usable timestamp, so the "
            "--since/--until window kept them (they cannot be placed in time)"
        )
    if report.no_transcripts:
        notes.append("  no transcript directory for this project: every session is unattributed")
    if notes:
        blocks.append(["Notes", *notes])
    return "\n\n".join("\n".join(block) for block in blocks)
