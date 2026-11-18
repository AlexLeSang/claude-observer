#!/usr/bin/env python3
"""Remove identity and content attributes from captures written before they were dropped.

    python3 scripts/scrub_capture.py [--capture logs/agent-telemetry.jsonl] [--dry-run]

Rewrites the capture and its rotated files (`<capture>.N`) in one pass, using the
receiver's own drop list (`IDENTITY_ATTRIBUTES`) on OTLP attribute lists and on
the keys of native plugin records. A line that is not a JSON object (cut by a crash, two records
fused, not JSON, an array) is replaced whole by a fixed marker, `TORN_MARKER`, which is not JSON: no
reader uses such a line, and a pattern cannot tell its content from the rest, so nothing of it is
kept. The line count is unchanged; the summary counts these lines ("N unparseable lines replaced by a
marker", line numbers only; this replaced an earlier keep-every-line rule). A line with
invalid UTF-8 is scrubbed all the same and each bad byte stays that byte (`_scrub_bytes`
names the one case where it cannot); a symlinked capture is rewritten at its target. The per-file
record counts before and after must match or the file is left as it was. The file is replaced
atomically with its mode kept (0600).

It removes keys in dicts and lists, and inside JSON text held in an OTLP `stringValue`
(an earlier appender wrote a dict-valued OTel attribute as a JSON string): such text is parsed and cleaned by `mca.drop_sensitive`, and text that is too
deep or cut short while naming a sensitive key counts as unscrubbed. It also removes the OTLP
attribute entries named in `CONTENT_ATTRIBUTES` (`prompt`, `prompt_text`, `response`,
`tool_*`, `error`) wherever they sit in an attribute list; a plain field of a
native record with one of those names is left alone. JSON text in any string value is cleaned
the same way (text serialized twice, a BOM or space before it, a key with space around it); a
name used as a key in text that is not JSON as a whole counts as unscrubbed. The text
is first normalised (JSON escapes, `%XX`, HTML entities, NFKC, zero-width characters), so most
spellings are found; the ones it does not find (`\\x70`, base64, camelCase, a tab or backtick as separator) stay. A line holding a CR or BOM that does not parse whole is scrubbed per CR-separated piece,
BOM kept.

A running writer can append between the read and the replace, so a file whose
size changed during the pass is left untouched and reported; run it again, or
stop the receiver first. A record that lands on the old file in the instant of the
replace is read back from it and appended, scrubbed, to the new one; if that append
fails the scrubbed records are saved to `<capture>.scrub-late-*` (0600) and the
error names that file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import agent_telemetry as at
import mod_capture_append as mca

LISTED_LINES = 5  # how many unscrubbed line numbers a summary names
# What a line that cannot be read as a JSON object becomes (see `_cannot_scrub`).
TORN_MARKER = "# unreadable line removed by scrub_capture"


class ScrubError(RuntimeError):
    """The file changed during the pass, or the record count would change."""


@dataclass(frozen=True)
class ScrubResult:
    path: Path
    before: int
    after: int
    changed: int
    unscrubbed: int = 0
    # 1-based numbers of the first few unscrubbed lines (numbers only, never content).
    unscrubbed_lines: tuple[int, ...] = ()
    # Lines that do not parse as a JSON object and were replaced by the marker; numbers only.
    blanked: int = 0
    blanked_lines: tuple[int, ...] = ()


drop_content = mca.drop_content  # the appender runs the same function
# The scrub's own tests, named so `check_capture_content` shares them instead of copying.
is_content_entry = mca.is_content_entry  # the list-item test `drop_content` applies


def torn_line_is_sensitive(line: str) -> bool:
    """Whether a line that is not a JSON object uses a content or identity name as a key, as the scrub's
    text search reads it; text that cannot be searched counts. The scrub replaces every such line by
    `TORN_MARKER` whatever this says: it tells the read-only check which of them held a name."""
    try:
        return mca._names_a_key(line)  # noqa: SLF001  (the scrub and the read-only check share mca's one key test, not a copy)
    except mca.UncheckableError:
        return True


# Lines scrub_line could not process (too deeply nested, a repeated key, or any other failure
# while scrubbing a line that parses). A one-shot CLI counts them here; `_scrub_file` reads the difference.
_unscrubbed = 0
# Lines that did not parse as a JSON object and were replaced by TORN_MARKER.
_blanked = 0


class DuplicateKeyError(ValueError):
    """An object repeats a key: `json.loads` would keep only the last copy, so the
    shadowed one could not be scrubbed or even seen."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record = dict(pairs)
    if len(record) != len(pairs):
        raise DuplicateKeyError
    return record


