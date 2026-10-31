"""Shared transcript-iteration module.

Walks `~/.claude/projects/<project>/**/*.jsonl` -- main-session transcripts
and their `subagents/*.jsonl` children, each subagent transcript paired with
its `.meta.json` sibling -- and yields parsed, message-id-deduplicated
records. This module is deliberately navigation-agnostic: it exposes
`ToolUseEvent`/`ToolResultEvent` records, not counts, so any report (such as
`scripts/agent_usage_report.py`'s per-ticket/role/model cost rollup) can
reuse it without inheriting another report's counting rules.

Message-id deduplication matters because Claude Code writes one assistant
message as several JSONL lines sharing one `message.id` as it streams --
**empirically, always one new content block per line, never a line that is
a superset of the previous one** (confirmed against this checkout's own
`~/.claude/projects/.../*.jsonl` transcripts while fixing an undercount: every multi-line message observed there carries
exactly one content block per line, and no two lines for the same id ever
repeat an identical block). An earlier version of this module assumed the
opposite -- that later lines were a growing superset -- and kept only the
last line per id, silently dropping every block that line did not happen to
repeat (the reported bug: an LSP call sharing a message with a later Bash
call vanished because only the Bash-only last line survived). This module
now merges content blocks across *all* lines for a given message id, in
file order, deduplicating identical blocks (same `tool_use.id`, or identical
block content for anything else) -- so it is correct whether a transcript's
streaming shape is "one block per line" (the only shape seen so far) or a
genuine growing-superset shape (never observed here, but a single dedup-by-
identity merge handles both without special-casing either). `usage` is
still counted once per message id, taken from that id's last line.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import stat
from typing import TYPE_CHECKING, Any, Literal

from safe_name import clean_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

TranscriptKind = Literal["main", "subagent"]

# A `.meta.json` over this is ignored: a real one holds a type, a model and a description.
META_MAX_BYTES = 64 * 1024


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


def _read_meta(path: Path) -> dict[str, Any]:
    """The `.meta.json` object, or {} for a missing, symlinked, non-regular, oversized, unreadable, invalid or
    non-object file."""
    fd = _open_regular(path)
    if fd is None:
        return {}
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(META_MAX_BYTES + 1)
        if len(raw) > META_MAX_BYTES:
            return {}
        meta = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):  # unreadable, invalid UTF-8, invalid or too deeply nested JSON
        return {}
    return meta if isinstance(meta, dict) else {}


@dataclasses.dataclass(frozen=True)
class TranscriptContext:
    """Identifies one transcript file and, for a subagent transcript, the
    subagent/model recorded in its paired `.meta.json`."""

    transcript_path: Path
    session_id: str
    kind: TranscriptKind
    agent_type: str | None
    meta_model: str | None


@dataclasses.dataclass(frozen=True)
class AssistantMessageEvent:
    """One per deduplicated assistant message -- emitted regardless of
    whether the message contains any tool call, so a per-dispatch token
    total (the spec's "per dispatch, total tokens" counter) can be summed
    once per message rather than once per tool_use block within it."""

    context: TranscriptContext
    message_id: str
    timestamp: str | None
    model: str | None
    usage: Mapping[str, object] | None
    request_id: str | None = None


@dataclasses.dataclass(frozen=True)
class ToolUseEvent:
    context: TranscriptContext
    message_id: str
    timestamp: str | None
    model: str | None
    tool_use_id: str
    tool_name: str
    tool_input: Mapping[str, object]


@dataclasses.dataclass(frozen=True)
class ToolResultEvent:
    context: TranscriptContext
    tool_use_id: str
    is_error: bool
    result_text: str | None


TranscriptEvent = AssistantMessageEvent | ToolUseEvent | ToolResultEvent


def discover_transcripts(project_dir: Path, *, skip_links: bool = True) -> tuple[TranscriptContext, ...]:
    """Every main-session `<uuid>.jsonl` directly under `project_dir`, plus
    every `<uuid>/subagents/agent-*.jsonl` (paired with its `.meta.json`,
    when present). A symlinked transcript, session directory or `subagents` directory is left out and a symlinked
    `.meta.json` is ignored. `skip_links=False` lists the links too, for a caller (the quota
    reports) that counts them as skipped itself; reading one through `read_jsonl` still yields nothing."""
    contexts: list[TranscriptContext] = []
    if not project_dir.is_dir():
        return ()

    for path in sorted(project_dir.glob("*.jsonl")):
        if skip_links and path.is_symlink():
            continue
        session_id = path.stem
        contexts.append(
            TranscriptContext(
                transcript_path=path,
                session_id=session_id,
                kind="main",
                agent_type=None,
                meta_model=None,
            )
        )

    for subagents_dir in sorted(project_dir.glob("*/subagents")):
        if skip_links and (subagents_dir.parent.is_symlink() or subagents_dir.is_symlink()):
            continue
        session_id = subagents_dir.parent.name
        for path in sorted(subagents_dir.glob("agent-*.jsonl")):
            if skip_links and path.is_symlink():
                continue
            meta_path = path.with_suffix("").with_suffix(".meta.json")
            meta = _read_meta(meta_path)
            agent_type = clean_name(meta.get("agentType"))  # hostile text: cleaned for every report
            meta_model = meta.get("model")
            contexts.append(
                TranscriptContext(
                    transcript_path=path,
                    session_id=session_id,
                    kind="subagent",
                    agent_type=agent_type,
                    meta_model=meta_model,
                )
            )
    return tuple(contexts)


def read_jsonl(path: Path) -> Iterator[dict[str, object]]:
    """The JSON objects of a JSONL file. A symlink or a non-regular file (a FIFO, a device) reads as empty. Lines and total size are not capped here: dropping a long line would silently change every
    report's totals."""
    fd = _open_regular(path)
    if fd is None:
        return
    try:
        with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except (ValueError, RecursionError):  # invalid or too deeply nested JSON
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:
        return


def _collect_records(
    transcript_path: Path,
) -> tuple[dict[str, list[dict[str, object]]], list[str], list[dict[str, object]]]:
    """One pass over the raw JSONL: every raw line seen for each assistant
    message (by id, in first-seen order, each id's own lines kept in file
    order) and every `tool_result` content block from `user`-role lines."""
    records_by_id: dict[str, list[dict[str, object]]] = {}
    assistant_order: list[str] = []
    tool_results: list[dict[str, object]] = []

    for record in read_jsonl(transcript_path):
        if record.get("type") == "assistant":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str):
                continue
            if message_id not in records_by_id:
                assistant_order.append(message_id)
                records_by_id[message_id] = []
            records_by_id[message_id].append(record)
        elif record.get("type") == "user":
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                tool_results.extend(
                    block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"
                )

    return records_by_id, assistant_order, tool_results


