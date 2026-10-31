#!/usr/bin/env python3
"""Read-only check: how many lines of a project's captures still hold content or identity attributes.

    python3 -B scripts/check_capture_content.py [--capture PATH ...] [--max-seconds N] [--json]

It scans the project home's `logs/` (rotated files, per-label split directories, `.splitting` and
`.scrub-late-*` leftovers included) or the files given with `--capture`, and prints per file how many
lines are counted under each figure. Counts only: no attribute value, line content or line number is
ever printed, and nothing is written, renamed or created.

    removable    OTLP content entries (`prompt`, `response`, `tool_*`) the scrub removes
    serialized   JSON text in a string value that `mod_capture_append.drop_sensitive` would change
    unparseable  a line that is not a JSON object and still names a content or identity key, or one the scrub
                 keeps and counts as unscrubbed (a complete object too deep to parse, nested past 100 levels,
                 a repeated key)
    identity     identity attributes (`user.email`, `organization.id`, `vcs.*`)
    names        a skill or agent-type name (`skill`, `subagent_type`, `agent_type`, `skill.name`, ...) that
                 holds control characters, bidi overrides or line breaks, or is longer than 64 characters. Reports clean such a name when they read it, so it is not a finding:
                 it never changes the exit code, and the scrub does not rewrite it.
    redacted     content entries holding only Claude Code's `<REDACTED>` placeholder (not a finding)

Exit codes: 0 clean (placeholder lines do not count), 1 content or identity found, 2 usage error or
nothing to scan, 3 the scan was incomplete (time budget, unreadable file) and found nothing.
The definitions are the scrub's and the appender's, not copies: see `scrub_capture` and
`mod_capture_append`. Rewriting a capture stays with `scrub_capture.py`.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import agent_telemetry as at
import mod_capture_append as mca
import scrub_capture as sc
from capture_paths import project_home
from safe_name import name_is_clean

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

PLACEHOLDER = "<REDACTED>"  # what Claude Code writes in place of content when content logging is off
CAPTURE_NAMES = ("agent-telemetry.jsonl", "agent-telemetry.mod.jsonl")
LINE_CAP = 4 * 1024 * 1024  # a longer line is searched for names, never held or parsed
# Memory: a line up to the cap is held as bytes, decoded to text and parsed; parsed JSON of many small
# values takes about 30 times the line, so a line at the cap peaks near 120 MB (475 MB at a 16 MB cap).
# Time: the budget is checked every `WALK_CHECK` nodes while a line is walked, but one string is judged by one
# call of the appender's helper that cannot be interrupted: a single string of the cap's size can run a few
# seconds past `--max-seconds`.
WALK_CHECK = 512
CHUNK_SIZE = 1024 * 1024
DEFAULT_MAX_SECONDS = 120.0
FIGURES = ("removable", "serialized", "unparseable", "identity", "redacted", "names")
FINDINGS = FIGURES[:4]  # the figures that make the exit code 1
# Keys whose string value is a skill or agent-type name, in the plugin's records and in the OTLP mirror.
NAME_KEYS = frozenset({"skill", "subagent_type", "agent_type", "skill.name", "skill_name", "agent.name"})

# A line that cannot hold a name in any spelling a reader decodes is not parsed: a name in any letter case,
# or an escape (`\uXXXX`, `%XX`, an HTML entity) or a non-ASCII byte, which the text search would decode.
_NAMES = sorted(mca.CONTENT_ATTRIBUTES | at.IDENTITY_ATTRIBUTES)
_PREFILTER = re.compile(
    "|".join(
        [
            "(?:" + "|".join(re.escape(name) for name in _NAMES) + ")",
            r"\\u[0-9a-fA-F]{4}",
            r"%[0-9a-fA-F]{2}",
            r"&[#a-zA-Z]",
            r"[^\x00-\x7f]",
        ]
    ).encode("latin-1", "backslashreplace"),
    re.IGNORECASE,
)
# A line with a name key is parsed too: a name past the cap, or with a `\n` escape, has no other mark in an
# ASCII line. Kept apart from _PREFILTER: a line over the cap is never parsed and counts `unparseable` on
# `_PREFILTER` alone, and a name key must not make it one.
_NAME_KEY = re.compile("|".join(f'"{re.escape(key)}"' for key in sorted(NAME_KEYS)).encode("ascii"))
_CARRY = max(map(len, _NAMES)) + 8  # a match split across two chunks of an oversized line


@dataclass
class FileResult:
    path: Path
    shown: str
    size: int = 0
    lines: int = 0
    bytes_scanned: int = 0
    removable: int = 0
    serialized: int = 0
    unparseable: int = 0
    identity: int = 0
    redacted: int = 0
    names: int = 0
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.error is None and self.bytes_scanned >= self.size

    @property
    def found(self) -> bool:
        return any(getattr(self, name) for name in FINDINGS)


@dataclass(frozen=True)
class Verdict:
    removable: bool = False
    serialized: bool = False
    unparseable: bool = False
    identity: bool = False
    redacted: bool = False
    names: bool = False


def _changes(text: str) -> bool:
    """Whether the appender's shared helper would change this string or reject it."""
    try:
        return bool(mca.drop_sensitive(text) != text)
    except (mca.UncheckableError, RecursionError):
        return True


