"""The usage-observer doctor: read-only checks of the install and the capture.

    python3 -B doctor.py [--capture PATH] [--json] [--excerpt-lines N] [--out PATH] [--keep] [--show]

`doctor.py` is the launcher (it guards the Python version, then imports this module). Each check returns a
`Result`: a level (`ok`, `warn`, `fail`, `unknown`, `info`) and one line. A line holds counts, levels, versions that
matched a strict pattern and the project-relative capture path; never a value read from the capture, never a home
directory. A check that could not run is `unknown`, never `ok`.

Exit code: 0 when every result is `ok` or `info`; 1 when any is `warn` or `fail`; else 3 when any is `unknown`;
2 for a usage error (argparse). The checks write nothing: no probe file, no index refresh. The excerpt step (see
`doctor_excerpt`) writes one file, `doctor-excerpt.md`, in a new private temp directory or at `--out`, and removes
its work directory. The processes it starts are `git` (read-only calls through `capture_exclude`), the
interpreter probes (`python3`, `python`, `py -3`, each `--version`) and `claude --version`.

Limits: `os.access` and the mode bits can be fooled by ACLs and read-only mounts, so "writable" is a hint, not a
proof. The capture's latest `session_start` is looked for in the last TAIL_BYTES only. The probes read the whole
output of a program within 3 seconds, and keep 4096 characters of it.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import capture_exclude as ce
import check_capture_content as ccc
import excerpt_project as ep
from agent_telemetry import rotated_captures
from capture_paths import default_plugin_capture, project_home
from check_agent_telemetry_freshness import DEFAULT_MAX_AGE_HOURS
from safe_name import printable_line

if TYPE_CHECKING:
    from collections.abc import Callable

# The one place the minimum Claude Code version lives in code; a test pins it to the documented one.
MIN_CLAUDE_CODE = "2.1.287"
# plugin/hooks/python.ts: `PYTHON_CANDIDATES` and `MIN_PYTHON`, pinned equal by a test.
PYTHON_CANDIDATES: tuple[tuple[str, ...], ...] = (("python3",), ("python",), ("py", "-3"))
MIN_PYTHON = (3, 10)
LEVELS = ("ok", "warn", "fail", "unknown", "info")
PROBE_SECONDS = 3.0
PROBE_KEEP = 4096
TAIL_BYTES = 256 * 1024
MAX_LINE = 64 * 1024
SMALL_FILE = 1024 * 1024
CONTENT_SECONDS = 60
GIT_SECONDS = 8.0
SHOWN_CAP = 120
EXCERPT_DEFAULT_LINES = 10
EXCERPT_MAX_LINES = 50  # excerpt_project.MAX_KEEP, equal by test
TEXT_CAP = 400
NAME_CAP = 80
OUTSIDE_HINT = (
    " (outside your home directory: in the system temp directory (/tmp unless TMPDIR is set), or where --out put it)"
)
CLOCK_SKEW_SECONDS = 300
OSRELEASE = Path("/proc/sys/kernel/osrelease")
# The surfaces the plugin records (observer.ts: terminal, desktop, vscode, mobile or null); any other value is "other".
SURFACES = frozenset({"terminal", "desktop", "vscode", "mobile"})

# One version grammar with the excerpt's projection (excerpt_project._VERSION, 1-4 digits a part): a number the capture's
# writer controls is shown only when the excerpt would keep it too.
_VERSION_OUT = re.compile(r"\A\s{0,8}([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})(?![0-9.])", re.ASCII)
_VERSION_FIELD = ep._VERSION  # noqa: SLF001  (the one grammar, not a copy)
_PYTHON_OUT = re.compile(r"\A\s{0,8}Python ([0-9]{1,3})\.([0-9]{1,3})(?![0-9])")


@dataclass(frozen=True)
class Result:
    name: str
    level: str
    text: str


def exit_code(results: list[Result]) -> int:
    levels = {r.level for r in results}
    if levels & {"warn", "fail"}:
        return 1
    return 3 if "unknown" in levels else 0


# ---- helpers ---------------------------------------------------------------------------------------------------


def _clean(text: str, cap: int = SHOWN_CAP) -> str:
    line = printable_line(text)
    return line if len(line) <= cap else line[: cap - 3] + "..."


def shown_path(path: Path | str, home: Path | str) -> str:
    """`path` for display: `<project>/<relative>` inside the project home, else `<outside project>/<name>`. `..` is
    normalised without following links (then once with links, for a project reached through a link); the name is
    made printable and capped. Never a home directory or an absolute path."""
    for resolve in (os.path.abspath, os.path.realpath):
        try:
            relative = Path(resolve(path)).relative_to(resolve(home))
        except ValueError:
            continue
        return "<project>/" + _clean(relative.as_posix())
    return "<outside project>/" + _clean(Path(os.path.abspath(path)).name)  # noqa: PTH100


def _probe(argv: list[str], timeout: float) -> tuple[int, str] | None:
    """Run `argv` with stdout and stderr together, at most `timeout` seconds (the whole process group is killed
    on the deadline, so a grandchild holding the pipe cannot stall us). (exit code, first 4096 characters), or
    None when the program is missing, not runnable or too slow."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError):
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows
                proc.kill()
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            proc.communicate(timeout=1)
        return None
    return proc.returncode, out.decode("utf-8", "replace")[:PROBE_KEEP]


