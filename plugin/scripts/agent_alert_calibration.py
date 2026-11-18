#!/usr/bin/env python3
"""What the subagent alerts would have flagged in this project's own history: the script behind the
`calibrate-alerts` skill.

    agent_alert_calibration.py [--capture PATH] [--size-percentile X] [--hours H] [--count-only]
                               [--write --size-percentile X --hours H]

Run it from the project directory. It reads this project's plugin capture and its rotated `<capture>.N` files
(never any other file), drops repeated records by (run, seq), leaves out subagents whose last record is in the last
10 minutes, and prints aggregate counts and percentiles only: no id, name, path, prompt or tool text. One subagent
is one (session, agent id) pair, cumulative over its runs. A value "passes" a cutoff when it is strictly greater.

`--count-only` prints only the number of past subagents, the calibration's age and the hint the pane and the doctor
show. `--write` (with both values agreed) writes `alert-calibration.json` next to the capture: nothing is written
below 100 subagents.

Exit codes as reflect_report.py: 0 printed; 2 bad arguments; 3 nothing usable (Python older than 3.10, no capture or
not a plugin capture); 1 a failed write.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, NoReturn

import capture_exclude
from capture_paths import project_home
from reflect_report import MIN_PYTHON, Refusal, resolve_capture, rotated_files
from safe_name import printable_text

if TYPE_CHECKING:
    from collections.abc import Iterator

MIN_AGENTS = 100
RECENT_SECONDS = 600
MAX_LINE_BYTES = 1024 * 1024
MAX_AGENTS = 200_000
MAX_TOKENS = 10_000_000  # one request's count; above it the value is not a token count (as the probe)
MAX_MS = 86_400_000.0
MAX_TURN_MS = 7 * 86_400_000.0
MAX_CALIBRATION_BYTES = 64 * 1024
CALIBRATION_FILE = "alert-calibration.json"
DEFAULT_CUTOFF = 10_000_000
DEFAULT_HOURS = 2
PERCENTILES = (50, 90, 95, 98, 99, 99.5)
CANDIDATE_PERCENTILES = (95, 98, 99, 99.5)
CANDIDATE_HOURS = (1, 2, 4, 8)
STALE_DAYS = 30
NO_FILE_HINT = "subagent alerts use defaults: ask Claude to calibrate them"
# The mod does not read a calibration file git tracks: a cloned repository could ship one that sets or silences the
# limits. The pane, the doctor and `--write` say so with this text.
TRACKED_HINT = (
    f"{'alert-calibration.json'} is tracked by git, so the alerts ignore it "
    "(a cloned repository could set your limits): run `git rm --cached` on it, then calibrate again"
)
MAX_CUTOFF = 1e12
MAX_HOURS = 1000.0
MIN_HOURS = 0.1  # floors for the two arguments: below them every subagent would flag, so a tiny value is a typo
MIN_PERCENTILE = 1.0
MIN_TS = 946_684_800.0  # 2000-01-01T00:00:00Z: a record time outside 2000 to 2100 is not a time
MAX_TS = 4_102_444_800.0  # 2100-01-01T00:00:00Z
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _now() -> float:
    return time.time()


@dataclass
class _Agent:
    session: str
    tokens: int = 0
    has_steps: bool = False
    first_ts: float = math.inf
    last_ts: float = -math.inf
    turn_ms: float = 0.0
    recorded: set[str] = field(default_factory=set)
    spans: dict[str, list[float]] = field(default_factory=dict)  # turn id -> [first start, last end]
    loose: list[tuple[float, float, int | None]] = field(default_factory=list)  # steps with no turn id


@dataclass
class Subagent:
    session: str
    tokens: int
    active_s: float
    first_ts: float
    last_ts: float


@dataclass
class History:
    agents: list[Subagent] = field(default_factory=list)
    sessions: int = 0
    repeats: int = 0
    recent: int = 0
    bad_lines: int = 0
    dropped_agents: int = 0
    timed_out: bool = False  # the read stopped at its deadline: the other fields hold only what was read by then


@dataclass
class Calibration:
    agents: int
    calibrated: date
    percentile: float | None = None
    cutoff: float | None = None
    hours: float | None = None


def _number(value: object) -> float:
    """A JSON number or an ASCII numeric string, else NaN (`float()` alone takes non-ASCII digits and `1_0`)."""
    if isinstance(value, bool):
        return math.nan
    if isinstance(value, str) and not (value.isascii() and "_" not in value):
        return math.nan
    try:
        return float(value) if isinstance(value, (int, float, str)) else math.nan
    except (ValueError, OverflowError):
        return math.nan


def _bounded(value: object, top: float) -> float | None:
    number = _number(value)
    return number if math.isfinite(number) and 0 <= number <= top else None


def _time(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # A time without a zone is UTC, as quota_readings and mod_coverage read it: the machine's zone must not
        # change which subagents count.
        seconds = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return seconds if MIN_TS <= seconds < MAX_TS else None  # a year-1 or year-9999 value is no time of ours


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


def _skip_rest_of_line(handle: BinaryIO, raw: bytes) -> None:
    while raw and not raw.endswith(b"\n"):
        raw = handle.readline(MAX_LINE_BYTES + 1)


def _decode_row(raw: bytes, history: History) -> dict[str, Any] | None:
    try:
        row = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # bad bytes, bad JSON or a nesting too deep
        history.bad_lines += 1
        return None
    if not isinstance(row, dict):
        history.bad_lines += 1
        return None
    return row


def _rows(handle: BinaryIO, history: History, deadline: float | None) -> Iterator[dict[str, Any]]:
    first = True
    while True:
        if _expired(deadline):
            history.timed_out = True
            return
        raw = handle.readline(MAX_LINE_BYTES + 1)
        if first:
            raw, first = raw.removeprefix(b"\xef\xbb\xbf"), False
        if not raw:
            return
        if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
            history.bad_lines += 1
            _skip_rest_of_line(handle, raw)
            continue
        if not raw.strip():
            continue
        row = _decode_row(raw, history)
        if row is not None:
            yield row


def _lines(path: Path, history: History, deadline: float | None = None) -> Iterator[dict[str, Any]]:
    """The decoded records of one regular file, a line over MAX_LINE_BYTES skipped without holding it. Past the
    monotonic `deadline` it stops and sets `history.timed_out`; a UTF-8 BOM at the start of the file is dropped."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return
    with os.fdopen(fd, "rb") as handle:
        try:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return
            yield from _rows(handle, history, deadline)
        except OSError:
            return