def _block_dedup_key(block: dict[str, object]) -> tuple[str, str]:
    """Identifies a content block for merge-dedup purposes: a `tool_use`
    block by its own id (stable across the lines that repeat it), anything
    else by its exact serialized content (covers a `thinking`/`text` block
    that a genuinely cumulative transcript might repeat verbatim)."""
    block_id = block.get("id")
    if block.get("type") == "tool_use" and isinstance(block_id, str):
        return ("tool_use", block_id)
    return ("content", json.dumps(block, sort_keys=True))


def _merge_content_blocks(
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Merges every line's content blocks for one message id, in file
    order, skipping a block already seen under an earlier line's key --
    correct whether each line contributes exactly one new block (the only
    shape observed in real transcripts) or a later line repeats every block
    an earlier one already had (a genuine growing superset, never observed
    here but handled the same way)."""
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, object]] = []
    for record in records:
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            key = _block_dedup_key(block)
            if key in seen:
                continue
            seen.add(key)
            merged.append(block)
    return merged


def _assistant_message_events(
    context: TranscriptContext,
    records_by_id: dict[str, list[dict[str, object]]],
    assistant_order: list[str],
) -> Iterator[AssistantMessageEvent | ToolUseEvent]:
    for message_id in assistant_order:
        records = records_by_id[message_id]
        last_record = records[-1]
        last_message = last_record["message"]
        assert isinstance(last_message, dict)  # noqa: S101  (narrows the type; the record shape is checked upstream)
        timestamp = last_record.get("timestamp")
        model = last_message.get("model")
        usage = last_message.get("usage")
        request_id = last_record.get("requestId")
        timestamp_str = timestamp if isinstance(timestamp, str) else None
        model_str = model if isinstance(model, str) else None

        yield AssistantMessageEvent(
            context=context,
            message_id=message_id,
            timestamp=timestamp_str,
            model=model_str,
            usage=usage if isinstance(usage, dict) else None,
            request_id=request_id if isinstance(request_id, str) else None,
        )

        for block in _merge_content_blocks(records):
            if block.get("type") == "tool_use":
                tool_use_id = block.get("id")
                tool_name = block.get("name")
                tool_input = block.get("input")
                if not isinstance(tool_use_id, str) or not isinstance(tool_name, str):
                    continue
                yield ToolUseEvent(
                    context=context,
                    message_id=message_id,
                    timestamp=timestamp_str,
                    model=model_str,
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    tool_input=tool_input if isinstance(tool_input, dict) else {},
                )


def _result_text_of(block: dict[str, object]) -> str | None:
    """The plain-text form of a `tool_result` block's `content`, when there
    is one -- a bare string, or the concatenated `text` fields of a list of
    typed content blocks. Used only for classification (e.g. an LSP
    plain-text failure message), never surfaced verbatim in a report."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    texts.append(text)
        if texts:
            return "\n".join(texts)
    return None


def _tool_result_events(context: TranscriptContext, tool_results: list[dict[str, object]]) -> Iterator[ToolResultEvent]:
    seen_result_ids: set[str] = set()
    for block in tool_results:
        tool_use_id = block.get("tool_use_id")
        if not isinstance(tool_use_id, str) or tool_use_id in seen_result_ids:
            continue
        seen_result_ids.add(tool_use_id)
        yield ToolResultEvent(
            context=context,
            tool_use_id=tool_use_id,
            is_error=bool(block.get("is_error", False)),
            result_text=_result_text_of(block),
        )


def iter_events(context: TranscriptContext) -> Iterator[TranscriptEvent]:
    """Parses one transcript file, deduplicating assistant messages by id
    (merging every line's content blocks per id, see the module docstring),
    and yields one `AssistantMessageEvent` per deduplicated message, one
    `ToolUseEvent` per merged `tool_use` content block within it, and one
    `ToolResultEvent` per `tool_result` content block -- in that order,
    across the deduplicated messages."""
    records_by_id, assistant_order, tool_results = _collect_records(context.transcript_path)
    yield from _assistant_message_events(context, records_by_id, assistant_order)
    yield from _tool_result_events(context, tool_results)


def iter_project_events(project_dir: Path) -> Iterator[TranscriptEvent]:
    """The full walk: every discovered transcript's deduplicated events, in
    transcript-discovery order."""
    for context in discover_transcripts(project_dir):
        yield from iter_events(context)


# Largest token count a copy may report and still rank: the numeric readers' own cap
# (`agent_tool_report.MAX_NUMBER`, pinned equal by a test), so a count a report would add is a count that ranks.
MAX_TOKENS = 10**15
_PROMPT_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _token_field(usage: object, field: str) -> int:
    """A usable token count of one usage field, else 0 (a missing, non-numeric, boolean, negative,
    non-finite or over-`MAX_TOKENS` count ranks below any real one)."""
    value = usage.get(field) if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value) or value < 0 or value > MAX_TOKENS:  # NaN, infinite, negative, huge
        return 0
    return int(value)


