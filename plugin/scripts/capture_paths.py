"""Where the usage-observer plugin's capture lives, shared by `reflect_report`, `agent_telemetry report` and
the freshness check.

The plugin writes to `<main checkout>/logs/agent-telemetry.mod.jsonl`, so a default
resolved against the bare cwd is wrong from a subdirectory or a linked git worktree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Variables that redirect where git looks or what it calls the work tree: an inherited one would answer for
# another repository (`GIT_WORK_TREE=/usr` made a bare clone's worktree resolve to /usr).
_GIT_ENV_DROPPED = frozenset(
    {
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
    }
)
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