def _activity(agent: _Agent) -> float:
    """Active seconds: the sum of the `turn` records' ms; a run with steps and no turn record of its own falls back
    to its steps' span, and steps with no turn id are split into runs at `index == 0`."""
    seconds = agent.turn_ms / 1000.0
    for turn, (start, end) in agent.spans.items():
        if turn not in agent.recorded:
            seconds += max(0.0, end - start)
    if "" not in agent.recorded and agent.loose:
        runs: list[list[float]] = []
        for t, start, index in sorted(agent.loose, key=lambda s: s[0]):
            if index == 0 or not runs:
                runs.append([start, t])
            else:
                runs[-1][0] = min(runs[-1][0], start)
                runs[-1][1] = max(runs[-1][1], t)
        seconds += sum(max(0.0, end - start) for start, end in runs)
    return seconds


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_repeat(row: dict[str, Any], seen: set[tuple[str, int]]) -> bool:
    """Whether the record's (run, seq) was seen before; a new pair is remembered."""
    run, seq = row.get("run"), row.get("seq")
    if isinstance(run, str) and isinstance(seq, int) and not isinstance(seq, bool):
        if (run, seq) in seen:
            return True
        seen.add((run, seq))
    return False


def _agent_of(
    row: dict[str, Any], agents: dict[tuple[str, str], _Agent], history: History
) -> tuple[_Agent | None, str]:
    """The agent a step or turn record belongs to (created on first sight, None when skipped) and the record's kind."""
    kind, agent_id = row.get("kind"), row.get("agent")
    if kind not in ("step", "turn") or not isinstance(agent_id, str) or not agent_id:
        return None, ""
    session = row.get("session")
    key = (session if isinstance(session, str) else "", agent_id)
    agent = agents.get(key)
    if agent is None:
        if len(agents) >= MAX_AGENTS:
            history.dropped_agents += 1
            return None, ""
        agent = agents[key] = _Agent(key[0])
    return agent, kind


