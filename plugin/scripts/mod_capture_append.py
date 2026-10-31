#!/usr/bin/env python3
"""Append a batch of records from the claude-observer plugin to its capture.

The plugin's hooks module (`plugin/hooks/observer.ts`) runs inside Claude Code with no
filesystem append of its own, so it pipes each batch of JSON lines here:

    python3 scripts/mod_capture_append.py --out <project>/logs/agent-telemetry.mod.jsonl

Every line is one JSON object. Lines of `kind: "otel"` mirror a Claude Code
OTel log event (`event`, `attributes`, `ts`); they are wrapped in the receiver's
OTLP/HTTP-JSON record shape, so `agent_telemetry.py report --tools` and
`--failures` read the plugin's capture unchanged. Every other line is a native
record and is written as it stands.

The capture is written through the receiver's own `append_record`: 0600 file,
0700 directory when created, size rotation and age retention. Identity
attributes are dropped here as well as in the plugin, so a plugin bug cannot
put them on disk; so are the content attributes of an OTel event (prompt,
response, tool input and output), by name in any letter case, at the top level
and at any depth inside an attribute's value: a nested dict or list, or JSON text
held in a string. An attribute nested deeper than MAX_DEPTH, or JSON
text cut short that names such a key, is dropped whole, since it cannot be checked. Text that is
not JSON but uses such a name as a key is dropped too. All text is first normalised (`_normalised`: JSON
escapes of any character, `%XX`, HTML entities, then NFKC with invisible characters removed) and then
searched, so those spellings are found; a bare `prompt:refine` is a plugin's skill name, not a key. The
spellings this does not cover (`\\x70`, base64, camelCase, a tab or backtick as separator) and a complete object too
deep for Python's JSON parser (kept by the scrub and counted as unscrubbed) are not covered. An OTel list attribute is also bounded once
wrapped in the OTLP record (`_ATTRIBUTE_VALUE_DEPTH`): one that is too deep is dropped alone.
Bad lines are skipped and counted on stderr;
the exit code is 0 unless the arguments are wrong, so a malformed record never costs a batch.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import html
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import agent_telemetry as at
import capture_exclude

if TYPE_CHECKING:
    from collections.abc import Iterator

DEFAULT_MOD_CAPTURE = Path.cwd() / "logs" / "agent-telemetry.mod.jsonl"

# OTel event attributes that hold what the user or the model wrote or what a tool
# took and returned. The plugin drops them (CONTENT_ATTRIBUTES in
# plugin/hooks/records.ts, kept equal by a test); the appender drops them again.
CONTENT_ATTRIBUTES = frozenset(
    {
        "prompt",
        "prompt_text",
        "response",
        "tool_parameters",
        "tool_input",
        "tool_output",
        "tool_result_content",
    }
)

# How many levels of dicts and lists an OTel attribute value may nest. Claude Code's
# attributes are flat (a string, number, boolean or list of strings); a deeper value
# is dropped whole rather than walked, so no input can exhaust the recursion limit.
MAX_DEPTH = 32

# How many levels of dicts and lists a field of a capture record may nest. The appender and the scrub
# both stop here (UncheckableError), well before the interpreter's recursion limit, so what the
# appender writes is always something the scrub can walk.
MAX_RECORD_DEPTH = 100


def may_exceed_depth(openers: int, size: int, escaped: bool) -> bool:
    """Whether a capture line of `size` bytes could nest deeper than `MAX_DEPTH` the way `drop_sensitive` counts it,
    judged without parsing it (the check's decision to parse a line that names nothing).

    `openers` is the number of bytes `{` and `[` (at least the dict and list levels; a bracket spelled as a `\\u`
    escape is for the caller to catch, as the check's name prefilter does). Each layer of JSON text held in a string, which counts one
    level, needs its quote escaped at that layer: a backslash, and `2**layer - 1` characters at the layer's depth, so
    a line of `size` bytes holds at most `size.bit_length()` layers, and none without a backslash (`escaped`). It is a
    ceiling, never a count: a line it refuses may still be fine, a line it passes cannot nest past `MAX_DEPTH`."""
    return openers + (size.bit_length() if escaped else 0) > MAX_DEPTH


class UncheckableError(ValueError):
    """An attribute value cannot be cleaned: nested deeper than MAX_DEPTH, or JSON text
    that does not parse and names a sensitive key."""


# A content or identity name anywhere in text, in any letter case.
_NAMES = "|".join(map(re.escape, sorted(CONTENT_ATTRIBUTES | at.IDENTITY_ATTRIBUTES)))
_IDENTITY_NAMES = "|".join(map(re.escape, sorted(at.IDENTITY_ATTRIBUTES)))
_NAMED = re.compile(_NAMES, re.IGNORECASE)

# How the text is searched: it is first `_normalised` (every spelling a reader would decode is
# decoded), then these patterns look for a name used as a key. They run on text with no escapes left, so
# they know nothing about encodings.
_GAP = r"[\s\\]*"  # space (any Unicode space), or a backslash left over from an escape that was not one
# `a[]=x`, `a[0]=x`, `a[k][j]=x`: a query-string array after the name; `event.prompt`, `a.b.prompt=`.
_INDEX = rf"(?:{_GAP}\[[^\[\]]{{0,32}}\]){{0,4}}"
_TAIL = rf"(?:\.\w{{1,32}}){{0,4}}{_INDEX}"
# A name used as a key: not inside a longer word, then `=`, or a quote and `:`/`=`, or `:` followed by
# space, a quote, a bracket or the end; or right after `{` or `,`, where anything with a colon is a pair.
# A bare `prompt:refine` (a plugin's skill or agent name) is a name, not a key.
# Only a content name gets that exemption: an identity name (`user.email`) is never a skill name, so
# `user.email:x` is a key.
# Every start is a name, `{` or `,`, so a run of backslashes or quotes cannot make the search slow.
_KEYED_NAME = re.compile(
    rf"(?<!\w)(?:{_NAMES}){_TAIL}{_GAP}(?:=|[\"']{_GAP}[:=]"
    rf"|:(?=[\s\"'\[{{]|\Z))"
    rf"|(?<!\w)(?:{_IDENTITY_NAMES}){_TAIL}{_GAP}:"
    rf"|[{{,]{_GAP}[\"']?(?:{_NAMES}){_TAIL}{_GAP}[\"']?{_GAP}[:=]",
    re.IGNORECASE,
)
# An attribute entry written as text: `key=prompt`, `"name": "user.email"`.
_ENTRY_NAME = re.compile(
    rf"(?<!\w)(?:key|name)(?!\w)[\s\\\"']*[:=][\s\\\"']*(?:{_NAMES})(?![\w.])",
    re.IGNORECASE,
)
# One escape of JSON, under any number of backslashes (text serialized several times): `\uXXXX` of any
# character, `\n`, `\"`, `\/`. It starts only at the first backslash of a run, so it stays linear.
_ESCAPE = re.compile(r"(?<!\\)\\+(?:u([0-9a-fA-F]{4})|([nrtbf\"/]))")
_LETTERS = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", '"': '"', "/": "/"}
_READ_PASSES = 8
# Characters a renderer shows as nothing that are not format characters (category Cf): Unicode's
# Default_Ignorable_Code_Point set minus Cf. U+034F and the Khmer vowels are Mn, the Hangul fillers Lo, the
# variation selectors Mn, the rest unassigned: U+2065 sits between the Cf runs U+2060-2064 and U+2066-206F. NFKC maps U+3164 and U+FFA0 to U+1160, so it is listed too.
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2065, 0x2065),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0xE0000, 0xE0FFF),
)


def _invisible(char: str) -> bool:
    code = ord(char)
    return unicodedata.category(char) == "Cf" or any(low <= code <= high for low, high in _INVISIBLE_RANGES)


def _read_once(text: str) -> str:
    text = _ESCAPE.sub(lambda m: chr(int(m[1], 16)) if m[1] else _LETTERS[m[2]], text)
    if "%" in text:
        text = unquote(text, errors="replace")
    if "&" in text:
        text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    if not text.isascii():
        # A surrogate pair written as two escapes is one character (a mathematical bold `p`).
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
        text = unicodedata.normalize("NFKC", "".join(c for c in text if not _invisible(c)))
    return text


def _normalised(text: str) -> str:
    """`text` as a reader would see it: every JSON escape (`\\uXXXX` of any character, `\\n`, `\\"`), `%XX`
    (UTF-8) and HTML entity read, again and again until none is left, then NFKC (fullwidth and look-alike
    forms) and invisible characters (zero width, soft hyphen, variation selectors) removed. Letter case is
    left to the patterns (IGNORECASE). Raises UncheckableError when more than _READ_PASSES rounds are
    needed (escapes of escapes of escapes), since the text then cannot be shown to be clean."""
    for _ in range(_READ_PASSES):
        read = _read_once(text)
        if read == text:
            return text
        text = read
    raise UncheckableError


def _reads_as_itself(text: str) -> bool:
    """Whether `_normalised(text)` is `text`, known without reading it: ASCII (NFKC leaves it alone, and no
    invisible character is ASCII) with no backslash (`_ESCAPE`), `%` (`unquote`) or `&` (`html.unescape`),
    so not one of the five rewriting steps of `_read_once` can change it."""
    return text.isascii() and "\\" not in text and "%" not in text and "&" not in text


def _readings(text: str) -> tuple[str, str]:
    """Two readings of text whose backslashes may be escapes or plain characters (`C:\\tool_input:`):
    escapes read (`_normalised`), and every backslash and invisible character taken as a space."""
    if _reads_as_itself(text):  # no backslash: both readings are the text
        return text, text
    plain = text.replace("\\", " ")
    if not plain.isascii():
        plain = "".join(" " if _invisible(c) else c for c in plain)
    return _normalised(text), _normalised(plain)


def _names_a_key(text: str) -> bool:
    """Whether non-JSON text uses a content or identity name as a key. Every alternative of both patterns
    holds one of the names, so a reading that holds none (`_NAMED`, the same alternation and flags) is clean."""
    first, second = _readings(text)
    return any(
        _NAMED.search(read) is not None and (_KEYED_NAME.search(read) or _ENTRY_NAME.search(read)) is not None
        for read in ((first,) if first == second else (first, second))
    )


def _compute_key_name(key: str) -> str:
    try:
        return _normalised(key).strip().casefold()
    except UncheckableError:
        return key.strip().casefold()  # the caller's text check rejects such a key


_cached_key_name = functools.lru_cache(maxsize=2048)(_compute_key_name)
_KEY_CACHE_MAX = 128  # longer keys are computed, so a hostile key cannot fill the cache with megabytes


def _key_name(key: str) -> str:
    """A key as compared with the attribute names: read like text, stripped of space, case folded."""
    return _cached_key_name(key) if len(key) <= _KEY_CACHE_MAX else _compute_key_name(key)


def is_content(key: object) -> bool:
    """Whether `key` names a content attribute, in any letter case and with space around it."""
    return isinstance(key, str) and _key_name(key) in CONTENT_ATTRIBUTES


def is_identity(key: object) -> bool:
    """Whether `key` names an identity attribute, in any letter case and with space around it."""
    return isinstance(key, str) and _key_name(key) in at.IDENTITY_ATTRIBUTES


_ENTRY_KEYS = frozenset({"key", "name"})


def is_sensitive_entry(item: object) -> bool:
    """Whether `item` is an attribute entry (`{"key": name, "value": ...}`, or `name`) for a
    content or identity name, with the key in any letter case and space."""
    return isinstance(item, dict) and any(
        isinstance(field, str) and _key_name(field) in _ENTRY_KEYS and (is_content(name) or is_identity(name))
        for field, name in item.items()
    )


class _RepeatedKey(Exception):  # noqa: N818
    """A JSON object in text repeats a key."""


def _no_repeat(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise _RepeatedKey
    return obj


def is_identity_entry(item: object) -> bool:
    """Whether `item` is an attribute entry (`key` or `name` field, any case and space) for an identity name."""
    return isinstance(item, dict) and any(
        isinstance(field, str) and _key_name(field) in _ENTRY_KEYS and is_identity(name) for field, name in item.items()
    )


def drop_sensitive(value: Any, depth: int = 0) -> Any:
    """An OTel attribute value without content or identity keys at any depth.

    Walks dicts and lists, and JSON text in a string (`'{"response": ...}'`): such a
    string is parsed, cleaned and written back, or kept as it was when nothing was
    removed. Raises UncheckableError past MAX_DEPTH levels.
    """
    if depth > MAX_DEPTH:
        raise UncheckableError
    if isinstance(value, dict):
        if is_sensitive_entry(value):  # an entry on its own, as the text's root
            raise UncheckableError
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if is_content(key) or is_identity(key) or is_sensitive_entry(item):
                continue
            if isinstance(key, str) and _names_a_key(key):  # JSON text used as a key
                raise UncheckableError
            out[key] = drop_sensitive(item, depth + 1)
        return out
    if isinstance(value, list):
        return [drop_sensitive(item, depth + 1) for item in value if not is_sensitive_entry(item)]
    if not isinstance(value, str):
        return value
    text = value.lstrip().lstrip("﻿").lstrip()  # json.loads refuses a BOM
    if text[:1] in ("{", "[", '"'):  # a quote: JSON text serialized twice
        try:
            parsed = json.loads(text, object_pairs_hook=_no_repeat)
        except RecursionError:
            raise UncheckableError from None
        except _RepeatedKey:
            # json.loads keeps the last copy of a repeated key, so a sensitive value in an earlier
            # copy is invisible to the walk and would be written back unchanged.
            if _NAMED.search(_normalised(value)):
                raise UncheckableError from None
            return value
        except ValueError:  # text that only starts like JSON, or JSON cut short
            # A cut `{"tool_output": "te` cannot be cleaned, so a sensitive name in it
            # means the whole attribute goes. A bracket followed by a word (`[response] timed
            # out`) is prose, not cut JSON, and is judged like other text.
            if _cut_json(text):
                if _NAMED.search(_normalised(value)):
                    raise UncheckableError from None
            elif _names_a_key(value):
                raise UncheckableError from None
            return value
        # Text holding an object or list is that value written out: it stays at this depth, so
        # the appender (which writes a dict as text) and the scrub (which reads it) agree on the
        # bound. Text holding a string is another layer of text and counts as a level.
        clean = drop_sensitive(parsed, depth if isinstance(parsed, (dict, list)) else depth + 1)
        if clean == parsed:
            return value
        if SURROGATE_ESCAPE.search(text):  # only read inside raw_text_output, which clears it first
            _escape_in_text.append(True)  # raw output could not tell this escape from a bad byte
        return json.dumps(clean, ensure_ascii=_ensure_ascii)
    if _names_a_key(value):  # JSON, YAML or the like after other text cannot be cleaned
        raise UncheckableError
    return value


# JSON text written back by `drop_sensitive` escapes every non-ASCII character. The scrub of a line with invalid
# UTF-8 reads the bad bytes as lone surrogates and turns this off for that line, so they stay raw and go back to
# bytes; escaped, each would become the text `\udcXX`.
_ensure_ascii = True
_escape_in_text: list[bool] = []  # set while raw: JSON text written back held a surrogate escape (any spelling)
# An odd run of backslashes before `u` (an even one is escaped backslashes and text, `C:\\\\udemy`),
# literal in the text it is parsed from, at every level.
SURROGATE_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\u[dD][89a-fA-F]")


def escape_in_text() -> bool:
    """Whether, inside `raw_text_output`, JSON text held a surrogate escape that was parsed and written back."""
    return bool(_escape_in_text)


def ensure_ascii() -> bool:
    """Whether JSON text written back escapes non-ASCII characters (the default)."""
    return _ensure_ascii


@contextlib.contextmanager
def raw_text_output() -> Iterator[None]:
    """Inside the block, JSON text written back keeps non-ASCII characters (and lone surrogates) as they are."""
    global _ensure_ascii  # noqa: PLW0603
    before, _ensure_ascii = _ensure_ascii, False
    _escape_in_text.clear()
    try:
        yield
    finally:
        _ensure_ascii = before


_JSON_AFTER_BRACKET = re.compile(r"\[\s*(?:[\[{\"\-0-9])")


def _cut_json(text: str) -> bool:
    """Whether text that starts like JSON and does not parse may be JSON that was cut."""
    return text[:1] != "[" or _JSON_AFTER_BRACKET.match(text) is not None


# Attributes and record fields left out because they could not be checked; `append_batch` reports them.
_dropped = 0


def drop_content(value: Any, depth: int = 0) -> Any:
    """`value` without OTLP attribute entries named in CONTENT_ATTRIBUTES, at any depth, and with
    content and identity keys removed from JSON text in any string in it.

    Outside JSON text an entry goes, as a list item or as a dict value, when its `key` field is a content name, or when its `key` or
    `name` field (any case, any space) is an identity name; an entry with `name: "prompt"` stays, since
    the plugin's own records hold such blocks (`prompt_context`). A field of a native record that
    happens to share a name stays. Inside JSON text `drop_sensitive` is stricter (see its docstring). Raises UncheckableError (a ValueError) for
    text that cannot be cleaned, a key that is JSON text naming a sensitive key, or a value nested
    deeper than MAX_RECORD_DEPTH.
    """
    if depth > MAX_RECORD_DEPTH:
        raise UncheckableError
    if isinstance(value, str):
        return drop_sensitive(value)
    if isinstance(value, dict):
        for key in value:
            if isinstance(key, str) and _names_a_key(key):
                raise UncheckableError
        return {
            key: drop_content(item, depth + 1)
            for key, item in value.items()
            if not is_identity_entry(item) and not is_content_entry(item)
        }
    if isinstance(value, list):
        return [
            drop_content(item, depth + 1)
            for item in value
            if not is_content_entry(item) and not is_identity_entry(item)
        ]
    return value


def is_content_entry(item: object) -> bool:
    """A list entry whose `key` field (any letter case, space or spelling) is a content name."""
    return isinstance(item, dict) and any(
        isinstance(field, str) and _key_name(field) == "key" and is_content(name) for field, name in item.items()
    )


def without_entry_record(record: dict[str, Any]) -> dict[str, Any]:
    """`{}` when the record is itself a content or identity entry (`{"key": "prompt", "value": ...}`): no
    parent holds it to drop it. Any other record as it is."""
    return {} if is_content_entry(record) or is_identity_entry(record) else record


def clean_record(record: dict[str, Any]) -> dict[str, Any]:
    """A capture record as `scrub_capture` would leave it: identity and content removed, and a
    top-level field that cannot be checked left out (counted in `_dropped`). The appender and the
    scrub run this one function, so what the appender writes is a fixed point of the scrub."""
    global _dropped  # noqa: PLW0603
    whole = without_entry_record(record)
    if whole is not record:  # a record that is itself an entry: nothing of it is written
        _dropped += 1
    base = drop_identity_keys(at.drop_identity(whole))
    out: dict[str, Any] = {}
    for key, value in base.items():
        try:
            out.update(drop_content({key: value}))
        except (UncheckableError, RecursionError):  # noqa: PERF203
            _dropped += 1
    return out


def _wrap_log_record(log_record: dict[str, Any]) -> dict[str, Any]:
    """The receiver's OTLP/HTTP-JSON payload around one log record."""
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": []},
                "scopeLogs": [{"scope": {"name": "claude-observer-plugin"}, "logRecords": [log_record]}],
            }
        ]
    }