def _read_tail(path: Path) -> bytes | None:
    """The last TAIL_BYTES of a regular file, from the first complete line; None for anything else (a pipe is never
    opened blocking) or an unreadable file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        start = max(0, info.st_size - TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        chunks: list[bytes] = []
        left = TAIL_BYTES
        while left > 0 and (chunk := os.read(fd, left)):
            chunks.append(chunk)
            left -= len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if start > 0:  # the window opens mid-line: drop that partial line
        cut = data.find(b"\n")
        data = b"" if cut < 0 else data[cut + 1 :]
    return data


def _last_session_start(capture: Path | None) -> dict[str, object] | None:
    """The newest `session_start` record in the capture's tail. A torn, over-long, deeply nested or invalid line is
    skipped, never an error."""
    data = _read_tail(capture) if capture is not None else None
    if not data:
        return None
    for line in reversed(data.split(b"\n")):
        if b'"session_start"' not in line or len(line) > MAX_LINE:
            continue
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict) and record.get("kind") == "session_start":
            return record
    return None


def _version_tuple(match: re.Match[str]) -> tuple[int, ...]:
    return tuple(int(part) for part in match[0].strip().split("."))


def _dotted(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


# ---- checks ----------------------------------------------------------------------------------------------------


def check_claude_version(run: Callable[[list[str], float], tuple[int, str] | None], capture: Path | None) -> Result:
    """`claude --version`, else the latest `session_start.version` in the capture, against MIN_CLAUDE_CODE. Only a
    version matching a strict pattern is used or shown."""
    minimum = tuple(int(part) for part in MIN_CLAUDE_CODE.split("."))
    found: tuple[int, ...] | None = None
    source = "claude --version"
    ran = run(["claude", "--version"], PROBE_SECONDS)
    if ran is not None and ran[0] == 0 and (match := _VERSION_OUT.match(ran[1])):
        found = _version_tuple(match)
    else:
        start = _last_session_start(capture)
        field = start.get("version") if start else None
        if isinstance(field, str) and (match := _VERSION_FIELD.fullmatch(field)):
            found = _version_tuple(match)
            source = "the latest session_start in the capture"
        elif isinstance(field, str) and field:  # a version the excerpt would not keep: named, never echoed
            return Result(
                "claude-code",
                "unknown",
                f"the version in the capture is other (not x.y.z with 1-4 digits a part; the minimum is {MIN_CLAUDE_CODE})",
            )
    if found is None:
        return Result(
            "claude-code",
            "unknown",
            f"could not read the version from `claude --version` or the capture (the minimum is {MIN_CLAUDE_CODE})",
        )
    if found < minimum:
        return Result(
            "claude-code",
            "fail",
            f"{_dotted(found)} from {source} is older than the minimum {MIN_CLAUDE_CODE}: "
            "the plugin's hooks are ignored without a message",
        )
    return Result("claude-code", "ok", f"{_dotted(found)} from {source} (minimum {MIN_CLAUDE_CODE})")


def check_python(run: Callable[[list[str], float], tuple[int, str] | None]) -> Result:
    """The hook's probe: each candidate with `--version`, the first 3.10 or newer wins."""
    tried: list[str] = []
    for argv in PYTHON_CANDIDATES:
        name = " ".join(argv)
        ran = run([*argv, "--version"], PROBE_SECONDS)
        match = _PYTHON_OUT.match(ran[1]) if ran is not None and ran[0] == 0 else None
        if match is None:
            tried.append(f"{name} (not usable)")
            continue
        version = (int(match[1]), int(match[2]))
        tried.append(f"{name} {version[0]}.{version[1]}")
        if version >= MIN_PYTHON:
            here = sys.version_info
            return Result(
                "python",
                "ok",
                f"{tried[-1]} (tried: {', '.join(tried)}); this doctor runs on Python {here[0]}.{here[1]}.{here[2]}",
            )
    return Result(
        "python",
        "fail",
        f"no Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer: none found (tried: {', '.join(tried)}); "
        "the plugin cannot write the capture",
    )