parse_object = _object  # `object_pairs_hook` that refuses a repeated key; the check parses lines the same way


def _cannot_scrub(line: str) -> str:
    """The marker for a line that cannot be read as a JSON object (torn by a crash, two writes fused,
    not JSON at all, an array or a bare string). Nothing of such a line is kept: no reader uses it, and
    no pattern can tell content from the rest in text whose structure is lost, so the only line sure to
    hold no identity or content is one with nothing of the original left (this replaced an earlier
    keep-every-line rule). The marker is not JSON, so the count of records is unchanged. It
    counts as blanked; a marker from an earlier run is left as it is and not counted again."""
    global _blanked  # noqa: PLW0603  (module counter the one-shot CLI and _scrub_file read by difference)
    if line == TORN_MARKER:
        return line
    _blanked += 1
    return TORN_MARKER


def scrub_line(line: str) -> str:
    """The line without identity and content attributes. A line that is not a JSON object is replaced by
    the marker (`_cannot_scrub`), a line too deep to parse that is not one complete object too
    (`_is_one_object`); one that scrubbing fails on comes back unchanged and counts as unscrubbed:
    a complete object too deep to parse, a repeated object key, or a line that has something to remove
    but cannot be written as JSON."""
    # Readers open the file in text mode, so a CR ends a line for them and a BOM is not part
    # of a record. A line that does not parse whole and holds either is scrubbed piece by
    # piece, so it can be purged like any other; BOM and CR are kept.
    if "\r" in line or "﻿" in line:
        try:
            json.loads(line, object_pairs_hook=_object)
        except (ValueError, RecursionError):
            return "\r".join(_scrub_piece(piece) for piece in line.split("\r"))
    return _scrub_piece(line)


def _scrub_piece(line: str) -> str:
    """`scrub_line` for one line without a CR, after any BOMs at its start."""
    bom = line[: len(line) - len(line.lstrip("﻿"))]
    rest = line[len(bom) :]
    return bom + (rest if not rest.strip() else _scrub_record(rest))  # blank pieces: nothing to scrub


def _parse_record(line: str) -> dict[str, Any] | None:
    """The line as a JSON object, or None when it is not valid JSON or not an object. A line too deep for the parser
    (`RecursionError`) or repeating a key (`DuplicateKeyError`) raises."""
    try:
        record = json.loads(line, object_pairs_hook=_object)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _clean_line(line: str, record: dict[str, Any]) -> str:
    """The parsed record without its identity and content entries, as the line to write."""
    global _unscrubbed  # noqa: PLW0603  (module counter the one-shot CLI and _scrub_file read by difference)
    clean = drop_content(
        mca.drop_identity_keys(at.drop_identity(mca.without_entry_record(record)))
    )  # as mca.clean_record, counting
    if clean == record:
        return line
    try:
        return json.dumps(clean, allow_nan=False, ensure_ascii=mca.ensure_ascii())
    except ValueError:  # nan or infinity (1e999) has no JSON form, and `clean != record`
        _unscrubbed += 1  # proves a sensitive entry is in the line, whatever spelling its key has
        return line


def _keep_or_blank(line: str) -> str:
    """A line the scrub could not walk. A line that is not one complete object is torn (a crash cut it), an array
    or two records fused, and nothing of it is kept, like any torn line; a complete one is kept and counted."""
    global _unscrubbed  # noqa: PLW0603  (module counter the one-shot CLI and _scrub_file read by difference)
    if _is_one_object(line):
        _unscrubbed += 1
        return line
    return _cannot_scrub(line)


def _scrub_record(line: str) -> str:
    try:
        record = _parse_record(line)
        if record is None:
            return _cannot_scrub(line)
        return _clean_line(line, record)
    except (RecursionError, ValueError):
        # Too deep for the parser, a repeated key (the shadowed copy cannot be seen) or text the walk refuses.
        return _keep_or_blank(line)


