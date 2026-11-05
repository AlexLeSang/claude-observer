"""Record `/usage` readings and derive weekly-quota points per eq.

    quota_readings.py add --week-pct 40 --week-reset 2026-10-20T16:00:00Z [--session-pct N] [--week-model-pct opus=15]
    quota_readings.py add --from-text < usage.txt       # parse piped /usage text; no model call
    quota_readings.py rate [--json] [--week-reset ISO] [--include-reset]

A reading is one JSON line in `~/.claude/observer/quota-readings.jsonl` (override `QUOTA_READINGS_FILE`). It stores
the all-projects eq total and this project's eq total for the window from the week start to `ts`, and nothing about any
other project. The totals come from `quota_totals` through the `totals_fn` seam of `main`: this module never reads a
transcript itself. Points per eq is the sum of week-percent deltas over the sum of all-projects eq deltas of the
qualifying reading pairs in one week; with none, no figure is given and no constant is substituted.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import IO, Any
from zoneinfo import ZoneInfo, available_timezones

import project_groups as pg
import quota_totals as qt

UTC = timezone.utc
WEEK = timedelta(days=7)
MAX_LINE_BYTES = 64 * 1024  # a longer line in the readings file is skipped and counted
MAX_TEXT_CHARS = 1024 * 1024  # piped /usage text is cut here
MAX_TEXT_LINE = 2000  # a longer line of /usage text is ignored
WEEK_TOLERANCE = timedelta(seconds=60)  # reset times of one week that differ by up to this are the same week
COARSE_BELOW = 3  # a pair spanning fewer points than this is flagged: percentages are whole numbers
ENV_FILE = "QUOTA_READINGS_FILE"

_PCT = re.compile(r"[0-9]{1,3}(?:\.[0-9]+)?")
_LABEL = re.compile(r"[a-z0-9][a-z0-9 ._-]{0,39}")
_WEEK_LINE = re.compile(
    r"current\s+week\s*\((?P<label>[^()]{1,40})\)\s*:?\s*(?P<pct>[0-9]+(?:\.[0-9]+)?)\s*%",
    re.IGNORECASE | re.ASCII,
)
_SESSION_LINE = re.compile(r"current\s+session\s*:?\s*(?P<pct>[0-9]+(?:\.[0-9]+)?)\s*%", re.IGNORECASE | re.ASCII)
_ZONE_NAME = re.compile(r"[A-Za-z0-9_+/-]{1,64}")
_RESETS = re.compile(r"\bresets\b(?P<rest>.*)", re.IGNORECASE | re.ASCII)
_RESET_TIME = re.compile(
    r"\s*(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>[0-9]{1,2}),?\s+)?"
    r"(?P<h>[0-9]{1,2})(?::(?P<m>[0-9]{2}))?\s*(?P<ap>[ap]m)\b"
    r"\s*(?:\((?P<tz>[^()]{0,80})\))?",
    re.IGNORECASE | re.ASCII,
)
_MONTHS = {
    m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])
}


class ReadingError(ValueError):
    """A reading that must not be stored; the message is ours and safe to print."""


class ZoneError(ReadingError):
    """The zone of a reset time is missing or unknown; an optional line may drop its reset on this."""


class TotalsUnavailable(ReadingError):  # noqa: N818
    """The eq-totals module could not be used."""


@dataclass(frozen=True)
class Totals:
    """All-projects and this-project eq for one window, plus a count of skipped files in other projects."""

    eq_all: float
    eq_project: float
    skipped: int = 0


TotalsFn = Callable[[datetime, datetime], Totals]


@dataclass(frozen=True)
class Reading:
    ts: datetime
    week_pct: float
    week_reset: datetime
    eq_all: float
    eq_project: float


@dataclass(frozen=True)
class Pair:
    from_ts: datetime
    to_ts: datetime
    delta_pct: float
    delta_eq: float
    from_reset: bool

    @property
    def coarse(self) -> bool:
        return self.delta_pct < COARSE_BELOW


@dataclass(frozen=True)
class Calibration:
    week_reset: datetime | None
    pairs: list[Pair]

    @property
    def points_per_eq(self) -> float | None:
        """None when no pair qualifies or the sums are not a usable finite positive figure."""
        if not self.pairs:
            return None
        # a sum of eq that overflowed gives 0.0, a tiny eq delta gives inf: neither is a figure
        rate = sum(p.delta_pct for p in self.pairs) / sum(p.delta_eq for p in self.pairs)
        if math.isfinite(rate) and rate > 0:
            return rate
        return None

    @property
    def eq_per_point(self) -> float | None:
        rate = self.points_per_eq
        return None if rate is None else 1 / rate

    @property
    def coarse(self) -> bool:
        return any(p.coarse for p in self.pairs)


# ---- values ---------------------------------------------------------------------------------------------------


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: object) -> datetime:
    """An ISO-8601 time as aware UTC; naive input is taken as UTC. ASCII only."""
    if not isinstance(value, str) or not value.isascii() or not 0 < len(value) <= 64:
        raise ReadingError("unparsable time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, OverflowError) as exc:  # OverflowError: year 9999 or 1 shifted out of range by an offset
        raise ReadingError("unparsable time") from exc


def parse_pct(value: object, what: str) -> float:
    """A whole or decimal percentage 0..100 from ASCII digits; nan, inf, signs and other digit scripts are refused."""
    if not isinstance(value, str) or not _PCT.fullmatch(value):
        raise ReadingError(f"{what} is not a percentage")
    number = float(value)
    if not 0 <= number <= 100:
        raise ReadingError(f"{what} is outside 0..100")
    return int(number) if number.is_integer() else number


def _finite(value: object, *, non_negative: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or (non_negative and number < 0):
        return None
    return number


def _model_label(raw: str) -> str:
    label = re.sub(r"\s+only$", "", " ".join(raw.lower().split()))
    if not _LABEL.fullmatch(label):
        raise ReadingError("unusable model label")
    return label


# ---- the file -------------------------------------------------------------------------------------------------


def readings_path() -> Path:
    override = os.environ.get(ENV_FILE)
    return Path(override) if override else Path.home() / ".claude" / "observer" / "quota-readings.jsonl"


def parse_reading(row: object) -> Reading | None:
    """A stored line as a Reading, or None when any field a calculation needs is missing or not a finite number."""
    if not isinstance(row, dict):
        return None
    pct = _finite(row.get("week_pct"))
    eq_all = _finite(row.get("eq_all"), non_negative=True)
    eq_project = _finite(row.get("eq_project"), non_negative=True)
    if pct is None or not 0 <= pct <= 100 or eq_all is None or eq_project is None:
        return None
    try:
        return Reading(parse_time(row.get("ts")), pct, parse_time(row.get("week_reset")), eq_all, eq_project)
    except ReadingError:
        return None


def load_readings(path: Path) -> tuple[list[Reading], int]:
    """Valid readings in file order and the count of skipped lines (torn, non-JSON, invalid, over-long)."""
    readings: list[Reading] = []
    skipped = 0
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return readings, skipped
    with handle:
        while True:
            raw = handle.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            too_long = len(raw) > MAX_LINE_BYTES
            while too_long and not raw.endswith(b"\n"):  # drain the rest of an over-long line
                raw = handle.readline(MAX_LINE_BYTES)
                if not raw:
                    break
            if too_long:
                skipped += 1
                continue
            if not raw.strip():
                continue
            try:
                parsed = parse_reading(json.loads(raw))
            except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
                parsed = None
            if parsed is None:
                skipped += 1
            else:
                readings.append(parsed)
    return readings, skipped


def append_line(path: Path, line: str) -> None:
    """One write of one line; a file whose last line was torn (no newline) gets a newline first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        size = os.fstat(fd).st_size
        prefix = b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""
        data = prefix + line.encode() + b"\n"
        while data:
            data = data[os.write(fd, data) :]
    finally:
        os.close(fd)