def _classify_record(record: dict[str, Any], tick: Callable[[], None] | None = None) -> Verdict:
    """The figures for a line that parsed as an object. An iterative walk, so no depth the parser accepted
    can exhaust the recursion limit. A content entry is judged whole and its value is not walked."""
    content = placeholder = nodes = 0
    serialized = names = too_deep = False

    def entry_counted(item: object) -> bool:
        """Count `item` when it is a content entry (kept apart from the walk: it is judged whole)."""
        nonlocal content, placeholder
        if not isinstance(item, dict) or not sc.is_content_entry(item):
            return False
        content += 1
        if item == {"key": item.get("key"), "value": {"stringValue": PLACEHOLDER}}:  # no other field
            placeholder += 1
        return True

    # (node, depth as the scrub counts it, None under an identity entry: the scrub removes those unread)
    stack: list[tuple[Any, int | None]] = [] if entry_counted(record) else [(record, 0)]  # an entry: dropped whole
    identity = mca.is_identity_entry(record)
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if depth is not None and depth > mca.MAX_RECORD_DEPTH:
            too_deep = True  # `drop_content` refuses it: the scrub keeps the line and counts it unscrubbed
            depth = None
        if tick is not None and nodes % WALK_CHECK == 0:
            tick()
        if isinstance(node, dict):
            label = node.get("key")  # an OTLP attribute: {"key": <name key>, "value": {"stringValue": <name>}}
            value = node.get("value")
            if isinstance(label, str) and label in NAME_KEYS and isinstance(value, dict):
                names = names or not name_is_clean(value.get("stringValue"))
            for key, item in node.items():
                if isinstance(key, str):
                    if key in NAME_KEYS and not name_is_clean(item):
                        names = True
                    if mca.is_identity(key):
                        identity = True
                    elif not serialized and mca._names_a_key(key):  # noqa: SLF001  JSON text used as a key
                        serialized = True
                if entry_counted(item):  # an entry as a dict value: the scrub drops it too
                    continue
                if mca.is_identity_entry(item):
                    identity = True
                stack.append((item, None if depth is None or mca.is_identity_entry(item) else depth + 1))
        elif isinstance(node, list):
            for item in node:
                if entry_counted(item):
                    continue
                identity = identity or mca.is_identity_entry(item)
                stack.append((item, None if depth is None or mca.is_identity_entry(item) else depth + 1))
        elif isinstance(node, str) and not serialized:
            serialized = _changes(node)
    return Verdict(
        removable=content > placeholder,
        serialized=serialized,
        identity=identity,
        redacted=content > 0 and content == placeholder,
        names=names,
        unparseable=too_deep,
    )