def _nearest_dir(path: Path) -> tuple[Path, os.stat_result] | None:
    """`path` or its nearest existing ancestor, with its stat (links followed). A path that is not there (or has a
    file where a directory should be) is climbed past; any other error (EACCES, ELOOP, EIO) is raised, because
    "cannot look" is not "does not exist"."""
    while True:
        try:
            info: os.stat_result | None = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode):
            return path, info
        if path == path.parent:
            return None
        path = path.parent


def _writable(path: Path, info: os.stat_result, flags: int) -> bool:
    """`os.access` and, when we own it, the owner's permission bit (os.access says yes to root for a 0555 directory,
    and the plugin runs as the user)."""
    if not os.access(path, flags):
        return False
    uid = os.getuid() if hasattr(os, "getuid") else None
    return not (info.st_uid == uid and not info.st_mode & stat.S_IWUSR)


def _dir_result(info: os.stat_result | None, shown: str) -> Result:
    if info is None:
        return Result("capture-dir", "info", f"{shown} does not exist yet; the plugin creates it with mode 0700")
    mode = stat.S_IMODE(info.st_mode)
    if mode == 0o700:
        return Result("capture-dir", "ok", "capture directory mode 0700")
    if mode & stat.S_IWOTH:
        return Result("capture-dir", "warn", f"capture directory mode {mode:04o}: any local user can write to it")
    if mode & 0o077:
        return Result(
            "capture-dir",
            "info",
            f"capture directory mode {mode:04o}: it existed before the plugin (the plugin sets 0700 only on a "
            "directory it creates); the capture file's own mode 0600 keeps its content private",
        )
    return Result("capture-dir", "ok", f"capture directory mode {mode:04o}")