# ---- /usage text ----------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _zone_names() -> dict[str, str]:
    """Zone names by lower-case form, for `/usage` text that writes `(utc)` or `(europe/amsterdam)`."""
    return {name.lower(): name for name in sorted(available_timezones())}


def _zone(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, LookupError):  # ZoneInfoNotFoundError is a KeyError, so a LookupError
        pass
    exact = _zone_names().get(name.lower())
    if exact is not None:
        try:
            return ZoneInfo(exact)
        except (ValueError, OSError, LookupError):
            pass
    raise ZoneError("unknown time zone")


def _reset_time(rest: str, now: datetime) -> datetime:
    match = _RESET_TIME.match(rest)
    if not match:
        raise ReadingError("unparsable reset time")
    hour, minute = int(match["h"]), int(match["m"] or 0)
    if not 1 <= hour <= 12 or minute > 59:
        raise ReadingError("unparsable reset time")
    hour = hour % 12 + (12 if match["ap"].lower() == "pm" else 0)
    tz = match["tz"]
    if tz is None:
        raise ZoneError("the reset time has no recognised time zone group")
    if not _ZONE_NAME.fullmatch(tz):
        raise ZoneError("unknown time zone")
    zone = _zone(tz)
    if match["mon"]:
        month = _MONTHS.get(match["mon"][:3].lower())
        if month is None:
            raise ReadingError("unparsable reset time")
        for year in (now.year, now.year + 1):
            try:
                candidate = datetime(year, month, int(match["day"]), hour, minute, tzinfo=zone)
            except ValueError:
                continue
            if candidate.astimezone(UTC) > now:
                return candidate.astimezone(UTC)
        raise ReadingError("unparsable reset time")
    day: date = now.astimezone(zone).date()
    for _ in range(2):
        candidate = datetime.combine(day, time(hour, minute), tzinfo=zone)
        if candidate.astimezone(UTC) > now:
            return candidate.astimezone(UTC)
        day += timedelta(days=1)
    raise ReadingError("unparsable reset time")  # pragma: no cover - two days always contain a future instant