_CLOSER = {"{": "}", "[": "]"}
_JSON_SPACE = " \t\n\r"


@dataclass
class _BracketScan:
    """The state of `_is_one_object`'s walk: the closers still owed, and whether it is inside a string."""

    stack: list[str] = field(default_factory=list)
    in_string: bool = False
    escaped: bool = False
    started: bool = False

    def feed(self, char: str) -> bool:
        """Take one character; False when the text cannot be exactly one complete object."""
        if self.in_string:
            self._in_string(char)
            return True
        if not self.stack and self.started:
            return char in _JSON_SPACE
        if char == '"':
            return self._open_string()
        if char in _CLOSER:
            return self._open_bracket(char)
        if char in "}]":
            return bool(self.stack) and self.stack.pop() == char
        return bool(self.stack) or char in _JSON_SPACE

    def _in_string(self, char: str) -> None:
        if self.escaped:
            self.escaped = False
        elif char == "\\":
            self.escaped = True
        elif char == '"':
            self.in_string = False

    def _open_string(self) -> bool:
        if not self.stack:
            return False
        self.in_string = True
        return True

    def _open_bracket(self, char: str) -> bool:
        if not self.stack and char != "{":
            return False
        self.stack.append(_CLOSER[char])
        self.started = True
        return True


def _is_one_object(text: str) -> bool:
    """Whether `text` is, as far as brackets and quotes go, exactly one complete `{...}`: it starts with `{`,
    every bracket is matched, no string is left open and nothing follows the closing bracket. A loop, not a
    recursion, so a line of any depth is answered; the rest of JSON's syntax is not checked."""
    scan = _BracketScan()
    for char in text:
        if not scan.feed(char):
            return False
    return scan.started and not scan.stack and not scan.in_string


# What `surrogateescape` makes of one invalid byte, and any escaped surrogate (`\ud83d`) a line may hold.
_BAD_BYTE = re.compile("[\udc80-\udcff]")
# A JSON escape needs an odd run of backslashes: `\\\\udemy` (an escaped backslash, then text) is not one.
_SURROGATE_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\u[dD][89a-fA-F]")


def _scrub_bytes(line: bytes) -> bytes:
    """`scrub_line` on bytes. Invalid UTF-8 is read as lone surrogates (`surrogateescape`), one character
    per byte, so every name check sees the same width and boundaries as for any other character. A line
    with a bad byte is written back with its non-ASCII characters raw (`mca.raw_text_output`), so each
    surrogate goes back to its byte and only what the scrub removes changes; an unchanged line is
    returned byte for byte (an earlier version carried each byte as a stand-in string and shifted
    the length-bounded name patterns).

    A line that also holds an escaped surrogate (`\\udcff`, or half of an emoji pair) is read the same
    way but written with escapes: raw output could not tell the escape from the byte. Each bad byte then
    comes back as the text `\\udcXX`; the line is still scrubbed."""
    text = line.decode("utf-8", "surrogateescape")
    try:
        return _scrub_text(text)
    except Exception:  # no single line may abort a file's scrub or leave its secrets
        return _blank_line(line)


def _scrub_text(text: str) -> bytes:
    """The scrub of one decoded line, as bytes. Raw output is kept only if it is sure to be right: no
    surrogate escape was parsed at any level (`mca.escape_in_text`, the line's own escapes by
    `_SURROGATE_ESCAPE`) and the result encodes. Otherwise the line is scrubbed again with escapes."""
    if _BAD_BYTE.search(text) and not _SURROGATE_ESCAPE.search(text):
        counts = _blanked, _unscrubbed
        try:
            with mca.raw_text_output():
                clean = scrub_line(text).encode("utf-8", "surrogateescape")
                if not mca.escape_in_text():
                    return clean
        except UnicodeEncodeError:
            pass
        _reset_counts(*counts)
    return scrub_line(text).encode("utf-8", "surrogateescape")


def _reset_counts(blanked: int, unscrubbed: int) -> None:
    global _blanked, _unscrubbed
    _blanked, _unscrubbed = blanked, unscrubbed