def classify(text: str, tick: Callable[[], None] | None = None) -> Verdict:
    """The figures for one line. A line that does not parse as an object is `unparseable` when the scrub's
    text search finds a sensitive name in it; a line holding CR or BOM is judged per piece, as the scrub
    scrubs it. A line the check cannot judge counts as unparseable, never as clean."""
    try:
        parsed, kept, too_deep = True, False, False  # kept: the scrub leaves the line as it is and counts it unscrubbed
        try:
            record = json.loads(text, object_pairs_hook=sc.parse_object)
        except (ValueError, RecursionError) as error:
            record, parsed = None, False
            kept = isinstance(error, sc.DuplicateKeyError)  # a repeated key: the scrub cannot see the shadowed copy
            too_deep = isinstance(error, RecursionError)
        if isinstance(record, dict):
            return _classify_record(record, tick)
        if not parsed and ("\r" in text or "\ufeff" in text):  # the scrub splits only a line that does not parse whole
            pieces = [piece.lstrip("\ufeff") for piece in text.split("\r")]
            if pieces != [text]:
                verdicts = [classify(piece, tick) for piece in pieces if piece.strip()]
                return Verdict(**{name: any(getattr(v, name) for v in verdicts) for name in FIGURES})
        if (kept or too_deep) and sc._is_one_object(text):  # noqa: SLF001
            return Verdict(unparseable=True)
        return Verdict(
            unparseable=sc.torn_line_is_sensitive(text)
        )  # the scrub marker names nothing, so it is never counted
    except (ValueError, RecursionError):
        return Verdict(unparseable=True)


def _count(result: FileResult, verdict: Verdict) -> None:
    for name in FIGURES:
        if getattr(verdict, name):
            setattr(result, name, getattr(result, name) + 1)


class _Expired(Exception):  # noqa: N818  not an error: the time budget ran out
    pass


def scan_file(
    result: FileResult,
    handle: IO[bytes],
    expired: Callable[[], bool],
) -> None:
    """Read `handle` up to `result.size` (its size at open), line by line, holding at most `LINE_CAP`
    bytes of a line. Raises _Expired between chunks when the budget is gone."""
    pending = bytearray()  # extended in place: `bytes +=` would copy the whole line each time
    oversized: list[Any] | None = None  # [hit, carry, brackets, size, escaped] while skipping a line over the cap
    offset = 0
    while offset < result.size:
        if expired():
            raise _Expired
        data = handle.read(min(CHUNK_SIZE, result.size - offset))
        if not data:
            result.error = "file shrank during the scan"
            return
        offset += len(data)
        pos = 0
        while pos < len(data):
            end = data.find(b"\n", pos)
            piece = data[pos:] if end < 0 else data[pos:end]
            pos = len(data) if end < 0 else end + 1
            if oversized is not None:
                window = oversized[1] + piece
                oversized[0] = oversized[0] or _PREFILTER.search(window) is not None
                oversized[1] = window[-_CARRY:]
                oversized[2] += _brackets(piece)
                oversized[3] += len(piece)
                oversized[4] = oversized[4] or b"\\" in piece
                if end >= 0:
                    _count(result, _oversized_verdict(oversized))
                    result.lines += 1
                    oversized = None
                continue
            pending += piece
            if len(pending) > LINE_CAP:
                oversized = [
                    _PREFILTER.search(pending) is not None,
                    bytes(pending[-_CARRY:]),
                    _brackets(pending),
                    len(pending),
                    b"\\" in pending,
                ]
                pending.clear()
                if end >= 0:
                    _count(result, _oversized_verdict(oversized))
                    result.lines += 1
                    oversized = None
                continue
            if end >= 0:
                _scan_line(result, bytes(pending), expired)
                pending.clear()
        result.bytes_scanned = offset
    if oversized is not None:
        _count(result, _oversized_verdict(oversized))
        result.lines += 1
    elif pending:
        _scan_line(result, bytes(pending), expired)  # a last line with no newline: a torn tail counts


def _brackets(data: bytes | bytearray) -> int:
    """`{` and `[` bytes in `data`: the ways to open a level that `mca.may_exceed_depth` counts. A bracket spelled
    as an escape (`\\u007b`) is not counted: the escape alone makes `_PREFILTER` parse the line."""
    return data.count(b"{") + data.count(b"[")