def _depth_of(node: Any, target: object, depth: int = 0) -> int | None:
    """The depth of `target` (by identity) in nested dicts and lists, `node` being at `depth`."""
    if node is target:
        return depth
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    return next((found for child in children if (found := _depth_of(child, target, depth + 1)) is not None), None)


def _attribute_value_depth() -> int:
    """How deep `drop_content` sees an attribute's AnyValue inside the wrapped record, whose own fields
    are at depth 1: found in the structure `otel_to_receiver_record` builds, not counted by hand."""
    marker: dict[str, Any] = {}
    depth = _depth_of({"payload": _wrap_log_record({"attributes": [{"key": "k", "value": marker}]})}, marker)
    assert depth is not None  # noqa: S101 - import-time invariant of the structure built above
    return depth


_ATTRIBUTE_VALUE_DEPTH = _attribute_value_depth()


def _clean_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Top-level attributes without content or identity names; nested values cleaned
    by `drop_sensitive`, and an attribute too deep to check left out. Too deep also means too deep once
    wrapped: each list level becomes three levels of the record (`arrayValue`, `values`, the list), and
    the record is checked to MAX_RECORD_DEPTH, which would otherwise take the whole payload with it."""
    global _dropped  # noqa: PLW0603
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if is_content(key) or is_identity(key):
            continue
        try:
            # A name that is itself text using a sensitive name as a key (`note prompt=x`) would make
            # `drop_content` refuse the whole wrapped payload: drop just this attribute.
            if _names_a_key(key):
                _dropped += 1
                continue
            clean = drop_sensitive(value)
            drop_content(_any_value(clean), _ATTRIBUTE_VALUE_DEPTH)  # raises before the attribute is kept
        except UncheckableError:
            _dropped += 1
            continue
        out[key] = clean
    return out


def _any_value(value: Any) -> dict[str, Any]:
    """An OTLP AnyValue for a plain JSON value."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_any_value(item) for item in value]}}
    return {"stringValue": value if isinstance(value, str) else json.dumps(value)}