def _add_step(agent: _Agent, row: dict[str, Any], t: float, turn: str) -> None:
    usage = row["usage"]
    agent.tokens += sum(int(_bounded(usage.get(name), MAX_TOKENS) or 0) for name in _TOKEN_FIELDS)
    agent.has_steps = True
    start = t - (_bounded(row.get("ms"), MAX_MS) or 0.0) / 1000.0
    index = row.get("index")
    if turn:
        span = agent.spans.setdefault(turn, [start, t])
        span[0], span[1] = min(span[0], start), max(span[1], t)
    else:
        agent.loose.append((t, start, index if _is_int(index) else None))


def _ingest(row: dict[str, Any], agent: _Agent, kind: str) -> None:
    t = _time(row.get("ts"))
    turn = row.get("turn")
    turn = turn if isinstance(turn, str) else ""
    if kind == "turn":
        agent.recorded.add(turn)
        agent.turn_ms += _bounded(row.get("ms"), MAX_TURN_MS) or 0.0
    else:
        if t is None or not isinstance(row.get("usage"), dict):
            return
        _add_step(agent, row, t, turn)
    if t is not None:
        agent.first_ts, agent.last_ts = min(agent.first_ts, t), max(agent.last_ts, t)


def _finished_agents(agents: dict[tuple[str, str], _Agent], now: float, history: History) -> None:
    sessions: set[str] = set()
    for agent in agents.values():
        if not agent.has_steps:
            continue
        if now - agent.last_ts < RECENT_SECONDS:
            history.recent += 1
            continue
        sessions.add(agent.session)
        history.agents.append(Subagent(agent.session, agent.tokens, _activity(agent), agent.first_ts, agent.last_ts))
    history.sessions = len(sessions)


def load_history(capture: Path, now: float, deadline: float | None = None) -> History:
    """The past subagents in the capture and its rotated files. `deadline` is a `time.monotonic()` value: past it
    the read stops and `timed_out` is set, so a caller with a budget never waits on a huge capture."""
    history = History()
    agents: dict[tuple[str, str], _Agent] = {}
    seen: set[tuple[str, int]] = set()
    files = rotated_files(capture)
    if capture.is_file() or capture.is_symlink():
        files.append(capture)
    for path in files:
        for row in _lines(path, history, deadline):
            if _is_repeat(row, seen):
                history.repeats += 1
                continue
            agent, kind = _agent_of(row, agents, history)
            if agent is not None:
                _ingest(row, agent, kind)
    _finished_agents(agents, now, history)
    return history


def nearest_rank(values: list[float], p: float) -> float:
    ordered = sorted(values)
    exact = Fraction(str(p)) * len(ordered) / 100  # exact: a float `p / 100 * n` rounds up on some exact multiples
    return ordered[max(0, math.ceil(exact) - 1)]