def _blank_line(line: bytes) -> bytes:
    """Last resort for a line the scrub raised on: a record becomes `{}` and anything else the marker, as
    `_cannot_scrub` does, both counted as blanked, so nothing of the line is kept. Never raises."""
    global _blanked  # noqa: PLW0603  (module counter the one-shot CLI and _scrub_file read by difference)
    if _is_record(line):
        _blanked += 1
        return b"{}"
    return _cannot_scrub(line.decode("utf-8", "surrogateescape")).encode()


def scrub_bytes_noted(line: bytes) -> tuple[bytes, bool, bool]:
    """`_scrub_bytes` and whether the line had values blanked by pattern, and whether it is
    left unscrubbed. Callers outside this module use it to report both by line number."""
    blanked, unscrubbed = _blanked, _unscrubbed
    clean = _scrub_bytes(line)
    return clean, _blanked > blanked, _unscrubbed > unscrubbed


def _is_record(line: bytes) -> bool:
    try:
        return isinstance(json.loads(line.decode("utf-8", "surrogateescape")), dict)
    except (ValueError, RecursionError):
        return False


def _split(raw: bytes) -> list[bytes]:
    lines = raw.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    return lines


def _scrub_numbered(lines: list[bytes], first: int, failed_lines: list[int], blanked_lines: list[int]) -> list[bytes]:
    """The scrubbed lines; the numbers (from `first`) of those that were blanked or left unscrubbed are added to
    the two lists."""
    scrubbed = []
    for number, line in enumerate(lines, first):
        clean, was_blanked, failed = scrub_bytes_noted(line)
        scrubbed.append(clean)
        if was_blanked:
            blanked_lines.append(number)
        if failed:
            failed_lines.append(number)
    return scrubbed


def _result(
    shown: Path,
    counts: tuple[int, int, int],
    notes: tuple[int, int],
    failed_lines: list[int],
    blanked_lines: list[int],
) -> ScrubResult:
    """The `ScrubResult` from (before, after, changed), (unscrubbed, blanked) and the two line-number lists."""
    return ScrubResult(
        shown,
        counts[0],
        counts[1],
        counts[2],
        notes[0],
        tuple(failed_lines[:LISTED_LINES]),
        notes[1],
        tuple(blanked_lines[:LISTED_LINES]),
    )


def _swap_in(path: Path, handle: BinaryIO, size: int, output: bytes) -> None:
    """Replace `path` by `output` atomically, mode 0600 and times kept; raises `ScrubError` when the file is no
    longer the one that was read."""
    stat = os.fstat(handle.fileno())
    try:
        now = path.stat()
    except FileNotFoundError:
        now = None
    if now is None or (now.st_dev, now.st_ino) != (stat.st_dev, stat.st_ino) or now.st_size != size:
        raise ScrubError(f"{path} changed during the pass; nothing written")
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".scrub-")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(output)
            tmp.flush()
            os.fsync(tmp.fileno())
        Path(tmp_name).chmod(0o600)
        # A rotated file's mtime is its newest record (prune_rotated), so keep it.
        os.utime(tmp_name, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(tmp_name, path)  # noqa: PTH105  (Path.replace binds os.replace early on 3.10; tests patch it)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _append_late(
    path: Path, late: bytes, already: int, failed_lines: list[int], blanked_lines: list[int]
) -> tuple[int, int]:
    """Scrub the records a writer appended to the old file during the pass and append them to the new one. Returns
    how many lines were left unscrubbed and how many were blanked; raises `ScrubError` when the append fails."""
    late_lines = _split(late)
    count, blanked_at = _unscrubbed, _blanked
    tail = b"".join(clean + b"\n" for clean in _scrub_numbered(late_lines, already + 1, failed_lines, blanked_lines))
    unscrubbed, blanked = _unscrubbed - count, _blanked - blanked_at
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        with os.fdopen(fd, "ab") as out:
            out.write(tail)
    except OSError as error:
        raise ScrubError(_late_tail_message(path, path.name, tail, len(late_lines), unscrubbed, error)) from error
    return unscrubbed, blanked