def parse_usage_text(text: str, now: datetime) -> dict[str, Any]:
    """Fields of a reading from `/usage` text; ReadingError when it has no week percentage or reset."""
    week: float | None = None
    week_reset: datetime | None = None
    session: float | None = None
    session_reset: datetime | None = None
    models: dict[str, float] = {}
    for line in text[:MAX_TEXT_CHARS].splitlines():
        if len(line) > MAX_TEXT_LINE:
            continue
        week_match = _WEEK_LINE.search(line)
        session_match = None if week_match else _SESSION_LINE.search(line)
        match = week_match or session_match
        if not match:
            continue
        pct = parse_pct(match["pct"], "percentage")
        resets = _RESETS.search(line, match.end())
        label = " ".join(week_match["label"].lower().split()) if week_match else ""
        reset = None
        if resets:
            try:
                reset = _reset_time(resets["rest"], now)
            except ZoneError:
                # only the all-models week line needs its reset; a session or per-model line without a usable zone
                # keeps its percentage and drops the reset
                if label == "all models":
                    raise
        if session_match:
            session, session_reset = pct, reset
            continue
        if label == "all models":
            if week is not None and (week, week_reset) != (pct, reset):
                raise ReadingError("conflicting all-models week lines")
            week, week_reset = pct, reset
        else:
            try:
                models[_model_label(label)] = pct
            except ReadingError:
                continue
    if week is None:
        raise ReadingError("no week (all models) percentage in the text")
    if week_reset is None:
        raise ReadingError("no week reset time in the text")
    return {
        "week_pct": week,
        "week_reset": week_reset,
        "session_pct": session,
        "session_reset": session_reset,
        "week_model_pct": models,
    }


# ---- recording ------------------------------------------------------------------------------------------------


def default_totals_fn() -> TotalsFn:
    """Eq totals from `quota_totals`: all projects, and this project's group of directories under ~/.claude/projects."""

    def fetch(since: datetime, until: datetime) -> Totals:
        root = Path.home() / ".claude" / "projects"
        try:
            overrides = pg.load_overrides(pg.default_overrides_path())
            everything = qt.all_projects_eq(root, since, until, overrides)
            names = sorted(e.name for e in root.iterdir() if e.is_dir() and not e.is_symlink())
        except (pg.ProjectsFileError, qt.TotalsError) as exc:
            raise TotalsUnavailable(str(exc)) from None  # both messages are fixed text of ours, safe to print
        roots = pg.known_roots(names)
        key = pg.this_project(Path.cwd(), roots, overrides, root)
        mine = [root / n for n in names if pg.group_of(n, roots, overrides) == key]
        return Totals(everything.eq, qt.project_eq(mine, since, until).eq, everything.skipped)

    return fetch


