"""Totals-only reader of every project's transcripts: the privacy exception, in one place.

Calibrating `/usage` against transcripts needs ONE number: the eq (the usage report's weighting) of ALL
projects in a window. This is the only module that opens a transcript outside the current project's
group, and it lets nothing but numbers out:

- `all_projects_eq(root, since, until, overrides)` and `project_eq(dirs, since, until)` return a `Total`
  (an eq float and an integer count of skipped files). Rows, events, tickets, sessions, model ids,
  prompt text, file names and error text stay locals of the private `_sum_eq` and die there.
- It takes no logger, prints nothing, warns nothing and puts no text of a transcript into an exception.
  `TotalsError` has a fixed message.
- A file that cannot be used fully (a torn or non-object line, unreadable, unexpected failure, a
  symlink, or a message of a model with no price) counts once in `skipped`; its good messages still
  count. A symlinked project directory counts once too and is not followed. What a caller may say
  about it is `N file(s) skipped in transcripts`, and nothing more.

A test pins this: the total equals a hand sum, a sentinel scan over stdout,
stderr, log records and every file under a temp HOME finds no foreign string, and an AST test lets only
`quota_totals` and `quota_report` import the transcript reader.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_usage_report as aur
import project_groups as pg
import transcript_iterator as ti
from model_prices import usage_tokens

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime


class TotalsError(Exception):
    """The projects root cannot be listed. The message is fixed and names nothing."""


@dataclasses.dataclass(frozen=True)
class Total:
    """The only thing this module returns: an eq total and how many files were skipped."""

    eq: float
    skipped: int


def _is_clean(path: Path) -> bool:
    """False when the file cannot be read or holds a line that is not a JSON object."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except (ValueError, RecursionError):
                    return False
                if not isinstance(parsed, dict):
                    return False
    except OSError:
        return False
    return True


def _inside(path: Path, directory: Path) -> bool:
    """A regular file under `directory`, not reached through a link (see `project_groups.regular_file_inside`)."""
    return pg.regular_file_inside(path, directory)


def _listable(path: Path) -> bool:
    """The directory lists AND can be entered. A mode `r--` directory lists its names but `stat` and `open` of
    anything inside it fail, so a plain `scandir` would call it fine while its files are lost."""
    try:
        with os.scandir(path):
            return os.access(path, os.X_OK)
    except OSError:
        return False


def _unreadable_dirs(directory: Path) -> int:
    """How many session directories of the listable project `directory`, and their `subagents` directories,
    cannot be listed (a locked directory lists as empty through `glob`, which would hide its files).
    A directory that fails between the check and the read counts as lost and leaves no path behind."""
    count = 0
    try:
        for child in sorted(directory.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            locked_session = not _listable(child)
            locked_subagents = (
                not locked_session and (child / "subagents").is_dir() and not _listable(child / "subagents")
            )
            count += int(locked_session or locked_subagents)
    except OSError:  # the directory went away or locked after `_listable`: its files are lost, say so once
        return count + 1
    return count


def _in_window(event: ti.AssistantMessageEvent, since: datetime | None, until: datetime | None) -> bool:
    if since is None and until is None:
        return True
    stamp = aur.parse_timestamp(event.timestamp)
    if stamp is None:
        return False
    if since is not None and stamp < since:
        return False
    return not (until is not None and stamp > until)


def _has_tokens(usage: Mapping[str, object]) -> bool:
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return sum(usage_tokens(usage, key) for key in keys) > 0


def _sum_eq(dirs: Sequence[Path], since: datetime | None, until: datetime | None) -> Total:
    """The eq of the transcripts under `dirs`, read as one project (a response repeated in several
    files counts once). Everything but the two numbers stays inside this function."""
    skipped = 0
    candidates: list[tuple[ti.TranscriptContext, tuple[ti.AssistantMessageEvent, ...]]] = []
    flagged: set[int] = set()
    for directory in dirs:
        if not directory.exists():
            continue  # a missing directory is no data, not a lost one
        if not _listable(directory):
            skipped += 1  # the project directory itself is locked; whatever `glob` still names is not read
            continue
        skipped += _unreadable_dirs(directory)
        try:
            contexts = ti.discover_transcripts(directory, skip_links=False)
        except Exception:  # nothing from the failure may leave: not its text, not its path
            skipped += 1
            continue
        for context in contexts:
            if not _listable(context.transcript_path.parent):
                continue  # a directory that lists but cannot be entered: `_unreadable_dirs` counted it once already
            if not _inside(context.transcript_path, directory):
                skipped += 1  # never read through a link, and never open a FIFO
                continue
            torn = not _is_clean(context.transcript_path)
            try:
                events = tuple(e for e in ti.iter_events(context) if isinstance(e, ti.AssistantMessageEvent))
            except Exception:  # nothing from the failure may leave: not its text, not its path
                skipped += 1
                continue
            if torn:
                skipped += 1
                flagged.add(len(candidates))
            candidates.append((context, events))

    values: list[float] = []
    for index, (context, owned) in enumerate(ti.dedupe_across_files(candidates)):
        meta_model = context.meta_model if isinstance(context.meta_model, str) else None  # a hostile `.meta.json`
        file_values: list[float] = []
        unusable = False
        try:
            for event in owned:
                if not _in_window(event, since, until):
                    continue
                model = event.model or meta_model
                if not aur.is_priced(model):
                    unusable = unusable or (event.usage is not None and _has_tokens(event.usage))
                    continue
                weight = aur.eq_weight(event.usage, model)
                if math.isfinite(weight) and weight >= 0:
                    file_values.append(weight)
                else:
                    unusable = True
        except Exception:  # nothing from the failure may leave: the file weighs nothing and counts once
            unusable = True
            file_values = []
        values.extend(file_values)
        if unusable and index not in flagged:
            skipped += 1
    return Total(eq=math.fsum(values), skipped=skipped)


def project_eq(dirs: Sequence[Path], since: datetime | None, until: datetime | None) -> Total:
    """The eq total of one project, given every directory of its group, in the window (inclusive).
    A message with no parseable timestamp is outside any bounded window."""
    return _sum_eq(dirs, since, until)


def all_projects_eq(
    root: Path, since: datetime | None, until: datetime | None, overrides: Mapping[str, str] | None = None
) -> Total:
    """The eq total of every project under `root` (`~/.claude/projects`) in the window: each project
    (its directories grouped by `project_groups`) is summed on its own, then the sums are added."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        raise TotalsError("the projects root cannot be listed") from None
    skipped = 0
    plain: list[Path] = []
    for entry in entries:
        if entry.is_symlink():
            skipped += 1  # a link is never followed: it could point at another tree or repeat a directory
        elif entry.is_dir():
            plain.append(entry)
    roots = pg.known_roots(p.name for p in plain)
    groups: dict[str, list[Path]] = {}
    for entry in plain:
        groups.setdefault(pg.group_of(entry.name, roots, overrides), []).append(entry)
    eqs: list[float] = []
    for dirs in groups.values():
        total = _sum_eq(dirs, since, until)
        eqs.append(total.eq)
        skipped += total.skipped
    return Total(eq=math.fsum(eqs), skipped=skipped)