def _finite(value: object) -> bool:
    """A JSON number (not a bool) that is a finite float; an integer too big for a float is not one (`isfinite`
    raises OverflowError on it, which the mod's `Number.isFinite` reads as Infinity)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def read_calibration(capture: Path) -> Calibration | None:
    """The calibration file next to the capture, or None when it is missing, a link, too big or not valid."""
    path = capture.parent / CALIBRATION_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CALIBRATION_BYTES:
            return None
        raw = os.read(fd, MAX_CALIBRATION_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        body = json.loads(raw.decode("utf-8"))
        agents, day = body["agents"], body["calibrated"]
        if type(agents) is not int or not 1 <= agents <= 10**9 or not isinstance(day, str) or not _DAY.fullmatch(day):
            return None
        calibrated = date.fromisoformat(day)
    except (ValueError, KeyError, TypeError, RecursionError, UnicodeError):
        return None
    out = Calibration(agents, calibrated)
    percentile, cutoff, hours = body.get("percentile"), body.get("cutoff"), body.get("hours")
    ok = [_finite(v) for v in (percentile, cutoff, hours)]
    if ok[0] and ok[1] and MIN_PERCENTILE <= percentile < 100 and 1 <= cutoff <= MAX_CUTOFF:
        out.percentile, out.cutoff = percentile, cutoff
    if ok[2] and MIN_HOURS <= hours <= MAX_HOURS:
        out.hours = hours
    return out


def calibration_tracked(capture: Path) -> bool:
    """Whether git tracks the calibration file next to the capture (the mod then ignores it)."""
    return capture_exclude.is_tracked(capture.parent / CALIBRATION_FILE)


def age_days(cal: Calibration, now: float) -> int:
    return max(0, (datetime.fromtimestamp(now, timezone.utc).date() - cal.calibrated).days)


def calibration_hint(agents: int, cal: Calibration | None, now: float) -> str | None:
    """The one line the pane and the doctor show, or None. Thresholds are the design's call, not measured."""
    if cal is None:
        return NO_FILE_HINT if agents >= MIN_AGENTS else None
    days = age_days(cal, now)
    if days > STALE_DAYS:
        return f"subagent alert calibration is {days} days old: ask Claude to calibrate again"
    if agents >= 2 * cal.agents:
        return (
            f"subagent history doubled since the calibration ({cal.agents} to {agents} subagents): "
            "ask Claude to calibrate again"
        )
    return None


def _g(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def percentile_text(value: float) -> str:
    """A size percentile as the mod prints it (alerts.ts `Math.min(99.99, Number(p.toFixed(2)))`): two decimals, never
    100 (`:g` would round 99.99999 up to 100). The file keeps the exact value; only the text is capped.
    `toFixed` takes the larger of two equally near values on the float's exact decimal value, so this rounds
    `Decimal(value)` half up (`round()` goes to the even one: 98.125 is p98.13 in the alert, not p98.12). `value`
    is finite and below 100 (`_percentile_arg`, `parse_calibration`)."""
    return _g(min(99.99, float(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))))


def _flags(values: list[float], cutoff: float, weeks: float) -> str:
    n = sum(1 for v in values if v > cutoff)
    return f"flags {n} ({n / weeks:.1f} per week)"