def build_line(fields: dict[str, Any], ts: datetime, source: str, totals_fn: TotalsFn) -> tuple[str, int]:
    """Validate fields, compute the eq window totals, and return the JSON line and the skipped-file count."""
    week_reset: datetime = fields["week_reset"]
    if week_reset <= ts:
        raise ReadingError("the week reset is not after the reading time")
    if week_reset - ts > WEEK:
        raise ReadingError("the week reset is more than a week after the reading time")
    try:
        got = totals_fn(week_reset - WEEK, ts)
    except TotalsUnavailable:
        raise
    except Exception as exc:
        raise ReadingError("eq totals could not be computed") from exc
    eq_all, eq_project = _finite(got.eq_all, non_negative=True), _finite(got.eq_project, non_negative=True)
    if eq_all is None or eq_project is None:
        raise ReadingError("eq totals are not finite non-negative numbers")
    session_reset = fields["session_reset"]
    row = {
        "ts": iso(ts),
        "session_pct": fields["session_pct"],
        "week_pct": fields["week_pct"],
        "week_model_pct": fields["week_model_pct"],
        "session_reset": iso(session_reset) if session_reset else None,
        "week_reset": iso(week_reset),
        "eq_all": eq_all,
        "eq_project": eq_project,
        "source": source,
    }
    return json.dumps(row, allow_nan=False, separators=(",", ":")), int(got.skipped)


def _fields_from_flags(args: argparse.Namespace) -> dict[str, Any]:
    if args.week_pct is None or args.week_reset is None:
        raise ReadingError("--week-pct and --week-reset are required unless --from-text is given")
    models: dict[str, float] = {}
    for item in args.week_model_pct:
        name, sep, value = item.partition("=")
        if not sep:
            raise ReadingError("--week-model-pct takes name=percent")
        models[_model_label(name)] = parse_pct(value, "model percentage")
    return {
        "week_pct": parse_pct(args.week_pct, "week percentage"),
        "week_reset": parse_time(args.week_reset),
        "session_pct": None if args.session_pct is None else parse_pct(args.session_pct, "session percentage"),
        "session_reset": None if args.session_reset is None else parse_time(args.session_reset),
        "week_model_pct": models,
    }


def cmd_add(args: argparse.Namespace, totals_fn: TotalsFn | None, stdin: IO[str], now: datetime) -> int:
    ts = parse_time(args.ts) if args.ts is not None else now.astimezone(UTC)
    if args.from_text:
        if any(v not in (None, []) for v in (args.week_pct, args.session_pct, args.week_reset, args.session_reset)):
            raise ReadingError("--from-text cannot be combined with reading flags")
        fields = parse_usage_text(stdin.read(MAX_TEXT_CHARS), ts)
        if args.week_model_pct:
            raise ReadingError("--from-text cannot be combined with reading flags")
    else:
        fields = _fields_from_flags(args)
    fetch = totals_fn if totals_fn is not None else default_totals_fn()
    line, skipped = build_line(fields, ts, "text" if args.from_text else "flags", fetch)
    append_line(readings_path(), line)
    if skipped:
        print(f"{skipped} file(s) skipped in transcripts", file=sys.stderr)
    return 0


# ---- calibration ----------------------------------------------------------------------------------------------


def calibrate(
    readings: Sequence[Reading], week_reset: datetime | None = None, *, include_reset: bool = False
) -> Calibration:
    """Pairs of consecutive same-week readings (by time) that qualify, for the given or the newest reading's week."""
    if week_reset is None and readings:
        week_reset = max(readings, key=lambda r: r.ts).week_reset
    if week_reset is None:
        return Calibration(None, [])
    rows = sorted((r for r in readings if abs(r.week_reset - week_reset) <= WEEK_TOLERANCE), key=lambda r: r.ts)
    chain: list[tuple[datetime, float, float, bool]] = []
    if include_reset:
        chain.append((week_reset - WEEK, 0.0, 0.0, True))
    chain += [(r.ts, r.week_pct, r.eq_all, False) for r in rows]
    pairs: list[Pair] = []
    for (ts1, pct1, eq1, reset1), (ts2, pct2, eq2, _) in pairwise(chain):
        if ts2 > ts1 and pct2 > pct1 and eq2 > eq1:
            pairs.append(Pair(ts1, ts2, pct2 - pct1, eq2 - eq1, reset1))
    return Calibration(week_reset, pairs)