def _oversized_verdict(state: list[Any]) -> Verdict:
    """A line over the cap is not parsed. It is unparseable when it names something, and also when it could nest
    past the shallowest depth the scrub refuses (`mca.may_exceed_depth`): the scrub parses it whatever its length."""
    return Verdict(unparseable=state[0] or mca.may_exceed_depth(state[2], state[3], state[4]))


def _scan_line(result: FileResult, line: bytes, expired: Callable[[], bool]) -> None:
    result.lines += 1

    def tick() -> None:
        if expired():
            raise _Expired

    if _PREFILTER.search(line) is not None or _NAME_KEY.search(line) is not None:
        _count(result, classify(line.decode("utf-8", "surrogateescape"), tick))
    elif mca.may_exceed_depth(_brackets(line), len(line), b"\\" in line):
        # A line naming nothing is not parsed, but the scrub still keeps and counts one it cannot read: JSON text
        # in a string nested past `MAX_DEPTH` (brackets plus layers of string-in-string), a record nested past
        # `MAX_RECORD_DEPTH`, or one too deep for the parser. Any other line is not parsed.
        _count(result, classify(line.decode("utf-8", "surrogateescape"), tick))


def _error_name(error: OSError) -> str:
    return f"{errno.errorcode.get(error.errno or 0, 'OSError')} ({os.strerror(error.errno or 0).lower()})"


# ---- scan set -------------------------------------------------------------------------------------


