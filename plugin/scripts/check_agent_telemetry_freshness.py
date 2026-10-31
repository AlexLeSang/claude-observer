#!/usr/bin/env python3
"""Advisory check: flag when `logs/agent-telemetry.mod.jsonl` (the plugin capture
the usage-observer plugin appends to) has not grown recently.

The plugin capture is the default; pass `--path
logs/agent-telemetry.jsonl` to check an old OTel receiver capture instead.

A 2026-09-27 finding is the reason this exists: a capture can stop growing
without any error. The message names both possible causes rather than assuming
one: the plugin is not installed or enabled in the project, OR no session has
run it since.

Staleness is measured by the capture file's own mtime (any append updates
it) against a 24-hour window, not by parsing the file's last line: the check
must not assume the file is well-formed JSONL.

Deliberately advisory, not a gate: a consumer can wire it into a session
checkpoint as one more non-halting advisory line, but not into pre-commit or CI.

Read-only (never touches the capture file). Exit codes: 0 = the capture file
has grown within the last 24 hours, 1 = it has not (or does not exist at
all).

Usage:
    python3 -B plugin/scripts/check_agent_telemetry_freshness.py
        [--path PATH] [--max-age-hours N] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from capture_paths import default_plugin_capture

DEFAULT_MAX_AGE_HOURS = 24

_STALE_MESSAGE_SUFFIX = "plugin not installed or sessions not running it"


@dataclass(frozen=True)
class FreshnessResult:
    path: str
    exists: bool
    fresh: bool
    age_hours: float | None
    max_age_hours: float
    message: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def check_freshness(
    path: Path,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> FreshnessResult:
    """Read-only staleness check against `path`'s own mtime. `now` is
    injectable for deterministic tests; defaults to the real current UTC
    time. A missing file is reported as stale (age_hours=None) rather than
    raising -- "never started" and "stopped growing" are both real
    findings this check exists to surface, not error conditions."""
    current = now if now is not None else datetime.now(timezone.utc)

    if not path.is_file():
        return FreshnessResult(
            path=str(path),
            exists=False,
            fresh=False,
            age_hours=None,
            max_age_hours=max_age_hours,
            message=(f"STALE: {path} does not exist -- {_STALE_MESSAGE_SUFFIX}"),
        )

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age = current - mtime
    age_hours = age.total_seconds() / 3600.0
    fresh = age <= timedelta(hours=max_age_hours)

    if fresh:
        message = f"fresh: {path} last grew {age_hours:.1f}h ago (within the {max_age_hours:.0f}h window)"
    else:
        message = (
            f"STALE: {path} last grew {age_hours:.1f}h ago, "
            f"over the {max_age_hours:.0f}h window -- {_STALE_MESSAGE_SUFFIX}"
        )

    return FreshnessResult(
        path=str(path),
        exists=True,
        fresh=fresh,
        age_hours=age_hours,
        max_age_hours=max_age_hours,
        message=message,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="path to the capture file (default: the main checkout's plugin capture, also from a subdirectory or "
        "linked worktree; env CLAUDE_OBSERVER_CAPTURE overrides it)",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help=f"staleness threshold in hours (default {DEFAULT_MAX_AGE_HOURS})",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_age_hours <= 0:
        print("error: --max-age-hours must be positive", file=sys.stderr)
        return 2

    result = check_freshness(
        args.path if args.path is not None else default_plugin_capture(), max_age_hours=args.max_age_hours
    )

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(result.message)

    return 0 if result.fresh else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