def _calibration_json(cal: Calibration, skipped: int) -> dict[str, Any]:
    rate = cal.points_per_eq
    return {
        "week_reset": iso(cal.week_reset) if cal.week_reset else None,
        "points_per_eq": rate,
        "eq_per_point": cal.eq_per_point,
        "coarse": cal.coarse,
        "rests_on": [
            {
                "from_ts": iso(p.from_ts),
                "to_ts": iso(p.to_ts),
                "delta_pct": p.delta_pct,
                "delta_eq": p.delta_eq,
                "from_reset": p.from_reset,
                "coarse": p.coarse,
            }
            for p in (cal.pairs if rate is not None else [])
        ],
        "skipped_lines": skipped,
    }


def _print_calibration(data: dict[str, Any]) -> None:
    if data["points_per_eq"] is None:
        print("no calibration: no qualifying pair of readings in the week (need two readings, percent and eq rising)")
    else:
        flag = " (coarse: a pair spans under 3 points)" if data["coarse"] else ""
        print(f"points per eq: {data['points_per_eq']:.3e} = {data['eq_per_point'] / 1e6:.1f}M eq per point{flag}")
        print(f"week reset: {data['week_reset']}; rests on:")
        for p in data["rests_on"]:
            tags = (" from_reset" if p["from_reset"] else "") + (" coarse" if p["coarse"] else "")
            print(
                f"  {p['from_ts']} -> {p['to_ts']}: +{p['delta_pct']:g} points over {p['delta_eq'] / 1e6:.1f}M eq{tags}"
            )
    if data["skipped_lines"]:
        n = data["skipped_lines"]
        print(f"{n} line{'' if n == 1 else 's'} skipped in the readings file")


def cmd_rate(args: argparse.Namespace) -> int:
    path = readings_path()
    try:
        readings, skipped = load_readings(path)
    except OSError as exc:
        raise ReadingError("cannot read the readings file") from exc
    week_reset = parse_time(args.week_reset) if args.week_reset is not None else None
    data = _calibration_json(calibrate(readings, week_reset, include_reset=args.include_reset), skipped)
    if args.json:
        print(json.dumps(data, allow_nan=False))
    else:
        _print_calibration(data)
    return 0


# ---- CLI ------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="append one /usage reading")
    add.add_argument("--from-text", action="store_true", help="parse /usage text piped on stdin")
    add.add_argument("--week-pct", help="week (all models) percent used, 0..100")
    add.add_argument("--session-pct", help="session percent used, 0..100")
    add.add_argument("--week-reset", help="ISO time of the next weekly reset")
    add.add_argument("--session-reset", help="ISO time of the next session reset")
    add.add_argument("--week-model-pct", action="append", default=[], help="model=percent, repeatable")
    add.add_argument("--ts", help="ISO time of the reading (default: now)")
    rate = sub.add_parser("rate", help="points per eq from the readings of one week")
    rate.add_argument("--json", action="store_true")
    rate.add_argument("--week-reset", help="ISO reset time of the week to use (default: the newest reading's)")
    rate.add_argument("--include-reset", action="store_true", help="use 0%% at the week start as a first reading")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    totals_fn: TotalsFn | None = None,
    stdin: IO[str] | None = None,
    now: datetime | None = None,
) -> int:
    """Run the CLI. `totals_fn(since, until) -> Totals` is the seam for eq totals; None means `quota_totals`."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "add":
            return cmd_add(args, totals_fn, stdin if stdin is not None else sys.stdin, now or datetime.now(UTC))
        return cmd_rate(args)
    except ReadingError as exc:
        print(f"quota_readings: {exc}", file=sys.stderr)
    except OSError:
        print("quota_readings: cannot write the readings file", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
