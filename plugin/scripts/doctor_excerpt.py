"""The doctor's bug-report excerpt pipeline.

    build_excerpt(capture, home, lines=10, out=None, keep=False, checks=(), content_check=...)

1. A private work directory (`tempfile.mkdtemp`, mode 0700, owned by us, outside the project).
2. A bounded copy of the live capture's tail into a 0600 file there: the last `MAX_COPY_LINES` lines of at most the
   last `MAX_COPY_BYTES` bytes, from a line boundary (a torn first line is cut off, not a finding). The live file is
   opened read-only and never written, renamed or listed beside. The range is fixed by one `fstat`; it is read
   twice and the two reads must be equal, and afterwards the path must still name the same file (device and inode)
   that is not smaller: a rotation, a scrub or a truncation during the copy refuses (the scrub's own size check
   runs on the COPY, which nobody writes, so it cannot guard the live file; this does).
3. `scrub_capture` on the copy through its API. Any exception or any unscrubbed line refuses; blanked (torn) lines
   are reported.
4. The content check on the scrubbed copy; any result but `ok` refuses.
5. The allowlist projection (`excerpt_project`): the excerpt holds no free text, by construction (see there).
6. `doctor-excerpt.md`, mode 0600, written under a temporary name, synced, then linked into place without
   clobbering (`--out` never overwrites). Printed text is the path, never the excerpt, unless `--show`.
7. The work directory is removed on every exit path (an exception, SIGTERM, SIGHUP) unless `--keep` AND the run
   succeeded; a refused run never keeps a copy. The pseudonym map is never written.

Every refusal is an `ExcerptRefused` with a level and a reason made of constants and counts, never a value read
from the capture and never an exception's message.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import excerpt_project as ep
import scrub_capture as sc

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import FrameType

MAX_COPY_LINES = 2000
MAX_COPY_BYTES = 4 * 1024 * 1024
COPY_NAME = "capture-copy.jsonl"
EXCERPT_NAME = "doctor-excerpt.md"
WORK_PREFIX = "usage-observer-doctor-work-"
OUT_PREFIX = "usage-observer-doctor-"
STALE_SECONDS = 24 * 60 * 60
OURS = re.compile(re.escape(OUT_PREFIX) + r"(work-)?[a-z0-9_]{8}", re.ASCII)  # tempfile.mkdtemp's own suffix
READ_CHUNK = 1024 * 1024
DEFAULT_LINES = 10
PRINTABLE = re.compile(r"[\x20-\x7e\n]*", re.ASCII)
LINE_OK = re.compile(r"[\x20-\x7e]*", re.ASCII)

CAVEATS = (
    (
        "The scrub finds key-style spellings only. Known gaps: base64 and other encodings, a name followed by a tab, "
        "camelCase, XML-style tags, typographic quotes and colon look-alikes. Free text is not understood."
    ),
    (
        "This excerpt is built from a fixed list of fields (numbers, booleans, fixed sets, model and version strings "
        "that matched a pattern, and numbered stand-ins such as skill-1 for names). That is a design, not a guarantee."
    ),
    (
        "OpenTelemetry mirror records (the per-request events) are dropped whole, so the excerpt has no per-request "
        "events. Records of a kind this version does not list are dropped and counted."
    ),
    "Time values are cut to the minute. The stand-in numbers are valid within this file only.",
    "Read the excerpt before you paste it.",
)


class ExcerptRefused(Exception):  # noqa: N818  (a refusal, not an error: the name is the contract)
    """No excerpt. `level` is `fail` (the content is not acceptable), `unknown` (the capture could not be read or
    changed under us) or `info` (nothing to excerpt). `reason` holds constants and counts only."""

    def __init__(self, level: str, reason: str) -> None:
        super().__init__(reason)
        self.level = level
        self.reason = reason


@dataclass(frozen=True)
class Built:
    path: Path
    text: str
    kept_dir: Path | None  # the work directory, only with --keep
    note: str  # counts of what was copied and dropped; constants and numbers only


# ---- private directories ------------------------------------------------------------------------------------------


def _inside(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:  # another drive, or a mix of absolute and relative
        return False


def make_private_dir(home: Path, prefix: str) -> Path:
    """A new directory, 0700, ours, whose real path is outside the project. Removed again if any check fails."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise ExcerptRefused("fail", "the temporary directory is not a directory")  # noqa: TRY301
        if os.name == "posix" and (stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid()):
            raise ExcerptRefused("fail", "the temporary directory is not private (mode 0700, ours)")  # noqa: TRY301
        if _inside(os.path.realpath(path), os.path.realpath(home)):
            raise ExcerptRefused("fail", "the temporary directory is inside the project: set TMPDIR elsewhere")  # noqa: TRY301
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def sweep_stale(now: float | None = None) -> int:
    """Remove our own leftovers in the temp directory older than a day: `usage-observer-doctor-work-*` (a killed run's
    unprojected copy) and `usage-observer-doctor-*` (earlier excerpt directories and kept work directories). Only a
    real directory (never a link) with exactly our name shape, owned by this user. Returns how many were removed;
    never raises."""
    removed = 0
    cutoff = (time.time() if now is None else now) - STALE_SECONDS
    try:
        entries = list(os.scandir(tempfile.gettempdir()))
    except OSError:
        return 0
    for entry in entries:
        if not OURS.fullmatch(entry.name):
            continue
        try:
            info = os.lstat(entry.path)
            if not stat.S_ISDIR(info.st_mode) or info.st_mtime >= cutoff:
                continue
            if os.name == "posix" and info.st_uid != os.getuid():
                continue
            shutil.rmtree(entry.path)  # refuses a link at the top; never follows one inside
            removed += 1
        except OSError:
            continue
    return removed