def check_capture_file(capture: Path, home: Path, now: float | None = None) -> list[Result]:
    """Existence, mode, directory mode, writability (no probe file), age by the file's mtime, size and rotations."""
    now = time.time() if now is None else now
    shown = shown_path(capture, home)
    results: list[Result] = []
    info: os.stat_result | None
    try:
        info = capture.stat()
    except FileNotFoundError:
        info = None
    except OSError as error:
        info = None
        results.append(
            Result("capture-file", "unknown", f"cannot read {shown} ({errno.errorcode.get(error.errno or 0)})")
        )
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            results.append(Result("capture-file", "fail", f"{shown} is not a regular file"))
            info = None
        else:
            link = " (a symbolic link: the file it names is checked)" if capture.is_symlink() else ""
            results.append(Result("capture-file", "ok", f"{shown} exists{link}"))
    if info is None and not results:
        results.append(
            Result(
                "capture-file",
                "warn",
                f"no capture at {shown}: the plugin is not loaded in this project or no session has run since",
            )
        )
    if info is not None:
        results.extend(_file_results(capture, info, now))
    parent = capture.parent
    try:
        near = _nearest_dir(parent)
    except OSError as error:
        why = errno.errorcode.get(error.errno or 0, "an error")
        results.append(Result("capture-dir", "unknown", f"cannot look at the directory of {shown} ({why})"))
        results.append(Result("capture-writable", "unknown", "the directory could not be examined"))
        return results
    exact = near is not None and near[0] == parent
    results.append(_dir_result(near[1] if near and exact else None, shown_path(parent, home)))
    results.append(_writable_result(capture, info, near, exact))
    return results


def _file_results(capture: Path, info: os.stat_result, now: float) -> list[Result]:
    mode = stat.S_IMODE(info.st_mode)
    out = [
        Result("capture-mode", "warn", f"capture mode {mode:04o}, expected 0600: other users can read it")
        if mode & 0o077
        else Result("capture-mode", "ok", f"capture mode {mode:04o}")
    ]
    age = now - info.st_mtime
    hours = age / 3600
    window = DEFAULT_MAX_AGE_HOURS * 3600
    if age < -CLOCK_SKEW_SECONDS:
        out.append(Result("capture-age", "warn", "the capture's modification time is in the future: check the clock"))
    elif age > window:
        out.append(
            Result(
                "capture-age",
                "warn",
                f"the capture last grew {hours:.1f}h ago, over the {DEFAULT_MAX_AGE_HOURS}h window: "
                "the plugin is not loaded or no session has run since",
            )
        )
    else:
        out.append(Result("capture-age", "ok", f"the capture last grew {max(hours, 0.0):.1f}h ago (file mtime)"))
    try:
        rotated = str(len(rotated_captures(capture)))
    except OSError:
        rotated = "an unknown number of"
    out.append(Result("capture-size", "info", f"{info.st_size} bytes, {rotated} rotated files"))
    return out


def _writable_result(
    capture: Path, info: os.stat_result | None, near: tuple[Path, os.stat_result] | None, exact: bool
) -> Result:
    if near is None:
        return Result("capture-writable", "fail", "no existing directory above the capture path")
    directory, dir_info = near
    if not _writable(directory, dir_info, os.W_OK | os.X_OK):
        return Result("capture-writable", "fail", "the capture directory is not writable: the plugin cannot append")
    if info is not None and not _writable(capture, info, os.W_OK):
        return Result("capture-writable", "fail", "the capture file is not writable: the plugin cannot append")
    if not exact:
        return Result(
            "capture-writable", "ok", "the capture directory does not exist yet; the nearest existing one is writable"
        )
    return Result("capture-writable", "ok", "the capture directory and file are writable (by mode and os.access)")


def _read_small(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, SMALL_FILE)
    except OSError:
        return None
    finally:
        os.close(fd)