def report(
    history: History, shown: str, cal: Calibration | None, now: float, percentile: float | None, hours: float | None
) -> list[str]:
    n = len(history.agents)
    out = [f"source: {shown}"]
    first = min((a.first_ts for a in history.agents), default=now)
    last = max((a.last_ts for a in history.agents), default=now)
    day = datetime.fromtimestamp
    first_day, last_day = day(first, timezone.utc).date(), day(last, timezone.utc).date()
    out.append(f"past subagents: {n} from {history.sessions} sessions, {first_day} to {last_day}")
    if history.recent:
        out.append(f"left out: {history.recent} subagents with a record in the last 10 minutes")
    if n < MIN_AGENTS:
        out.append(f"not enough history ({n} subagents, {MIN_AGENTS} needed)")
        out.append(f"keep the defaults: {DEFAULT_CUTOFF} tokens and {DEFAULT_HOURS} h")
        return out
    weeks = ((last_day - first_day).days + 1) / 7
    sizes = [float(a.tokens) for a in history.agents]
    hours_all = [a.active_s / 3600.0 for a in history.agents]
    out.extend(f"size p{p:g}: {_g(nearest_rank(sizes, p))}" for p in PERCENTILES)
    out.append(f"size max: {_g(max(sizes))}")
    wanted = sorted({*CANDIDATE_PERCENTILES, *([percentile] if percentile is not None else [])})
    for p in wanted:
        cut = nearest_rank(sizes, p)
        out.append(f"size cutoff p{percentile_text(p)}: {_g(cut)}: {_flags(sizes, cut, weeks)}")
    out.extend(f"active p{p:g}: {nearest_rank(hours_all, p):.2f} h" for p in PERCENTILES)
    out.append(f"active max: {max(hours_all):.2f} h")
    out.extend(f"active over {h} h: {_flags(hours_all, h, weeks)}" for h in CANDIDATE_HOURS)
    if hours is not None and hours not in CANDIDATE_HOURS:
        out.append(f"active over {_g(hours)} h (yours): {_flags(hours_all, hours, weeks)}")
    if cal is None:
        out.append(f"in force: size {DEFAULT_CUTOFF} tokens, time {DEFAULT_HOURS} h (defaults, not calibrated)")
    else:
        size = (
            f"size p{percentile_text(cal.percentile)} cutoff {_g(cal.cutoff)} tokens"
            if cal.percentile is not None and cal.cutoff is not None
            else f"size {DEFAULT_CUTOFF} tokens (default)"
        )
        time_part = f"time {_g(cal.hours)} h" if cal.hours is not None else f"time {DEFAULT_HOURS} h (default)"
        out.append(f"in force: {size}, {time_part} (calibrated {cal.calibrated} on {cal.agents} subagents)")
    fits = [p for p in CANDIDATE_PERCENTILES if sum(v > nearest_rank(sizes, p) for v in sizes) * 100 <= n]
    pick = fits[0] if fits else CANDIDATE_PERCENTILES[-1]
    cut = nearest_rank(sizes, pick)
    size_flags = sum(v > cut for v in sizes)
    text = f"size percentile {pick:g} (cutoff {_g(cut)}, flags {size_flags} of {n}), "
    fit_hours = [h for h in CANDIDATE_HOURS if sum(v > h for v in hours_all) * 100 <= n]
    if fit_hours:
        text += f"hours {fit_hours[0]} (flags {sum(v > fit_hours[0] for v in hours_all)} of {n})"
    else:
        text += f"hours: none of {', '.join(map(str, CANDIDATE_HOURS))} keeps it at or under 1%; keep {DEFAULT_HOURS}"
    out.append(f"recommend: {text}")
    return out


def table(history: History) -> dict[str, dict[str, float]]:
    sizes = [float(a.tokens) for a in history.agents]
    hours = [a.active_s / 3600.0 for a in history.agents]
    keys = [f"p{p:g}" for p in PERCENTILES]
    return {
        "size": {
            **{k: int(nearest_rank(sizes, p)) for k, p in zip(keys, PERCENTILES, strict=True)},
            "max": int(max(sizes)),
        },
        "active_hours": {
            **{k: round(nearest_rank(hours, p), 4) for k, p in zip(keys, PERCENTILES, strict=True)},
            "max": round(max(hours), 4),
        },
    }


def write_calibration(capture: Path, history: History, percentile: float, hours: float, now: float) -> str:
    """Writes the file atomically; returns what was written. Raises OSError."""
    sizes = [float(a.tokens) for a in history.agents]
    body: dict[str, Any] = {
        "v": 1,
        "percentile": int(percentile) if percentile.is_integer() else percentile,
        "cutoff": int(nearest_rank(sizes, percentile)),
        "hours": int(hours) if hours.is_integer() else hours,
        "agents": len(history.agents),
        "calibrated": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"),
        "table": table(history),
    }
    target = capture.parent / CALIBRATION_FILE
    if target.is_symlink():
        # Renaming the new file over a link would turn the link into a regular file; the mod and the reader
        # never used a linked file, so nothing is written and the user decides about the link.
        raise OSError(errno.ELOOP, "it is a symbolic link; remove the link and run this again")
    temp = capture.parent / f".{CALIBRATION_FILE}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, indent=1) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(target)
    except OSError:
        temp.unlink(missing_ok=True)
        raise
    if warning := capture_exclude.ensure_excluded(target, sidecars=False, what="alert calibration"):
        print(printable_text(warning), file=sys.stderr)
    return (
        f"wrote {CALIBRATION_FILE}: percentile {percentile_text(body['percentile'])}, cutoff {body['cutoff']} tokens, "
        f"hours {_g(body['hours'])}, {body['agents']} subagents"
    )