def open_work_dir(path: Path) -> int:
    """A descriptor for the work directory just made: every later create and remove goes through it, so a rename and a
    link put in the directory's place cannot redirect a write. Refuses anything but a real directory of ours."""
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and info.st_uid != os.getuid()):
            raise ExcerptRefused("fail", "the temporary directory is not ours")  # noqa: TRY301
    except BaseException:
        os.close(fd)
        raise
    return fd


def same_dir(path: Path, fd: int) -> bool:
    """Whether `path` still names the directory `fd` holds (same device and inode; a link never does)."""
    try:
        now = os.lstat(path)
        held = os.fstat(fd)
    except OSError:
        return False
    return (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino)  # lstat: a link has an inode of its own


def _remove_work(path: Path, fd: int) -> bool:
    """Remove the work directory's files through the descriptor, then the directory by its name only if the name still
    is that directory. False when something is left: the directory itself (renamed away or replaced) or a file."""
    ok = True
    try:
        for name in os.listdir(fd):
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:  # noqa: PERF203
                ok = False
    except OSError:
        return False
    if not same_dir(path, fd):
        return False
    try:
        path.rmdir()
    except OSError:
        return False
    return ok


def _remove_tree(path: Path | None) -> bool:
    """Remove a directory this run made; False when it could not be removed. A directory that is already gone is
    removed."""
    if path is None:
        return True
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


# ---- the bounded, checked copy -----------------------------------------------------------------------------------


def _read_range(fd: int, start: int, end: int) -> bytes:
    chunks: list[bytes] = []
    at = start
    while at < end:
        chunk = os.pread(fd, min(READ_CHUNK, end - at), at)
        if not chunk:
            raise ExcerptRefused("unknown", "the capture got shorter while it was copied: run the doctor again")
        chunks.append(chunk)
        at += len(chunk)
    return b"".join(chunks)