def check_git(capture: Path, budget: float = GIT_SECONDS, home: Path | None = None) -> list[Result]:
    """Is the capture, a rotated file and each sidecar ignored, is it tracked, is the plugin's `info/exclude` line
    there. Read-only: `git check-ignore`, `git ls-files`, `git rev-parse`, through `capture_exclude`."""
    deadline = time.monotonic() + budget
    shown = shown_path(capture, project_home(Path.cwd()) if home is None else home)
    try:
        anchor = _nearest_dir(capture.parent)
    except OSError:
        return [Result("git-ignore", "unknown", "cannot look at the capture's directory")]
    if anchor is None:
        return [Result("git-ignore", "unknown", "no existing directory above the capture path")]
    missing_parts = capture.parent.relative_to(anchor[0]).parts if capture.parent.is_relative_to(anchor[0]) else ()
    full = anchor[0].resolve().joinpath(*missing_parts, capture.name)
    ran = ce.git(anchor[0].resolve(), ["rev-parse", "--show-toplevel"], deadline)
    if ran is None:
        return [Result("git-ignore", "unknown", "git did not answer (missing, too slow or out of time)")]
    top_out = ran.stdout.removesuffix(b"\n")
    if ran.returncode != 0 or not top_out:
        return [Result("git-ignore", "info", "the capture is not in a git work tree: no ignore check")]
    top = Path(os.fsdecode(top_out)).resolve()
    try:
        rel = os.fsencode(full.relative_to(top).as_posix())
    except ValueError:
        return [Result("git-ignore", "info", "the capture is outside the git work tree: no ignore check")]
    unignored = ce.unignored(top, [rel + suffix for suffix in ce.PROBE_SUFFIXES], deadline)
    if unignored is None:
        return [Result("git-ignore", "unknown", "git check-ignore did not answer")]
    if ce.tracked_warning(top, rel, deadline, "capture") is not None:
        first = Result("git-ignore", "fail", f"{shown} is tracked by git: run `git rm --cached` on it")
    elif not unignored:
        first = Result("git-ignore", "ok", "the capture, its rotated files and its sidecars are ignored by git")
    elif rel in unignored:
        first = Result("git-ignore", "warn", f"{shown} is not ignored by git: `git add -A` would commit it")
    else:
        first = Result("git-ignore", "warn", "a rotated file or sidecar name of the capture is not ignored by git")
    return [first, _exclude_line(top, rel, rel not in unignored, deadline)]


def _exclude_line(top: Path, rel: bytes, ignored: bool, deadline: float) -> Result:
    where = ce.git(top, ["rev-parse", "--git-path", "info/exclude"], deadline)
    body = None
    if where is not None and where.returncode == 0 and where.stdout.strip():
        exclude = Path(os.path.abspath(top / os.fsdecode(where.stdout.removesuffix(b"\n"))))  # noqa: PTH100
        body = _read_small(exclude)
    lines = {line.removesuffix(b"\r") for line in (body or b"").split(b"\n")}
    wanted = ce.patterns(rel)
    present = sum(1 for pattern in wanted if pattern in lines)
    if present == len(wanted):
        return Result("git-exclude-line", "ok", "the plugin's lines are in .git/info/exclude")
    note = "; the capture is ignored by another rule" if ignored else ""
    if present:
        return Result(
            "git-exclude-line",
            "info",
            f"only {present} of the plugin's {len(wanted)} lines are in .git/info/exclude{note}",
        )
    return Result("git-exclude-line", "info", f"the plugin's lines are not in .git/info/exclude{note}")


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with OSRELEASE.open("rb") as handle:
            return b"microsoft" in handle.read(256).lower()
    except OSError:
        return False


def check_session(capture: Path | None) -> Result:
    """Cloud variable, WSL and the latest session's surface: information, never a failure."""
    parts: list[str] = []
    if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        parts.append("CLAUDE_CODE_REMOTE=true: a cloud session, where capture is off")
    if _is_wsl():
        parts.append("WSL detected (a Desktop WSL session never loads plugins; the CLI inside WSL does)")
    start = _last_session_start(capture)
    if start is None:
        parts.append("no session_start in the capture's tail")
    else:
        surface = start.get("surface")
        shown = (
            surface
            if isinstance(surface, str) and surface in SURFACES
            else ("not recorded" if surface is None else "other")
        )
        parts.append(f"latest session surface: {shown}")
    return Result("session", "info", "; ".join(parts))


