"""Maps a `~/.claude/projects/<slug>` directory name to the project it belongs to.

Claude Code names a transcript directory after the working directory, every non-alphanumeric
character replaced by `-`. The same project therefore shows up under several slugs: its main
checkout, its worktrees (`<project>--claude-worktrees-<name>`, `<project>--worktrees-<name>`) and the
session scratchpads (`-tmp-claude-<uid>-<project...>`). `group_of` folds them into one project key,
the slug of the main checkout.

Rules, in order, on one slug:

1. A scratchpad prefix `-tmp-claude-<ascii digits>-` is dropped and the rest continues, when the slug is a scratchpad
   slug: prefix, a project slug starting with `-`, and `-<session uuid>-scratchpad`. A real project under
   `/tmp/claude-<uid>/` is not one: it keeps its whole slug.
2. An entry of the optional override file (`projects.json`, slug prefix to project) whose prefix
   matches at a `-` boundary wins, longest prefix first. Its value is the project key; a value
   that is not a slug (no leading `-`) is a plain project name.
3. The earliest worktree marker cuts the slug; the part before it is the project.
4. The longest known root slug that is a prefix at a `-` boundary wins; for a scratchpad the remainder must be
   exactly `-<session uuid>-scratchpad` (a sibling's scratchpad is not folded into a shorter root).
5. Otherwise the slug is its own project.

Known limit: a scratchpad whose root is not a known root stays its own group, so the scratchpad of a project
that has only worktree transcript directories, or of a session started in a sub-directory (`<project>-sub`), is not
folded into the project. That is attribution: `quota_totals` adds the groups' totals, so the all-projects total
changes only if one response is repeated across the two groups. Folding it would need a prefix guess, which rule 4
refuses (`proj-docs` is not `proj`).

This module reads directory NAMES only (via the callers) and never opens a transcript.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

WORKTREE_MARKERS = ("--claude-worktrees-", "--worktrees-")
# ASCII digits only: `\d` would also take Arabic-Indic and other Unicode digits
_SCRATCHPAD = re.compile(r"^-tmp-claude-[0-9]+-(.+)$", re.DOTALL)
# what follows a project slug in a scratchpad slug: `/<session uuid>/scratchpad` of the scratchpad path
# used with `fullmatch`: a `$` anchor would also accept one trailing newline
_SCRATCHPAD_TAIL = re.compile(
    r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-scratchpad", re.IGNORECASE
)
_SCRATCHPAD_END = re.compile(_SCRATCHPAD_TAIL.pattern + r"\Z", re.IGNORECASE)  # `\Z`, not `$`: no trailing newline
OVERRIDES_ENV = "QUOTA_PROJECTS_FILE"


class ProjectsFileError(ValueError):
    """The override file exists but cannot be used. The message names the file."""


SLUG_MAX_UNITS = 200  # Claude Code truncates a slug longer than this many UTF-16 code units


def claude_slug(text: str) -> str:
    """Claude Code's (2.1.312) project-folder name for the absolute path `text`, ported exactly.

    It is JavaScript `text.replace(/[^a-zA-Z0-9]/g, "-")` over UTF-16 code units: a character outside
    the BMP is two units and so gives two dashes. A slug longer than 200 units is cut to its first 200
    and gets `-` plus the base-36 of `Math.abs(h)`, where `h` is the int32 31-hash
    (`h = ((h << 5) - h + unit) | 0`) of the ORIGINAL path's units."""
    # surrogatepass: a lone surrogate (an undecodable file name byte) is one unit and must not raise
    raw = text.encode("utf-16-le", "surrogatepass")
    units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
    slug = "".join(chr(u) if chr(u) in _SLUG_KEEP else "-" for u in units)
    if len(slug) <= SLUG_MAX_UNITS:
        return slug
    h = 0
    for unit in units:
        h = (h * 31 + unit) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000  # int32 wrap-around
    return f"{slug[:SLUG_MAX_UNITS]}-{_base36(abs(h))}"  # abs(INT_MIN) = 2**31, as `Math.abs` gives


_SLUG_KEEP = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def slug_of(path: Path) -> str:
    """The transcript-directory slug of a working directory (the rule `agent_usage_report` uses)."""
    return claude_slug(str(path.resolve()))


def default_overrides_path() -> Path:
    env = os.environ.get(OVERRIDES_ENV)
    if env:
        return Path(env)
    return Path.home() / ".claude" / "observer" / "projects.json"