def read_tail(capture: Path, on_read: Callable[[], None] | None = None) -> bytes:
    """The capture's last lines, from a line boundary, as one bytes object (see the module doc for the checks)."""
    try:
        fd = os.open(capture, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise ExcerptRefused("info", "there is no capture file, so no excerpt") from None
    except OSError as error:
        raise ExcerptRefused(
            "unknown", f"the capture could not be opened ({errno.errorcode.get(error.errno or 0)})"
        ) from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ExcerptRefused("fail", "the capture is not a regular file")
        end = before.st_size
        start = max(0, end - MAX_COPY_BYTES)
        window = start - 1 if start else 0  # one byte back: was the cut exactly at a line boundary?
        try:
            first = _read_range(fd, window, end)
            if on_read is not None:
                on_read()
            second = _read_range(fd, window, end)
            after = os.fstat(fd)
            path_now = os.stat(capture)  # noqa: PTH116  (follows links, like the open above)
        except FileNotFoundError:
            raise ExcerptRefused("unknown", "the capture vanished while it was copied: run the doctor again") from None
        except OSError as error:
            raise ExcerptRefused(
                "unknown", f"the capture could not be read ({errno.errorcode.get(error.errno or 0)})"
            ) from None
    finally:
        os.close(fd)
    same_file = (path_now.st_dev, path_now.st_ino) == (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    if not same_file or after.st_size < end or first != second:
        raise ExcerptRefused("unknown", "the capture changed while it was copied: run the doctor again")
    data = first
    if start:
        # Dropping the partial line the window opens in, unless the byte before it was a newline.
        data = data[1:] if data[:1] == b"\n" else data[data.find(b"\n") + 1 :] if b"\n" in data else b""
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return b"".join(line + b"\n" for line in lines[-MAX_COPY_LINES:])


def _write_new(dir_fd: int, name: str, data: bytes) -> None:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=dir_fd,
    )
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _check_copy(path: Path) -> None:
    """The scrubbed copy must still be one regular file of ours with mode 0600 (a scrub replaces it)."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600):
        raise ExcerptRefused("fail", "the copy is not a private regular file")


def _read_copy(path: Path) -> list[bytes]:
    with path.open("rb") as handle:
        data = handle.read(MAX_COPY_BYTES + 2 * READ_CHUNK)
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


# ---- rendering --------------------------------------------------------------------------------------------------


def _line_text(text: str) -> str:
    """One check line made safe for the file: ASCII only, no backtick (the fence), else withheld."""
    flat = " ".join(text.replace("`", "'").split())
    return flat if LINE_OK.fullmatch(flat) else "(line withheld: it held characters outside printable ASCII)"


def _drops(projection: ep.Projection) -> str:
    return (
        f"dropped whole, never shown: otel mirror records {projection.otel}; records of a kind this version does not "
        f"list {projection.unknown_kind}; lines that are not a JSON object {projection.unreadable}; "
        f"lines over {ep.MAX_LINE} bytes {projection.oversize}"
    )


def render(
    projection: ep.Projection,
    scrub_report: str,
    window_lines: int,
    checks: Sequence[tuple[str, str, str]],
    python_version: str,
    platform: str,
) -> str:
    kinds = ", ".join(f"{kind} {n}" for kind, n in sorted(projection.counts.items())) or "none"
    body = [
        (
            f"copy: the last {window_lines} lines of the capture (at most {MAX_COPY_LINES} lines or "
            f"{MAX_COPY_BYTES} bytes)"
        ),
        f"records by kind: {kinds}",
        _drops(projection),
        f"the last {len(projection.records)} records of a known kind, projected:",
    ]
    body += [
        json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) for record in projection.records
    ]
    lines = [
        "# usage-observer doctor excerpt",
        "",
        "Read this file before you paste it anywhere.",
        "",
        "## Environment",
        "",
        f"- Python running the doctor: {python_version}",
        f"- Platform: {platform}",
        "",
        "## Scrub report",
        "",
        "```text",
        _line_text(scrub_report),
        "```",
        "",
        "## Checks",
        "",
        "```text",
        *(_line_text(f"[{level}] {label}: {text}") for label, level, text in checks),
        "```",
        "",
        "## Excerpt",
        "",
        "```text",
        *body,
        "```",
        "",
        "## Caveats",
        "",
        *(f"- {caveat}" for caveat in CAVEATS),
        "",
    ]
    text = "\n".join(lines)
    if not PRINTABLE.fullmatch(text):
        raise ExcerptRefused("fail", "the excerpt held a character outside printable ASCII")
    return text


def write_excerpt(dest: Path, text: str) -> None:
    """Write `text` to `dest` (0600) through a temporary file in the same directory, then link it into place: an
    existing `dest`, a link or a directory included, is never replaced."""
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".doctor-excerpt-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, dest)
        except FileExistsError:
            raise ExcerptRefused("fail", "the output file exists: the doctor never overwrites a file") from None
        except OSError as error:
            if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV, errno.EMLINK, errno.ENOSYS):
                raise
            if os.path.lexists(dest):  # no hard links here: a check and a rename, which a race can beat
                raise ExcerptRefused("fail", "the output file exists: the doctor never overwrites a file") from None
            tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        dir_fd = os.open(dest.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


# ---- signals ----------------------------------------------------------------------------------------------------


def _terminate(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)  # the `finally` of build_excerpt removes the directories


@contextlib.contextmanager
def _exit_on_signals() -> Any:
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, _terminate)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


# ---- the pipeline ------------------------------------------------------------------------------------------------


def _report_left(done: bool, out_dir: Path | None) -> None:
    """The work directory could not be removed: say so. Inside a refusal the reason grows; after a success the run
    becomes a refusal (and takes its own excerpt directory with it), because an unprojected copy may be left."""
    note = "the temporary work directory could not be removed (it was renamed away or is stuck)"
    pending = sys.exc_info()[1]
    if isinstance(pending, ExcerptRefused):
        pending.reason = f"{pending.reason}; {note}"
        pending.args = (pending.reason,)
    elif pending is None:
        if done:
            _remove_tree(out_dir)
        raise ExcerptRefused("fail", note)


def build_excerpt(
    capture: Path,
    home: Path,
    *,
    content_check: Callable[[Path], Any],
    lines: int = DEFAULT_LINES,
    out: Path | None = None,
    keep: bool = False,
    checks: Sequence[tuple[str, str, str]] = (),
    on_read: Callable[[], None] | None = None,
) -> Built:
    """Build the excerpt or raise `ExcerptRefused`. `content_check(copy)` returns an object with `.level` and
    `.text` (`doctor_checks.check_content`); `checks` are (label, level, text) lines for the file."""
    if out is not None and not Path(os.path.abspath(out)).parent.is_dir():  # noqa: PTH100
        raise ExcerptRefused("fail", "the directory of the output file does not exist")
    sweep_stale()
    work: Path | None = None
    work_fd: int | None = None
    out_dir: Path | None = None
    done = False

    def guard() -> None:
        if work is None or work_fd is None or not same_dir(work, work_fd):
            raise ExcerptRefused("fail", "the work directory was replaced while the doctor ran, so no excerpt")

    with _exit_on_signals():
        try:
            work = make_private_dir(home, WORK_PREFIX)
            work_fd = open_work_dir(work)
            copy = work / COPY_NAME
            data = read_tail(capture, on_read)
            copied = data.count(b"\n")
            if copied == 0:
                raise ExcerptRefused("info", "the capture's tail holds no complete line, so no excerpt")
            _write_new(work_fd, COPY_NAME, data)
            guard()
            try:
                (result,) = sc.scrub_files([copy])
            except Exception as error:  # any scrub failure: no excerpt, the type name only
                raise ExcerptRefused("fail", f"the scrub of the copy failed ({type(error).__name__})") from None
            guard()
            if result.unscrubbed:
                raise ExcerptRefused("fail", f"the scrub left {result.unscrubbed} lines unscrubbed, so no excerpt")
            _check_copy(copy)
            verdict = content_check(copy)
            guard()
            if verdict.level != "ok":
                raise ExcerptRefused(
                    "fail", f"the content check of the scrubbed copy was not clean ({verdict.level}), so no excerpt"
                )
            try:
                projection = ep.project_lines(_read_copy(copy), min(max(lines, 1), ep.MAX_KEEP))
            except ep.ProjectionRefused:  # the net: a key or a string outside the allowlist, so no excerpt at all
                raise ExcerptRefused("fail", "the projection's own check refused a record, so no excerpt") from None
            text = render(
                projection,
                sc.describe(COPY_NAME, result),
                copied,
                checks,
                ".".join(map(str, sys.version_info[:3])),
                sys.platform,
            )
            if out is None:
                out_dir = make_private_dir(home, OUT_PREFIX)
                dest = out_dir / EXCERPT_NAME
            else:
                dest = Path(os.path.abspath(out))  # noqa: PTH100
            write_excerpt(dest, text)
            done = True
            note = (
                f"{copied} lines copied, {result.blanked} blanked by the scrub; dropped whole: otel {projection.otel}, "
                f"unknown kind {projection.unknown_kind}, not objects {projection.unreadable}, "
                f"oversize {projection.oversize}"
            )
            return Built(dest, text, work if keep else None, note)
        finally:
            if not done:
                _remove_tree(out_dir)
            left = False
            if work_fd is not None and work is not None:
                if not (done and keep):
                    left = not _remove_work(work, work_fd)
                os.close(work_fd)
            elif not (done and keep):
                left = not _remove_tree(work)
            if left:
                _report_left(done, out_dir)