def check_content(capture: Path) -> Result:
    """`check_capture_content` over the capture and its rotated files; counts only."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ccc.main([f"--capture={capture}", "--json", f"--max-seconds={CONTENT_SECONDS}"])
    except SystemExit:
        return Result("content", "unknown", "the content check refused its arguments")
    except (OSError, ValueError, UnicodeError, MemoryError, RecursionError) as error:
        return Result("content", "unknown", f"the content check did not run ({type(error).__name__})")
    if code == 2:
        return Result("content", "unknown", "no capture file to check")
    try:
        body = json.loads(out.getvalue())
        totals = body["totals"]
        figures = {name: totals[name] for name in ("lines", "removable", "serialized", "unparseable", "identity")}
        files = len(body["files"])
    except (ValueError, KeyError, TypeError):
        return Result("content", "unknown", "the content check printed no counts")
    if not all(type(v) is int for v in figures.values()):
        return Result("content", "unknown", "the content check printed no counts")
    counts = (
        f"{figures['lines']} lines in {files} files; content in {figures['removable']} removable, "
        f"{figures['serialized']} serialized and {figures['unparseable']} unparseable lines; "
        f"identity in {figures['identity']} lines"
    )
    if code == 0:
        return Result("content", "ok", f"none found: {counts}")
    if code == 1:
        return Result("content", "fail", f"content or identity attributes are on disk: {counts}")
    return Result("content", "unknown", f"the scan was incomplete: {counts}")


# ---- running ---------------------------------------------------------------------------------------------------


def _guard(name: str, check: Callable[[], list[Result] | Result]) -> list[Result]:
    """A check that raises is `unknown` with the exception's type name, never a crash and never its message."""
    try:
        got = check()
    except Exception as error:  # the doctor must answer
        return [Result(name, "unknown", f"the check did not run ({type(error).__name__})")]
    return got if isinstance(got, list) else [got]


def run_checks(
    capture: Path, home: Path, run: Callable[[list[str], float], tuple[int, str] | None] = _probe
) -> list[Result]:
    results: list[Result] = []
    results += _guard("claude-code", lambda: check_claude_version(run, capture))
    results += _guard("python", lambda: check_python(run))
    results += _guard("capture-file", lambda: check_capture_file(capture, home))
    results += _guard("git-ignore", lambda: check_git(capture, home=home))
    results += _guard("session", lambda: check_session(capture))
    results += _guard("content", lambda: check_content(capture))
    return results


def render_text(results: list[Result], shown: str, code: int) -> str:
    lines = ["usage-observer doctor", f"capture: {shown}"]
    lines += [f"[{r.level}] {r.name}: {_clean(r.text, TEXT_CAP)}" for r in results]
    lines.append(f"result: exit {code}")
    return "\n".join(lines)


def render_json(results: list[Result], shown: str, code: int, excerpt: str | None = None) -> str:
    rows = [{**asdict(r), "text": _clean(r.text, TEXT_CAP)} for r in results]
    body: dict[str, object] = {"capture": shown, "results": rows, "exit_code": code}
    if excerpt is not None:
        body["excerpt"] = excerpt
    return json.dumps(body, indent=2)


def _display_path(path: Path, *, private_dir: bool = False) -> str:
    """A path the user must open: `~/...` under their home directory; anywhere else only the file name and a one-line
    hint, because an absolute path outside the home directory can name directories. `private_dir` adds the name of the
    parent, the doctor's own random directory (no private data in it), so the file can be found under the temp
    directory. Made printable and capped."""
    full = os.path.abspath(path)  # noqa: PTH100
    try:
        relative = Path(full).relative_to(os.path.expanduser("~"))  # noqa: PTH111
    except ValueError:
        name = Path(full).name
        if private_dir:
            name = f"{Path(full).parent.name}/{name}"
        return _clean(name, NAME_CAP) + OUTSIDE_HINT
    return _clean("~/" + relative.as_posix(), TEXT_CAP)


def _replace_shown(text: str, shown: str, shown_dir: str) -> str:
    """The capture's and its directory's display names replaced by `<capture>` and `<capture dir>`, longest first."""
    for old, new in sorted(((shown, "<capture>"), (shown_dir, "<capture dir>")), key=lambda pair: -len(pair[0])):
        text = text.replace(old, new)
    return text


