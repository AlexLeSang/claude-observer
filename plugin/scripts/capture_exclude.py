"""Keep the usage-observer capture out of the user's git.

The plugin writes `logs/agent-telemetry.mod.jsonl` into the user's own project. Without this, `git add -A`
commits models, costs, tool names, file paths and session ids. After each append, `ensure_excluded` asks git
whether the capture, its rotations and its sidecars are ignored; when one is not, it adds anchored lines to the
repository's `info/exclude`: local, never committed, no tracked file touched.

- `git rev-parse --git-path info/exclude` names the file, so a linked worktree writes the COMMON git dir's
  copy and a `.git` that is a pointer file (worktree, submodule) works as git resolves it.
- The capture's directory is resolved through symlinks first: a capture that really lives outside the work
  tree (an absolute `capture` option, a symlinked `logs/`) is skipped, and so is a project that is not a git
  repo (or a bare one).
- Every line is anchored with a leading `/` and every glob character, space and backslash in the path is
  backslash-escaped, so a hostile directory name cannot widen the pattern. A path with a newline or other
  control byte is not written at all (it would inject lines): a warning instead.
- The lines also cover the leftovers of scrub_capture.py and split_capture.py (`.scrub-*`, `.splitting`, and the
  `.<N>` forms): they hold the capture's own records.
- `info/exclude` as a symlink is followed only when the real target (every link and `..` resolved) is a regular
  file or a missing name inside `<git dir>/info/` (a dotfile manager keeping the file next to it): a link to
  git's `config`, a hook, a tracked `.gitignore` or anything outside `.git` is a warning instead. The target is
  opened O_NOFOLLOW and checked again after the open, so a swap in between is refused too.
- Everything runs under one deadline, ENSURE_BUDGET_SECONDS (1 s) at most, after the batch is written. The
  final flush passes a smaller `budget` so the helper cannot outlast the plugin's append timeout.
- Never raises. The result is a warning string for a case the user should know about (read-only `.git`, a
  tracked capture (checked at every call), a pattern a `.gitignore` negation overrides), else None. The
  appender prints it on stderr and the plugin shows it once.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    import fcntl

    _HAS_FLOCK = True
except ImportError:  # pragma: no cover - Windows
    _HAS_FLOCK = False

from capture_paths import _GIT_ENV_DROPPED

WARNING_PREFIX = "git-exclude:"
GIT_TIMEOUT_SECONDS = 3
# One deadline for everything `ensure_excluded` does (git calls and the lock wait). The appender runs it after the
# batch is written, under the plugin's 10 s append timeout (shorter on the final flush): a timeout there makes the
# plugin requeue and rewrite a batch that is already on disk, so this stays far below it.
ENSURE_BUDGET_SECONDS = 1.0
# What the appender and the rotation write next to the capture (agent_telemetry.py): `.N` rotations, the flock
# sidecar, the rotation claim and its breaker guard.
SIDECAR_SUFFIXES = (b".lock", b".rotating", b".rotating.breaking")
# What an interrupted or failed run of scrub_capture.py (`<name>.scrub-*` temp, `<name>.scrub-late-*` saved tail) and
# split_capture.py (`<name>.splitting`) leaves: the capture's own records, so `git add -A` must not take them. The
# rotated file's version is `<name>.<N>.<suffix>`. Each ends in a fixed word, so `.[0-9]*` there hides no user note.
LEFTOVER_PATTERNS = (b".splitting", b".[0-9]*.splitting", b".scrub-*", b".[0-9]*.scrub-*")
# Rotation names are `<capture>.<N>`, N a plain number (agent_telemetry.rotated_captures); gitignore cannot say "digits
# only" for any length, so one pattern per length up to 4 digits. `.[0-9]*` would also hide `<capture>.1notes.md`.
ROTATION_PATTERNS = tuple(b"." + b"[0-9]" * n for n in range(1, 5))
# What `git check-ignore` is asked about: one representative of each pattern.
PROBE_SUFFIXES = (
    b"",
    b".1",
    *SIDECAR_SUFFIXES,
    b".splitting",
    b".1.splitting",
    b".scrub-x",
    b".1.scrub-x",
)
_COMMENT = b"# usage-observer plugin: local capture, never commit (added automatically)"
_GLOB_CHARS = frozenset(b"\\*?[] ")


def git(
    cwd: Path, args: list[str], deadline: float, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes] | None:
    """`git <args>` in `cwd` without the variables that redirect git; None when git is missing, hangs, or the
    `deadline` (time.monotonic) has passed."""
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    env = {key: value for key, value in os.environ.items() if key not in _GIT_ENV_DROPPED}
    # The repository's own config is untrusted (a clone the user did not write): `core.fsmonitor` makes
    # `git ls-files` run the program it names (measured, git 2.x), so it is switched off for every call. The
    # user's global and system config stay: `safe.directory` lives there and nothing in them is repository-
    # controlled. No optional lock: these are read-only questions.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            timeout=min(GIT_TIMEOUT_SECONDS, left),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _out(ran: subprocess.CompletedProcess[bytes] | None) -> bytes | None:
    if ran is None or ran.returncode != 0:
        return None
    return ran.stdout.removesuffix(b"\n") or None


def escape_pattern(raw: bytes) -> bytes:
    """`raw` as a literal gitignore pattern: each of backslash, `*`, `?`, `[`, `]` and space gets a backslash.
    (`#` and `!` only matter at the start, and every pattern here starts with `/`.)"""
    return b"".join(b"\\" + bytes([b]) if b in _GLOB_CHARS else bytes([b]) for b in raw)


def patterns(rel: bytes, *, sidecars: bool = True) -> list[bytes]:
    """The exclude lines for the capture at `rel` (relative to the work tree, `/`-separated bytes); with
    `sidecars` False just the file itself (a report output has no rotations or lock files)."""
    base = b"/" + escape_pattern(rel)
    if not sidecars:
        return [base]
    return [
        base,
        *(base + rotation for rotation in ROTATION_PATTERNS),
        *(base + suffix for suffix in SIDECAR_SUFFIXES),
        *(base + leftover for leftover in LEFTOVER_PATTERNS),
    ]


def parse_budget(raw: str) -> float:
    """The `--exclude-budget` text as seconds in [0, ENSURE_BUDGET_SECONDS]. Anything that is not a plain number
    (empty, `nan`, a non-ASCII digit) is 0: no git at all is the safe answer for a junk value; `inf` and large
    values are the maximum, negative ones 0."""
    try:
        value = float(raw) if raw.isascii() else float("nan")
    except ValueError:
        return 0.0
    return _clamp(value)


def _clamp(value: float) -> float:
    if value != value:  # NaN  # noqa: PLR0124
        return 0.0
    return min(max(value, 0.0), ENSURE_BUDGET_SECONDS)


def _unsafe(rel: bytes) -> bool:
    """A control byte (newline, CR, NUL, tab, escape...) cannot be written to an exclude line safely."""
    return any(b < 0x20 or b == 0x7F for b in rel)


@contextlib.contextmanager
def _locked(fd: int, deadline: float) -> Iterator[None]:
    """An exclusive flock on the exclude file, so two appenders do not interleave; unlocked where flock itself
    fails. A holder that outlasts the `deadline` raises TimeoutError (an OSError): a stuck lock cannot stall the
    appender."""
    held = False
    if _HAS_FLOCK:
        while not held:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except BlockingIOError:  # noqa: PERF203 - a short poll loop; the exception is the signal
                if time.monotonic() >= deadline:
                    raise TimeoutError(errno.EAGAIN, "exclude file is locked") from None
                time.sleep(0.01)
            except OSError:
                break
    try:
        yield
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _real(path: Path | str) -> Path:
    return Path(os.path.realpath(path))


def _target(exclude: Path) -> Path:
    """`exclude`, or, when it is a symlink, the file it really names: only if that stays inside `<git dir>/info/`
    (links, chains and `..` resolved by realpath). Anything else (git's config, a hook, a tracked .gitignore, a
    file outside .git, a directory, a loop) raises OSError: the caller warns."""
    if not exclude.is_symlink():
        return exclude
    root = _real(exclude.parent.parent) / "info"
    target = _real(exclude)
    if root not in target.parents:
        raise OSError(errno.ELOOP, "exclude is a symlink that leaves .git/info")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise OSError(errno.ELOOP, "exclude link does not end at a regular file")
    return target


def _add_lines(exclude: Path, lines: list[bytes], deadline: float) -> None:
    """Append the lines of `lines` the file lacks (exact match, CR ignored), in one write, after a newline when
    the file does not end in one. Creates the file and its directory. A symlink at `exclude` is followed only
    inside `<git dir>/info/` (see `_target`); the final name is opened O_NOFOLLOW, so a link swapped in after
    the check is an ELOOP error, and the opened file is compared with the resolved name again."""
    target = _target(exclude)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        if target != exclude:  # a followed link: the name must still lead to the file that was opened
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, _target(exclude).stat()):
                raise OSError(errno.ELOOP, "exclude link changed while it was opened")
        with _locked(fd, deadline):
            existing = b""
            if os.fstat(fd).st_size:
                os.lseek(fd, 0, os.SEEK_SET)
                chunks = []
                while chunk := os.read(fd, 1 << 20):
                    chunks.append(chunk)
                existing = b"".join(chunks)
            have = {line.removesuffix(b"\r") for line in existing.split(b"\n")}
            missing = [line for line in lines if line not in have]
            if not missing:
                return
            block = b"" if _COMMENT in have else _COMMENT + b"\n"
            prefix = b"\n" if existing and not existing.endswith(b"\n") else b""
            os.write(fd, prefix + block + b"\n".join(missing) + b"\n")
    finally:
        os.close(fd)


def unignored(top: Path, rels: list[bytes], deadline: float) -> list[bytes] | None:
    """The probes `git check-ignore` does not ignore. `--no-index`: a tracked file is judged by the patterns
    alone. None when git could not answer (missing, hung, past the deadline): the caller then stays silent."""
    ran = git(top, ["check-ignore", "--no-index", "-z", "--stdin"], deadline, b"\0".join(rels) + b"\0")
    if ran is None or ran.returncode not in (0, 1):
        return None
    ignored = {item for item in ran.stdout.split(b"\0") if item}
    return [rel for rel in rels if rel not in ignored]


def ensure_excluded(
    capture: Path, *, sidecars: bool = True, budget: float = ENSURE_BUDGET_SECONDS, what: str = "capture"
) -> str | None:
    """Make git ignore `capture` and its sidecars (just the file when `sidecars` is False, for the digest
    report's output); the warning for a case the user should know, else None. `what` is the noun
    the warnings use for the file ("capture", or "digest" for the report output).
    Takes at most about `budget` seconds (clamped to [0, ENSURE_BUDGET_SECONDS]; 0 does nothing)."""
    try:
        return _ensure(capture, time.monotonic() + _clamp(budget), sidecars=sidecars, what=what)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
        return f"{WARNING_PREFIX} could not keep the {what} out of git ({type(error).__name__})"


def tracked_warning(top: Path, rel: bytes, deadline: float, what: str) -> str | None:
    """Warn when git already tracks the capture: ignore rules do not stop a tracked file being committed. Runs at
    every call, ignored or not (one `git ls-files`, inside the deadline); the plugin shows the warning once."""
    tracked = git(top, ["ls-files", "-z", "--", ":(literal)" + os.fsdecode(rel)], deadline)
    if tracked is not None and tracked.returncode == 0 and tracked.stdout:
        return f"{WARNING_PREFIX} the {what} is already tracked by git: run `git rm --cached` on it"
    return None


def _ensure(capture: Path, deadline: float, *, sidecars: bool = True, what: str = "capture") -> str | None:
    parent = capture.parent.resolve()  # through symlinks: git sees where the file really is
    top_out = _out(git(parent, ["rev-parse", "--show-toplevel"], deadline))
    if top_out is None:
        return None  # no such directory, not a git repo, a bare one, or git is missing
    top = Path(os.fsdecode(top_out)).resolve()
    try:
        rel_path = (parent / capture.name).relative_to(top)
    except ValueError:
        return None  # the capture is outside the work tree
    if capture.name in ("", ".", ".."):
        return None
    rel = os.fsencode(rel_path.as_posix())
    if _unsafe(rel):
        return f"{WARNING_PREFIX} the {what} path has a control character, so it is not added to .git/info/exclude"
    probes = [rel + suffix for suffix in (PROBE_SUFFIXES if sidecars else (b"",))]
    missing = unignored(top, probes, deadline)
    if not missing:  # all ignored already, or git could not answer (the tracked check then fails the same way)
        return tracked_warning(top, rel, deadline, what)
    where = _out(git(top, ["rev-parse", "--git-path", "info/exclude"], deadline))
    if where is None:
        return None
    exclude = Path(os.path.abspath(top / os.fsdecode(where)))  # noqa: PTH100 - `git-path` is relative to cwd
    try:
        _add_lines(exclude, patterns(rel, sidecars=sidecars), deadline)
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, "error")
        return f"{WARNING_PREFIX} cannot write .git/info/exclude ({code}): the {what} may be committed by `git add`"
    if unignored(top, probes, deadline):  # None (git could not answer) stays silent
        return f"{WARNING_PREFIX} a .gitignore rule re-includes the {what}: it may be committed by `git add`"
    return tracked_warning(top, rel, deadline, what)