def _copy_rank(
    event: AssistantMessageEvent, index: int, usable: Callable[[AssistantMessageEvent], bool] | None
) -> tuple[bool, int, int, int]:
    """How good a copy is, larger is better: a copy the caller can use beats one it cannot, then the
    larger `output_tokens` (the partial streamed copy reports fewer), then the larger prompt (input +
    cache read + cache write: a copy cut short before its usage settled reports less of those too),
    then the later file."""
    prompt = sum(_token_field(event.usage, field) for field in _PROMPT_FIELDS)
    return (True if usable is None else usable(event), _token_field(event.usage, "output_tokens"), prompt, index)


def claim_owners(
    per_file: Iterable[Iterable[AssistantMessageEvent]],
    usable: Callable[[AssistantMessageEvent], bool] | None = None,
) -> dict[tuple[str, str], int]:
    """First pass of the cross-file dedupe: key (`message_id`, `request_id`) -> index of the file
    holding its best copy. Holds only the keys, never the events, so the files may be a lazy
    iterable read one at a time. An event without a `request_id` claims nothing."""
    best: dict[tuple[str, str], tuple[bool, int, int, int]] = {}
    for index, events in enumerate(per_file):
        for event in events:
            if event.request_id is None:
                continue
            key = (event.message_id, event.request_id)
            rank = _copy_rank(event, index, usable)
            if key not in best or rank > best[key]:  # ranks never tie: `index` differs per file
                best[key] = rank
    return {key: rank[3] for key, rank in best.items()}


