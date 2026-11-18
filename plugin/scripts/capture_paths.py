"""Where the usage-observer plugin's capture lives, shared by `reflect_report`, `agent_telemetry report` and
the freshness check.

The plugin writes to `<main checkout>/logs/agent-telemetry.mod.jsonl`, so a default
resolved against the bare cwd is wrong from a subdirectory or a linked git worktree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Variables that redirect where git looks, what it calls the work tree, or how it reads a pathspec: an inherited
# one would answer for another repository (`GIT_WORK_TREE=/usr` made a bare clone's worktree resolve to /usr) or
# make `:(literal,icase)name` match nothing (`GIT_LITERAL_PATHSPECS=1`: a tracked file read as untracked). The
# `GIT_CEILING_DIRECTORIES` is deliberately not here: it fences a repository off (a dotfiles repository at $HOME)
# and the plugin's resolveHome honours it. The same names, in the same order, are `GIT_REPO_ENV` in
# plugin/hooks/alertEvents.ts; a test keeps them equal. Tested as needed: `GIT_OBJECT_DIRECTORY` (a missing one
# makes git fail, so the home falls back to the cwd). `GIT_NAMESPACE` and `GIT_ALTERNATE_OBJECT_DIRECTORIES` changed
# no answer in any experiment (git 2.56); they are cleared as harmless pass-throughs, with no test of their own.
_GIT_ENV_ORDER = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)
_GIT_ENV_DROPPED = frozenset(_GIT_ENV_ORDER)
PLUGIN_CAPTURE_RELATIVE = Path("logs") / "agent-telemetry.mod.jsonl"


def _git_path(cwd: Path, option: str) -> Path | None:
    """The path `git rev-parse <option>` prints (absolute, `cwd`-relative resolved), or None when git is
    missing, fails or prints nothing. Bytes decoded with the filesystem encoding, not `text=True`: a path
    that is not UTF-8 raised `UnicodeDecodeError` there."""
    env = {key: value for key, value in os.environ.items() if key not in _GIT_ENV_DROPPED}
    try:
        ran = subprocess.run(
            ["git", "rev-parse", option],
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = ran.stdout.removesuffix(b"\n")  # not strip(): a path may end in a space
    if ran.returncode != 0 or not out:
        return None
    return Path(os.path.abspath(cwd / os.fsdecode(out)))  # noqa: PTH100 - normalise `..` without following symlinks


def project_home(cwd: Path) -> Path:
    """The parent of the git common directory when that is named `.git` (the main checkout, also from a
    linked worktree). Else (a worktree of a bare repository, a submodule, a separate git dir) the work
    tree's top level, and `cwd` when git has none (a bare repository itself) or is missing.
    `git rev-parse --git-common-dir` works before git 2.31's `--path-format`."""
    common = _git_path(cwd, "--git-common-dir")
    if common is None:
        return cwd
    if common.name == ".git":
        return common.parent
    return _git_path(cwd, "--show-toplevel") or cwd


def default_plugin_capture() -> Path:
    """`CLAUDE_OBSERVER_CAPTURE` if non-empty, else the main checkout's plugin capture."""
    override = os.environ.get("CLAUDE_OBSERVER_CAPTURE")
    if override:
        return Path(override)
    return project_home(Path.cwd()) / PLUGIN_CAPTURE_RELATIVE