def _percentile_arg(text: str) -> float:
    value = float(text) if _DECIMAL.fullmatch(text) else math.nan
    if not MIN_PERCENTILE <= value < 100:
        raise argparse.ArgumentTypeError(f"must be a number from 1 up to (not including) 100, got {text!r}")
    return value


def _hours_arg(text: str) -> float:
    value = float(text) if _DECIMAL.fullmatch(text) else math.nan
    if not MIN_HOURS <= value <= MAX_HOURS:
        raise argparse.ArgumentTypeError(f"must be a number from 0.1 to 1000, got {text!r}")
    return value


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, f"{self.format_usage()}{self.prog}: error: {message}\n")


def _python_too_old() -> bool:
    if sys.version_info >= MIN_PYTHON:
        return False
    found = ".".join(str(part) for part in sys.version_info[:3])
    print(f"Python {found} is too old: this needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.", file=sys.stderr)
    return True


def _build_parser() -> _Parser:
    parser = _Parser(
        description="What the subagent alerts would have flagged in this project's history.", allow_abbrev=False
    )
    parser.add_argument("--capture", help="the plugin capture, relative to the project home or absolute")
    parser.add_argument("--size-percentile", type=_percentile_arg, help="percentile for the size cutoff")
    parser.add_argument("--hours", type=_hours_arg, help="hours for the time alert")
    parser.add_argument("--count-only", action="store_true", help="print only the counts and the hint")
    parser.add_argument("--write", action="store_true", help="save the agreed values (needs both)")
    return parser


def _print_count_only(shown: str, history: History, cal: Calibration | None, tracked: bool, now: float) -> None:
    print(f"source: {shown}\npast subagents: {len(history.agents)}")
    if tracked:
        print("calibration: ignored (tracked by git)")
        print(f"hint: {TRACKED_HINT}")
        return
    print("calibration: " + ("none" if cal is None else f"{age_days(cal, now)} days old, {cal.agents} agents"))
    if hint := calibration_hint(len(history.agents), cal, now):
        print(f"hint: {hint}")


def _finish_write(
    parser: argparse.ArgumentParser, args: argparse.Namespace, capture: Path, history: History, now: float
) -> int:
    if len(history.agents) < MIN_AGENTS:
        print("nothing written")
        return 0
    if nearest_rank([float(a.tokens) for a in history.agents], args.size_percentile) < 1:
        parser.error("the size cutoff would be 0 tokens, which the alerts cannot use; nothing written")
    try:
        print(write_calibration(capture, history, args.size_percentile, args.hours, now))
    except OSError as error:
        print(f"cannot write {CALIBRATION_FILE}: {error.strerror or 'error'}", file=sys.stderr)
        return 1
    return 0


def _after_report(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    capture: Path,
    history: History,
    tracked: bool,
    now: float,
) -> int:
    if tracked:
        print(f"calibration file: ignored, {TRACKED_HINT}")
        if args.write:
            print(f"cannot write {CALIBRATION_FILE}: {TRACKED_HINT}", file=sys.stderr)
            return 1
    if args.write:
        return _finish_write(parser, args, capture, history, now)
    return 0


def main(argv: list[str] | None = None) -> int:
    if _python_too_old():
        return 3
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.write and (args.size_percentile is None or args.hours is None):
        parser.error("--write needs both --size-percentile and --hours (the values the user agreed to)")
    now = _now()
    try:
        capture, shown = resolve_capture(project_home(Path.cwd()), args.capture)
        history = load_history(capture, now)
        tracked = calibration_tracked(capture)
        cal = None if tracked else read_calibration(capture)  # the limits in force: the mod ignores a tracked file
        if args.count_only:
            _print_count_only(shown, history, cal, tracked, now)
            return 0
        print("\n".join(report(history, shown, cal, now, args.size_percentile, args.hours)))
        return _after_report(parser, args, capture, history, tracked, now)
    except Refusal as refusal:
        print(str(refusal), file=sys.stderr)
        return refusal.code


if __name__ == "__main__":
    raise SystemExit(main())