def _scrub_file(shown: Path, dry_run: bool) -> ScrubResult:
    path = shown.resolve()  # a symlinked capture is rewritten in place at its target, not replaced by a file
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        raw = handle.read(size)
        lines = _split(raw)
        failed_before, blanked_before = _unscrubbed, _blanked
        failed_lines: list[int] = []
        blanked_lines: list[int] = []
        scrubbed = _scrub_numbered(lines, 1, failed_lines, blanked_lines)
        unscrubbed = _unscrubbed - failed_before
        blanked = _blanked - blanked_before
        changed = sum(1 for old, new in zip(lines, scrubbed, strict=False) if old != new)
        if not dry_run:
            os.fchmod(handle.fileno(), 0o600)
        if dry_run or not changed:
            counts = (len(lines), len(scrubbed), changed)
            return _result(shown, counts, (unscrubbed, blanked), failed_lines, blanked_lines)
        output = b"".join(line + b"\n" for line in scrubbed)
        after = _split(output)
        if len(after) != len(lines) or sum(map(_is_record, after)) != sum(map(_is_record, lines)):
            raise ScrubError(f"{path}: the record count would change; nothing written")
        _swap_in(path, handle, size, output)
        # A writer that had the old file open can still append to it after the size
        # check above; carry those records over to the new file.
        late = handle.read()
    if late:
        more_unscrubbed, more_blanked = _append_late(path, late, len(lines), failed_lines, blanked_lines)
        unscrubbed += more_unscrubbed
        blanked += more_blanked
    return _result(shown, (len(lines), len(after), changed), (unscrubbed, blanked), failed_lines, blanked_lines)


def _late_tail_message(path: Path, name: str, tail: bytes, count: int, unscrubbed: int, error: OSError) -> str:
    """Save the tail beside the capture and say where; never print its content. Lines of it
    that could not be scrubbed are saved as they were, and only their count is said."""
    head = f"{path} was scrubbed but {count} record(s) written during the pass could not be appended to it ({error})"
    try:
        fd, saved = tempfile.mkstemp(dir=path.parent, prefix=name + ".scrub-late-")
        with os.fdopen(fd, "wb") as out:
            out.write(tail)
    except OSError as save_error:
        return f"{head}, and could not be saved elsewhere either ({save_error}); they are lost"
    if unscrubbed:
        what = f"the records are in {saved} (mode 0600), {unscrubbed} of them left unscrubbed and still holding their original content"
    else:
        what = f"the scrubbed records are in {saved} (mode 0600)"
    return f"{head}; {what}: append them to {path} by hand"


def scrub_files(paths: list[Path], dry_run: bool = False) -> list[ScrubResult]:
    return [_scrub_file(path, dry_run) for path in paths]


def _numbers(lines: tuple[int, ...], total: int) -> str:
    """`line 3, 7, ...`: numbers only, never content."""
    return f"line {', '.join(map(str, lines))}{', ...' if total > len(lines) else ''}"


def describe(name: str, result: ScrubResult, dry_run: bool = False) -> str:
    """The one-line report for a file: counts and line numbers only, never content. `main` prints it and the doctor's
    excerpt quotes it, so both say the same thing."""
    verb = "would change" if dry_run else "changed"
    note = ""
    if result.blanked:
        note += f", {result.blanked} unparseable lines replaced by a marker ({_numbers(result.blanked_lines, result.blanked)})"
    if result.unscrubbed:
        listed = ", ".join(map(str, result.unscrubbed_lines))
        more = ", ..." if result.unscrubbed > len(result.unscrubbed_lines) else ""
        note += (
            f", {result.unscrubbed} lines left unscrubbed (line {listed}{more}); they still hold their original content"
        )
    return f"{name}: {result.before} -> {result.after} records, {verb} {result.changed}{note}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture", type=Path, default=at.DEFAULT_CAPTURE)
    parser.add_argument("--dry-run", action="store_true", help="count, change nothing")
    args = parser.parse_args(argv)

    paths = [*at.rotated_captures(args.capture)]
    if args.capture.is_file():
        paths.append(args.capture)
    if not paths:
        print(f"No capture found at {args.capture}", file=sys.stderr)
        return 1
    status = 0
    for path in paths:
        try:
            (result,) = scrub_files([path], args.dry_run)
        except (ScrubError, OSError) as error:  # OSError: a read-only directory, a vanished file
            print(error, file=sys.stderr)
            status = 1
            continue
        print(describe(path.name, result, args.dry_run))
        if result.unscrubbed:
            status = 1  # the capture still holds unscrubbed content; distinct from a usage error (2)
    return status


if __name__ == "__main__":
    sys.exit(main())