def _is_file(path: Path) -> bool:
    """Whether `path` is a regular file (or a link to one). A path that is not there is not a file; any other
    error (a directory that lists but cannot be searched) is raised: Python 3.14's `Path.is_file` hides it."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:  # also a dangling link
        return False
    except OSError as error:
        if error.errno == errno.ELOOP:
            return False
        raise


def _family(base: Path) -> list[Path]:
    """`base`, its rotated files (oldest first, as `agent_telemetry.rotated_captures`) and its leftovers,
    regular files only. A leftover belongs to the live capture or to a rotated file `<name>.N`:
    `<name>[.N].splitting` (`split_capture` sets it aside), `<name>[.N].scrub-late-*` (what a scrub could not
    write back) and the hidden `.<name>[.N].<pid>.tmp` of a split. `<name>.scrub-<random>`, the temp of an
    interrupted scrub, holds scrubbed output only and is not scanned. May raise OSError."""
    try:
        listing = sorted(base.parent.iterdir())
    except (FileNotFoundError, NotADirectoryError):  # no such directory (or it vanished): nothing to list
        return []
    name = re.escape(base.name)
    rotated = re.compile(rf"{name}\.({at.ASCII_NUMBER.pattern})")
    leftover = re.compile(
        rf"{name}(\.\d+)?\.splitting|{name}(\.\d+)?\.scrub-late-.*|\.{name}(\.\d+)?\.\d+\.tmp", re.DOTALL
    )
    numbered = [(int(match[1]), p) for p in listing if (match := rotated.fullmatch(p.name)) is not None and _is_file(p)]
    found = [p for _, p in sorted(numbered, reverse=True)]
    if base in listing and _is_file(base):
        found.append(base)
    found.extend(p for p in listing if leftover.fullmatch(p.name) and _is_file(p))
    return found


def _default_set(root: Path) -> tuple[list[Path], list[tuple[Path, OSError]]]:
    errors: list[tuple[Path, OSError]] = []
    dirs = [root]
    try:
        dirs += sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except FileNotFoundError:
        return [], errors
    except OSError as error:
        errors.append((root, error))
    files: list[Path] = []
    for directory in dirs:
        try:
            for name in CAPTURE_NAMES:
                files.extend(_family(directory / name))
        except OSError as error:  # noqa: PERF203  one unreadable directory is recorded, the rest are still listed
            errors.append((directory, error))
    return files, errors


def _add_family(capture: Path, paths: list[Path], listing_errors: list[tuple[Path, OSError]]) -> None:
    try:
        paths.extend(_family(capture))
    except OSError as error:  # an unreadable directory is incomplete (exit 3), never "nothing found"
        listing_errors.append((capture.parent, error))


def _unique(paths: Sequence[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = os.path.realpath(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _shown(path: Path, home: Path) -> str:
    """Relative to the project home, also for a path outside it (`../x`): never an absolute path."""
    absolute = os.path.abspath(path)  # noqa: PTH100  not resolved: a symlink keeps its own name
    return os.path.relpath(absolute, home)


def _is_leftover(shown: str) -> bool:
    """A `.splitting`, `.scrub-late-*` or hidden split temp file: the scrub does not read these."""
    return re.search(r"(^|/)\.[^/]+\.\d+\.tmp$|\.scrub-late-|\.splitting$", shown) is not None


def _family_name(shown: str) -> str:
    shown = re.sub(r"(^|/)\.([^/]+)\.\d+\.tmp$", r"\1\2", shown)  # the hidden temp of a split
    shown = re.sub(r"\.scrub-late-.*$|\.splitting$", "", shown)  # a leftover
    return re.sub(r"\.\d+$", "", shown)  # a rotated file


# ---- run ------------------------------------------------------------------------------------------


def _open(result: FileResult) -> IO[bytes] | None:
    """Open `result.path` and take its size at open; on failure record the error and return None."""
    try:
        handle: IO[bytes] = result.path.open(
            "rb", buffering=0
        )  # unbuffered: the buffer would hold 128 KB beside the line cap
    except OSError as error:
        result.error = _error_name(error)
        return None
    try:
        info = os.fstat(handle.fileno())
    except OSError as error:
        handle.close()
        result.error = _error_name(error)
        return None
    if not stat.S_ISREG(info.st_mode):
        handle.close()
        result.error = "not a regular file"
        return None
    result.size = info.st_size
    return handle


def run(
    paths: list[Path],
    home: Path,
    max_seconds: float,
    clock: Callable[[], float],
    listing_errors: Sequence[tuple[Path, OSError]] = (),
) -> list[FileResult]:
    """Open the files first and take each size at open, so a rotation during the scan neither repeats nor
    skips a file; then read each to that size, within the time budget. When the process runs out of file
    descriptors the rest are opened one by one, each when its turn comes, and closed after it."""
    results: list[FileResult] = []
    opened: list[tuple[FileResult, IO[bytes] | None]] = []
    for path, error in listing_errors:
        results.append(FileResult(path, _shown(path, home), error=_error_name(error)))
    try:
        out_of_descriptors = False
        for path in paths:
            result = FileResult(path, _shown(path, home))
            results.append(result)
            if out_of_descriptors:
                opened.append((result, None))
                continue
            handle = _open(result)
            if result.error and result.error.startswith(("EMFILE", "ENFILE")):
                out_of_descriptors, result.error = True, None
                opened.append((result, None))  # opened at its turn
            elif handle is not None:
                opened.append((result, handle))
        start = clock()

        def expired() -> bool:
            return max_seconds > 0 and clock() - start > max_seconds

        for position, (result, handle) in enumerate(opened):
            if handle is None:
                handle = _open(result)  # deferred: a descriptor is free again
                if handle is None:
                    continue
                opened[position] = (result, handle)
            try:
                scan_file(result, handle, expired)
            except _Expired:  # one unreadable file is recorded, the rest are still read
                pass
            except OSError as error:
                result.error = _error_name(error)
            if out_of_descriptors:
                handle.close()  # frees the descriptor for the next deferred file
    finally:
        for _, handle in opened:
            if handle is not None:
                handle.close()
    return results


def _totals(results: Sequence[FileResult]) -> dict[str, int]:
    return {name: sum(getattr(r, name) for r in results) for name in ("lines", *FIGURES)}


def exit_code(results: Sequence[FileResult]) -> int:
    if any(r.found for r in results):
        return 1
    return 0 if all(r.complete for r in results) else 3


def _unscanned(result: FileResult) -> int:
    return max(result.size - result.bytes_scanned, 0)


def _line(result: FileResult) -> str:
    if result.error and not result.size and not result.lines:
        return f"{result.shown}: not scanned ({result.error})"
    text = (
        f"{result.shown}: {result.lines} lines, content in {result.removable} removable / "
        f"{result.serialized} serialized / {result.unparseable} unparseable lines, identity in "
        f"{result.identity} lines, {result.redacted} lines with the {PLACEHOLDER} placeholder only"
    )
    if result.names:
        text += f", {result.names} skill or agent names that reports clean when they read them"
    if not result.complete:
        why = f" ({result.error})" if result.error else ""
        text += f"; INCOMPLETE: {_unscanned(result)} bytes not scanned{why}"
    return text


def render_text(results: Sequence[FileResult], code: int) -> str:
    totals = _totals(results)
    out = [_line(r) for r in results]
    out.append(
        f"total: {len(results)} files, {totals['lines']} lines, {totals['removable']} removable, "
        f"{totals['serialized']} serialized, {totals['unparseable']} unparseable, "
        f"{totals['identity']} identity, {totals['redacted']} redacted"
    )
    if totals["names"]:
        out[-1] += f", {totals['names']} skill or agent names that reports clean when they read them"
    if code == 1:
        found = [r.shown for r in results if r.found]
        families = sorted({_family_name(shown) for shown in found if not _is_leftover(shown)})
        leftovers = sorted(shown for shown in found if _is_leftover(shown))
        out.append("content or identity attributes are still on disk.")
        if families:
            out.append("To see what the scrub would change:")
            out.extend(f"  python3 -B scripts/scrub_capture.py --dry-run --capture {family}" for family in families)
        if leftovers:
            out.append("The scrub does not read leftovers (`.splitting`, `.scrub-late-*`, hidden split temp files):")
            out.extend(f"  {shown}" for shown in leftovers)
            out.append("Deal with each by hand, then scrub:")
            out.append("  `.splitting`: rename it back to the capture's name (a split set it aside).")
            out.append(
                "  `.scrub-late-*`: append its lines to the capture, then delete it (a scrub could not write them back)."
            )
            out.append(
                "  hidden `.<name>.N.<pid>.tmp`: a split cut short; delete it once its records are in the split files."
            )
        out.append("This check never rewrites a capture; rewriting one is a separate step the operator decides.")
    if code in (1, 3) and not all(r.complete for r in results):
        out.append("incomplete: some files were not read to their end; this result is not a clean bill.")
    if code == 0:
        note = f" ({totals['redacted']} lines hold only the {PLACEHOLDER} placeholder)" if totals["redacted"] else ""
        out.append(f"clean: no content or identity attributes found{note}")
    return "\n".join(out)


def render_json(results: Sequence[FileResult], code: int) -> str:
    files = []
    for r in results:
        entry = asdict(r)
        entry.update(path=r.shown, bytes_unscanned=_unscanned(r))
        for drop in ("shown", "size"):
            entry.pop(drop, None)
        files.append(entry)
    body = {
        "files": files,
        "totals": _totals(results),
        "complete": all(r.complete for r in results),
        "exit_code": code,
    }
    return json.dumps(body, indent=2)


def main(
    argv: Sequence[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--capture", type=Path, action="append", help="scan this capture (and its rotated files)")
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS, help="time budget; 0 for none")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    if not args.max_seconds >= 0:  # also rejects nan
        parser.error("--max-seconds must be 0 (no limit) or more")

    home = project_home(Path.cwd())
    listing_errors: list[tuple[Path, OSError]] = []
    looked_at: list[Path]
    if args.capture:
        looked_at = args.capture
        paths: list[Path] = []
        for capture in args.capture:
            _add_family(capture, paths, listing_errors)
    else:
        root = home / "logs"
        looked_at = [root]
        paths, listing_errors = _default_set(root)
        extra = os.environ.get("CLAUDE_OBSERVER_CAPTURE")
        if extra and not Path(os.path.abspath(extra)).is_relative_to(root):  # noqa: PTH100
            _add_family(Path(extra), paths, listing_errors)
    paths = _unique(paths)
    if not paths and not listing_errors:
        shown = ", ".join(_shown(p, home) for p in looked_at)
        print(f"no capture found at {shown}", file=sys.stderr)
        return 2

    results = run(paths, home, args.max_seconds, clock, listing_errors)
    code = exit_code(results)
    print(render_json(results, code) if args.json else render_text(results, code))
    return code


if __name__ == "__main__":
    sys.exit(main())