def load_overrides(path: Path) -> dict[str, str]:
    """The slug-prefix to project mapping of `path`; `{}` when the file does not exist.

    A file that exists but is unreadable, not valid JSON, not an object of non-empty strings
    to non-empty strings raises `ProjectsFileError` naming the file: a wrong grouping must never
    be the silent fallback."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ProjectsFileError(f"{path}: cannot be read ({type(err).__name__})") from None
    try:
        parsed = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # bad UTF-8, bad or too deeply nested JSON, nan/Infinity
        raise ProjectsFileError(f"{path}: is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise ProjectsFileError(f"{path}: must hold a JSON object of slug prefix to project")
    overrides: dict[str, str] = {}
    for key, value in parsed.items():
        if not key or not isinstance(value, str) or not value:
            raise ProjectsFileError(f"{path}: every entry must map a non-empty prefix to a non-empty project")
        overrides[key] = value
    return overrides


def _reject_constant(name: str) -> object:
    raise ValueError(name)  # `NaN` and `Infinity` are not JSON


def _has_prefix(slug: str, prefix: str) -> bool:
    """`prefix` is `slug` or a prefix of it ending where a `-` follows."""
    return slug == prefix or slug.startswith(prefix + "-")


def _scratch_of(rest: str, root: str) -> bool:
    return rest.startswith(root) and _SCRATCHPAD_TAIL.fullmatch(rest[len(root) :]) is not None


def _marker_index(slug: str) -> int:
    positions = [slug.find(marker) for marker in WORKTREE_MARKERS]
    found = [p for p in positions if p >= 0]
    return min(found) if found else -1


def _scratchpad_rest(slug: str) -> str | None:
    """The project slug inside a scratchpad slug, else None. A scratchpad slug is the prefix, the slug of a project
    directory (it starts with `-`, being an absolute path) and `-<session uuid>-scratchpad`, all three. A real
    project under `/tmp/claude-<uid>/` (`/tmp/claude-1000/x/proj`, `/tmp/claude-1000/.x/proj`) lacks the tail, so it
    keeps its whole slug and is its own project, as `this_project` already judges it."""
    match = _SCRATCHPAD.match(slug)
    if match is None:
        return None
    rest = match.group(1)
    if not rest.startswith("-") or _SCRATCHPAD_END.search(rest) is None:
        return None
    return rest


def _drop_scratchpad(slug: str) -> str:
    rest = _scratchpad_rest(slug)
    return slug if rest is None else rest


def is_scratchpad(slug: str) -> bool:
    return _scratchpad_rest(slug) is not None


def known_roots(slugs: Iterable[str]) -> list[str]:
    """The slugs that are a main directory: neither a worktree nor a scratchpad."""
    return [slug for slug in slugs if slug and _marker_index(slug) < 0 and not is_scratchpad(slug)]


def group_of(slug: str, known_roots: Iterable[str] = (), overrides: Mapping[str, str] | None = None) -> str:
    """The project key of one transcript-directory slug (see the module docstring for the rules)."""
    rest = _drop_scratchpad(slug)
    scratch = rest != slug
    if overrides:
        matches = [prefix for prefix in overrides if _has_prefix(rest, prefix)]
        if matches:
            return overrides[max(matches, key=len)]
    cut = _marker_index(rest)
    if cut > 0:
        return rest[:cut]
    # a scratchpad folds only into the root whose session scratchpad it is, so the scratchpad of an unlisted
    # sibling (`proj-docs`) is not taken for one of `proj`
    roots = [root for root in known_roots if root and (_scratch_of(rest, root) if scratch else _has_prefix(rest, root))]
    if roots:
        return max(roots, key=len)
    return rest


def project_name(key: str) -> str:
    """A readable name of a project key: the last path segment of a slug (a guess), or the key itself
    when it came from `projects.json` as a plain name."""
    if not key.startswith("-"):
        return key
    return key.rsplit("-", 1)[-1] or key


def this_project(
    cwd: Path,
    known: Iterable[str] = (),
    overrides: Mapping[str, str] | None = None,
    projects_root: Path | None = None,
) -> str:
    """The project key of the working directory.

    The cwd belongs to a project only when it is that project, and that is checked, not read off the name: its own
    transcript directory exists (it is in `known`), or it is inside a git worktree that checks out on disk
    (`_worktree_project`), or it is exactly a session scratchpad `/tmp/claude-<own uid>/<project slug>/<uuid>/scratchpad`
    whose project is a known root (`group_of`) or, for a worktree slug, whose session transcript
    `<projects_root>/<project slug>/<uuid>.jsonl` exists as a regular file (`_scratchpad_session_exists`; without a
    `projects_root` it is not folded), or an override names it. A cwd with none of these
    (`/home/u/proj-docs` with only `-home-u-proj` known, a new directory under a home project, `work/proj_/worktrees/x`,
    `/tmp/claude-7/.home.u.proj`) is NOT folded into another project: its key is its own slug, which has no
    directory, so nothing of another project is read for it."""
    resolved = cwd.resolve()
    slug = slug_of(cwd)
    rest = _drop_scratchpad(slug)
    grouped = group_of(slug, known, overrides)
    if slug != rest and not _scratchpad_shape(resolved):
        return slug  # before the overrides: they too fold only the own uid's scratchpad
    if overrides and any(_has_prefix(rest, prefix) for prefix in overrides):
        return grouped
    if slug != rest:
        # a worktree slug cuts at its marker by name alone, so the session must exist on disk
        if _marker_index(rest) >= 0 and not _scratchpad_session_exists(resolved, projects_root):
            return slug
        return grouped
    if _marker_index(rest) > 0:
        main = _worktree_project(resolved)
        return grouped if main is not None and main == grouped else slug
    return grouped if rest in set(known) else slug


# the directories under a project's main checkout where its git worktrees live (Claude Code's own layout is the first)
WORKTREE_DIRS = ((".claude", "worktrees"), (".worktrees",))
GITFILE_MAX = 4096  # a worktree's `.git` file holds one `gitdir: <path>` line


def _gitdir_of(worktree: Path) -> Path | None:
    """The `gitdir:` target named by `<worktree>/.git`, or None. Reads one small regular file: a symlink, FIFO,
    directory, oversized, binary, multi-line or unreadable file gives None."""
    git_file = worktree / ".git"
    try:
        if not stat.S_ISREG(git_file.lstat().st_mode):
            return None
        fd = os.open(git_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            raw = os.read(fd, GITFILE_MAX + 1)
        finally:
            os.close(fd)
    except OSError:
        return None
    if len(raw) > GITFILE_MAX or b"\0" in raw:
        return None
    lines = raw.decode("utf-8", "surrogateescape").splitlines()
    if len(lines) != 1 or not lines[0].startswith("gitdir:"):
        return None
    target = lines[0][len("gitdir:") :].strip()
    try:
        return (worktree / target).resolve()  # an absolute target replaces `worktree`; a relative one is under it
    except (OSError, RuntimeError, ValueError):
        return None


def _worktree_project(cwd: Path) -> str | None:
    """The slug of the main checkout when `cwd` (resolved) is inside a real git worktree of it, else None.

    A parent P is the main checkout when the cwd is under `P/.claude/worktrees/<name>` or `P/.worktrees/<name>`,
    `P/.git` is a directory, and `<name>/.git` is a file saying `gitdir: <P>/.git/worktrees/<id>` (an existing
    directory). The slug alone proves nothing: any path can contain `.worktrees`."""
    for parent in reversed(cwd.parents):  # outermost first, as the earliest marker of `group_of` wins
        tail = cwd.relative_to(parent).parts
        for layout in WORKTREE_DIRS:
            if len(tail) <= len(layout) or tail[: len(layout)] != layout:
                continue
            git_dir = parent / ".git"
            try:
                if git_dir.is_symlink():  # a file here means `parent` is itself a worktree: no `worktrees/` below it
                    continue
                target = _gitdir_of(parent.joinpath(*tail[: len(layout) + 1]))
                if target is not None and target.is_dir() and target.parent == (git_dir / "worktrees").resolve():
                    return slug_of(parent)
            except (OSError, ValueError):
                continue
    return None


def _scratchpad_shape(cwd: Path) -> bool:
    """Exactly `/tmp/claude-<own uid>/<slug of a project directory>/<session uuid>/scratchpad`: the slug directory is
    one name of letters, digits and `-`. The `/tmp/claude-<uid>` part must be spelled so (`/tmp_claude/1000` and
    `claude_1000` slug alike, and so does another user's uid)."""
    parts = cwd.parts
    return (
        len(parts) == 6
        and parts[:2] == ("/", "tmp")
        and parts[2] == f"claude-{os.getuid()}"  # the slug check takes any digits and `claude.7`, `claude_7`
        and re.fullmatch(r"-[A-Za-z0-9-]*", parts[3]) is not None
        and _SCRATCHPAD_TAIL.fullmatch("-" + parts[4] + "-" + parts[5]) is not None
    )


def _scratchpad_session_exists(cwd: Path, projects_root: Path | None) -> bool:
    """`<projects_root>/<slug>/<session uuid>.jsonl` is a regular file (a symlink is not followed) for the scratchpad
    `/tmp/claude-<uid>/<slug>/<uuid>/scratchpad` (already of that shape)."""
    if projects_root is None:
        return False
    transcript = projects_root / cwd.parts[3] / f"{cwd.parts[4]}.jsonl"
    try:
        return stat.S_ISREG(transcript.lstat().st_mode)
    except OSError:
        return False


def regular_file_inside(path: Path, directory: Path) -> bool:
    """A regular file under `directory`: not a symlink, FIFO, socket, device or directory, and not
    reached through a link. Opening a FIFO would block forever. Looks at metadata only."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
        return path.resolve().is_relative_to(directory.resolve())
    except (OSError, RuntimeError):
        return False