def excerpt_result(
    capture: Path, home: Path, args: argparse.Namespace, results: list[Result]
) -> tuple[list[Result], str | None]:
    """Build the excerpt and say so in an `excerpt` result (and an `excerpt-keep` result with --keep); the second value
    is the excerpt's text for `--show`. A refusal is a result with counts only, never a traceback or a value."""
    try:
        import doctor_excerpt as de  # noqa: PLC0415  (lazy: a failed import is an `unknown` result)
    except Exception as error:  # the doctor must answer, with the type name only
        return [Result("excerpt", "unknown", f"the excerpt module did not load ({type(error).__name__})")], None
    shown, shown_dir = shown_path(capture, home), shown_path(capture.parent, home)
    lines = [(r.name, r.level, _replace_shown(r.text, shown, shown_dir)) for r in results]
    try:
        built = de.build_excerpt(
            capture,
            home,
            content_check=check_content,
            lines=args.excerpt_lines,
            out=args.out,
            keep=args.keep,
            checks=lines,
        )
    except de.ExcerptRefused as refusal:
        return [Result("excerpt", refusal.level, refusal.reason)], None
    except Exception as error:  # nothing may leave a traceback with a value in it
        return [Result("excerpt", "unknown", f"the excerpt was not built ({type(error).__name__})")], None
    found = [
        Result(
            "excerpt",
            "info",
            f"{_display_path(built.path, private_dir=args.out is None)} written, mode 0600; read it before you paste it ({built.note})",
        )
    ]
    if built.kept_dir is not None:  # its own line: a long path above must not cut the warning off (TEXT_CAP)
        found.append(
            Result(
                "excerpt-keep",
                "info",
                f"never paste or share {_display_path(built.kept_dir)}: it holds the scrubbed, unprojected copy of the "
                "capture's tail; the scrub is not a guarantee, so it can still hold names and other text",
            )
        )
    return found, built.text


def _excerpt_lines(text: str) -> int:
    try:
        value = int(text, 10) if text.isascii() and text.isdigit() else 0
    except ValueError:  # a digit run past the int-conversion limit
        value = 0
    if not 1 <= value <= EXCERPT_MAX_LINES:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {EXCERPT_MAX_LINES}")
    return value


def main(argv: list[str] | None = None, *, run: Callable[[list[str], float], tuple[int, str] | None] = _probe) -> int:
    parser = argparse.ArgumentParser(prog="doctor.py", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--capture", type=Path, help="the capture file (default: the plugin's, or CLAUDE_OBSERVER_CAPTURE)"
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument(
        "--excerpt-lines",
        type=_excerpt_lines,
        default=EXCERPT_DEFAULT_LINES,
        metavar="N",
        help=f"records shown in the excerpt (default {EXCERPT_DEFAULT_LINES}, at most {EXCERPT_MAX_LINES})",
    )
    parser.add_argument("--out", type=Path, help="write the excerpt here (never overwrites a file)")
    parser.add_argument(
        "--keep", action="store_true", help="keep the work directory (it holds the scrubbed, unprojected copy)"
    )
    parser.add_argument(
        "--show", action="store_true", help="also print the excerpt (for a person's terminal, not a model's)"
    )
    args = parser.parse_args(argv)
    home = project_home(Path.cwd())
    capture: Path = args.capture if args.capture is not None else default_plugin_capture()
    shown = shown_path(capture, home)
    results = run_checks(capture, home, run)
    extra, excerpt = excerpt_result(capture, home, args, results)
    results.extend(extra)
    code = exit_code(results)
    shown_text = excerpt if args.show else None
    if args.json:
        print(render_json(results, shown, code, shown_text))
    else:
        print(render_text(results, shown, code))
        if shown_text is not None:
            print(shown_text)
    return code


if __name__ == "__main__":
    sys.exit(main())