def drop_identity_keys(value: Any) -> Any:
    """`value` without identity keys at any depth, through dicts and lists."""
    if isinstance(value, dict):
        return {key: drop_identity_keys(item) for key, item in value.items() if not is_identity(key)}
    if isinstance(value, list):
        return [drop_identity_keys(item) for item in value]
    return value


def otel_to_receiver_record(record: dict[str, Any]) -> dict[str, Any]:
    """Wrap a mirrored OTel event in the shape the old receiver wrote for /v1/logs."""
    attributes = record.get("attributes")
    # Before any value is serialized to text, where a key can no longer be seen.
    attributes = _clean_attributes(attributes) if isinstance(attributes, dict) else {}
    event = str(record.get("event") or attributes.get("event.name") or "unknown")
    log_record = {
        "body": {"stringValue": f"claude_code.{event}"},
        "attributes": [{"key": key, "value": _any_value(value)} for key, value in attributes.items()],
    }
    payload = _wrap_log_record(log_record)
    wrapped = {
        "ts": record.get("ts") or datetime.now(timezone.utc).isoformat(),
        "path": "/v1/logs",
        "source": "mod",
        "payload": at.drop_identity(payload),
    }
    # The plugin's repeat marker survives the wrap, so readers can drop a re-sent batch.
    if "run" in record and "seq" in record:
        wrapped["run"], wrapped["seq"] = record["run"], record["seq"]
    return wrapped