def _owned(
    events: Iterable[AssistantMessageEvent], index: int, owners: Mapping[tuple[str, str], int]
) -> tuple[AssistantMessageEvent, ...]:
    return tuple(
        event
        for event in events
        if event.request_id is None or owners.get((event.message_id, event.request_id)) == index
    )


def dedupe_across_files(
    per_file: Sequence[tuple[TranscriptContext, Sequence[AssistantMessageEvent]]],
    usable: Callable[[AssistantMessageEvent], bool] | None = None,
) -> list[tuple[TranscriptContext, tuple[AssistantMessageEvent, ...]]]:
    """Counts each response once across files: a fork, a resumed session or a
    worktree sibling folder repeats messages another file already holds.

    A response is keyed by (`message_id`, `request_id`). An event whose `request_id` is missing is
    never deduped: a bare id proves nothing (distinct sessions reuse ids in fixtures). Among the
    files passed in, the key is owned by the file holding the best copy (`_copy_rank`: a copy
    `usable` accepts, then the largest `output_tokens`, then the largest prompt, then the latest
    file in the given order). Every other file's copy is dropped; the owner and all keyless events
    stay. `per_file` is already deduplicated within each file, so a file holds a key at most once.
    The caller decides which files take part, so a file it filtered out (a `--since` window)
    never claims anything. Returns the same files, in order, with their kept events."""
    owners = claim_owners((events for _, events in per_file), usable)
    return [(context, _owned(events, index, owners)) for index, (context, events) in enumerate(per_file)]


def stream_owned_across_files(
    contexts: Sequence[TranscriptContext],
    usable: Callable[[AssistantMessageEvent], bool] | None = None,
) -> Iterator[tuple[TranscriptContext, tuple[AssistantMessageEvent, ...]]]:
    """`dedupe_across_files` over files read twice instead of held at once: a first pass keeps only
    the key -> owner table, a second yields each file's owned events. Peak memory is one file's
    events plus the table. The same files, in order. A file that changes between the passes may
    lose or repeat a response in this one run; the next run reads it settled."""
    owners = claim_owners((_assistant_events(context) for context in contexts), usable)
    for index, context in enumerate(contexts):
        yield context, _owned(_assistant_events(context), index, owners)


def _assistant_events(context: TranscriptContext) -> Iterator[AssistantMessageEvent]:
    return (event for event in iter_events(context) if isinstance(event, AssistantMessageEvent))