def append_batch(
    capture: Path,
    batch: str,
    max_bytes: int = at.DEFAULT_MAX_BYTES,
    backups: int = at.DEFAULT_BACKUPS,
    retention_days: int = at.DEFAULT_RETENTION_DAYS,
    exclude_budget: float = capture_exclude.ENSURE_BUDGET_SECONDS,
) -> int:
    """Append every valid line of `batch` in one write; return how many. `exclude_budget` is the seconds the
    git-exclude step may take afterwards (the final flush passes what its append timeout leaves)."""
    out: list[str] = []
    skipped = 0
    dropped_before = _dropped
    for line in batch.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                skipped += 1
                continue
            if record.get("kind") == "otel":
                record = otel_to_receiver_record(record)
            out.append(json.dumps(clean_record(record)) + "\n")
        except (ValueError, RecursionError):  # not JSON, or nested past the recursion limit
            skipped += 1
            continue
    if skipped:
        print(f"mod_capture_append: skipped {skipped} bad line(s)", file=sys.stderr)
    if _dropped > dropped_before:
        print(
            f"mod_capture_append: left out {_dropped - dropped_before} value(s) that could not be checked or are themselves a sensitive entry",
            file=sys.stderr,
        )
    if out:
        at.append_record(capture, "".join(out), max_bytes, backups, retention_days)
        # After the write, so a slow or failing git never costs a batch; the warning (stderr, one line)
        # is shown once by the plugin. Keeps the capture out of the user's `git add`.
        warning = capture_exclude.ensure_excluded(capture, budget=exclude_budget)
        if warning:
            print(f"mod_capture_append: {warning}", file=sys.stderr)
    return len(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append a batch of claude-observer plugin records to its capture.")
    parser.add_argument("--out", default=str(DEFAULT_MOD_CAPTURE), help="capture file")
    parser.add_argument("--max-bytes", type=int, default=at.DEFAULT_MAX_BYTES)
    parser.add_argument("--retention-days", type=int, default=at.DEFAULT_RETENTION_DAYS)
    parser.add_argument("--backups", type=int, default=at.DEFAULT_BACKUPS)
    parser.add_argument(
        "--exclude-budget",
        type=capture_exclude.parse_budget,
        default=capture_exclude.ENSURE_BUDGET_SECONDS,
        help="seconds the git-exclude step may take after the write (0 skips it)",
    )
    args = parser.parse_args(argv)
    append_batch(
        Path(args.out), sys.stdin.read(), args.max_bytes, args.backups, args.retention_days, args.exclude_budget
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
