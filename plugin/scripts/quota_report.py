"""This-project report of usage in eq and in weekly-quota points.

    quota_report.py [--since ISO] [--until ISO] [--sprint-pattern REGEX] [--until-pct N] [--json]
    quota_report.py --pace [--json]

Reads ONLY this project's transcript directories under `~/.claude/projects` (the group of the working
directory: main checkout, worktree and scratchpad slugs, see `project_groups`). It never opens another project's
directory and shows no other project's share. Usage is weighted like the usage report (`eq_weight`) and listed by
day (UTC), work item, role and model, in eq and, when the readings file gives a calibration, in points
(eq times points per eq). A work item is the ticket a dispatch names where one is detected (a convention of this
repo, optional), otherwise the session. A grouping by label appears only with `--sprint-pattern` or when the default
pattern matches a dispatch description; usage with no label is `unlabelled`, so labels sum to the total.

The window starts at `--since`, else at the last weekly reset of the newest reading (its reset time minus 7 days);
with neither the command exits 1. Without a qualifying pair of readings only eq is printed.

`--pace` shows, for the five-hour and seven-day window of the latest quota reading on this machine
(`quota_latest.py`), the percent used next to the percent of the window that has elapsed (from `resets_at`). It reads
no transcript and takes no other option. For the five-hour window it adds one plain linear line, labelled as
arithmetic: the used percent divided by the elapsed share of the window gives the percent at the reset and, when that
passes 100, the time of day (UTC) it reaches 100. Beside it is the backtest result: no method beat "no growth" on real
data, and this pace overshot, so on them it was worse than no growth. Where the arithmetic would give a wrong
number (a window that just began, a reading older than its limit, a reset that has passed, no reset time, a percent above 100) the line says
"no line". The seven-day window stays display only. Nothing from a prompt, path or error is printed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import signal
import statistics
import sys
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_usage_report as aur
import project_groups as pg
import quota_latest as ql
import quota_readings as qr
import transcript_iterator as ti
from model_prices import usage_tokens

UTC = timezone.utc
WEEK = timedelta(days=7)
DEFAULT_PATTERN = r"\b[WS]\d+\b"
UNLABELLED = "unlabelled"
FROM_READING = "since the last weekly reset of the newest reading"  # the window source when --since is not given
PATTERN_SECONDS = (
    1.0  # one --sprint-pattern search over one description may take this long, each description has its own
)
MAX_DESCRIPTION = 500  # a dispatch description is read up to here when matching a label
MAX_KEY = 80  # a label, role or session name is cut here for display
# The backtest result printed beside the linear pace line (openspec change add-quota-latest-and-forecast, design D4):
# 37 backtested five-hour windows, no method beat "no growth" (the current percent held to the reset).
BACKTEST_WINDOWS = 37
BACKTEST_TEXT = (
    f"backtest on {BACKTEST_WINDOWS} five-hour windows: no method beat no growth, "
    "and this pace overshot, so on them it was worse than no growth"
)
PACE_LABEL = "arithmetic, not a prediction"
MIN_ELAPSED_PERCENT = 1.0  # under this (3 minutes of five hours) one percent of use projects to 100 or more: no line
PACE_KIND = "five_hour"  # the seven-day window stays display only
CAVEAT = (
    "/usage also counts other devices, claude.ai and other projects, so the real remaining room is smaller "
    "than these points suggest."
)


_TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


class ReportError(Exception):
    """A failure whose message is ours and safe to print."""


def _clean(text: str) -> str:
    """Printable, single-line, bounded: a role or description comes from a file that may be hostile."""
    flat = "".join(c if c.isprintable() else "?" for c in text)
    return flat[:MAX_KEY]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime) -> str:
    return qr.iso(ts)


class _PatternTooSlowError(Exception):
    pass


@contextlib.contextmanager
def _pattern_time_limit() -> Iterator[None]:
    """Stops a search that runs past `PATTERN_SECONDS`. The pattern is the operator's own and is run over their own
    descriptions, but `(a+)+$` would still hang the report; `re` does not time out by itself. Needs SIGALRM and the main
    thread: elsewhere there is no limit (the pattern is still bounded to `MAX_DESCRIPTION` characters)."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def expired(_signum: int, _frame: object) -> None:
        raise _PatternTooSlowError

    previous = signal.signal(signal.SIGALRM, expired) or signal.SIG_DFL  # None: a handler Python did not install
    signal.setitimer(signal.ITIMER_REAL, PATTERN_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _label_of(pattern: re.Pattern[str], description: str | None) -> str | None:
    """The first match of `pattern` in the description, or None (no description, no match, an empty match).
    A search that takes too long is a `ReportError`, not a hang."""
    if not description:
        return None
    try:
        with _pattern_time_limit():
            found = pattern.search(description[:MAX_DESCRIPTION])
    except _PatternTooSlowError:
        raise ReportError(
            "--sprint-pattern took too long on a dispatch description (catastrophic backtracking?)"
        ) from None
    if found is None or not found.group(0).strip():
        return None
    return _clean(found.group(0))


def resolve_project(root: Path) -> tuple[str, list[Path]]:
    """The project key of the working directory and the directories under `root` that belong to it, and
    no others. Only names are listed here; a symlinked directory is never followed."""
    try:
        names = sorted(e.name for e in root.iterdir() if e.is_dir() and not e.is_symlink())
    except OSError:
        names = []
    overrides = pg.load_overrides(pg.default_overrides_path())
    roots = pg.known_roots(names)
    key = pg.this_project(Path.cwd(), roots, overrides, root)
    return key, [root / n for n in names if pg.group_of(n, roots, overrides) == key]


def _require_listable(dirs: list[Path]) -> None:
    """A directory of this project that cannot be listed is a read error: `discover_transcripts` globs it and a
    permission failure there looks like an empty directory."""
    try:
        for directory in dirs:
            with os.scandir(directory):
                pass
    except OSError:
        raise ReportError("cannot read the transcripts of this project") from None


class _Sums:
    """Weights per key, summed exactly at the end."""

    def __init__(self) -> None:
        self.weights: defaultdict[str, list[float]] = defaultdict(list)

    def add(self, key: str, weight: float) -> None:
        self.weights[key].append(weight)

    def eq(self, key: str) -> float:
        return math.fsum(self.weights[key])


def _in_window(ts: datetime, since: datetime | None, until: datetime | None) -> bool:
    return not ((since is not None and ts < since) or (until is not None and ts > until))


def collect(
    dirs: list[Path], since: datetime | None, until: datetime | None, pattern: re.Pattern[str]
) -> dict[str, Any]:
    """Weigh every deduplicated message of the project's directories that falls in the window.

    Returns plain data: per message (day, item, kind, role, model family, label, weight) in `messages`, the
    unpriced models and the number of undated messages. Dispatch metadata is read once per transcript."""
    candidates: list[tuple[ti.TranscriptContext, tuple[ti.AssistantMessageEvent, ...]]] = []
    skipped_files = 0
    for directory in dirs:
        for context in ti.discover_transcripts(directory, skip_links=False):
            if not pg.regular_file_inside(context.transcript_path, directory):
                skipped_files += 1  # a link may lead into another project; never read through it
                continue
            events = tuple(e for e in ti.iter_events(context) if isinstance(e, ti.AssistantMessageEvent))
            if events:
                candidates.append((context, events))
    messages: list[dict[str, Any]] = []
    unpriced: dict[str, dict[str, int]] = {}
    undated = 0
    for context, owned in ti.dedupe_across_files(candidates):
        if not owned:
            continue
        ticket = aur.ticket_of_dispatch(context)
        item, kind = (f"#{ticket}", "ticket") if ticket != aur.UNATTRIBUTED else (_clean(context.session_id), "session")
        role = _clean(aur.role_of(context))
        description = aur.meta_description(context)
        label = _label_of(pattern, description)
        meta_model = context.meta_model if isinstance(context.meta_model, str) else None
        for event in owned:
            stamp = aur.parse_timestamp(event.timestamp)
            if stamp is None:
                undated += 1  # outside any bounded window; counted so the gap is visible
                continue
            if not _in_window(stamp, since, until):
                continue
            model = event.model or meta_model
            if not aur.is_priced(model):
                if event.usage is not None:
                    counts = unpriced.setdefault(_clean(model or aur.NO_MODEL), {"requests": 0, "tokens": 0})
                    counts["requests"] += 1
                    counts["tokens"] += int(sum(usage_tokens(event.usage, k) for k in _TOKEN_KEYS))
                continue
            weight = aur.eq_weight(event.usage, model)
            if not (math.isfinite(weight) and weight >= 0):
                continue
            messages.append(
                {
                    "ts": stamp,
                    "day": stamp.date().isoformat(),
                    "item": item,
                    "kind": kind,
                    "role": role,
                    "model": aur.model_family(model),
                    "label": label,
                    "eq": weight,
                }
            )
    return {"messages": messages, "unpriced": unpriced, "undated": undated, "skipped_files": skipped_files}


def _points(eq: float, rate: float | None) -> float | None:
    return None if rate is None else eq * rate


def _group(messages: list[dict[str, Any]], field: str, rate: float | None) -> list[dict[str, Any]]:
    sums = _Sums()
    for m in messages:
        sums.add(m[field] or UNLABELLED, m["eq"])
    return [{"key": k, "eq": sums.eq(k), "points": _points(sums.eq(k), rate)} for k in sums.weights]


def make_forecast(
    messages: list[dict[str, Any]],
    by_item: list[dict[str, Any]],
    rate: float | None,
    readings: list[qr.Reading],
    since: datetime | None,
    until: datetime | None,
    now: datetime,
    until_pct: float | None,
) -> dict[str, Any]:
    """Burn rate, the current week %, and how many work items fit before `until_pct`.

    The unit is the work item (a ticket where detected, else the session). The current week % is the newest
    reading's plus this project's points since it; other projects' use after that reading is unknown."""
    reasons: list[str] = []
    f: dict[str, Any] = {
        "as_of": None,
        "current_pct": None,
        "burn_rate": None,
        "items": None,
        "until_pct": until_pct,
        "items_fit": None,
        "days_to": None,
        "reasons": reasons,
    }
    if rate is None:
        reasons.append("no calibration")
        return f
    # burn rate: this project's points per day over the last 7 days, or since the window start if shorter
    end = min(until, now) if until else now  # a future --until does not move the window past the clock
    start = end - WEEK if since is None else max(since, end - WEEK)
    days = (end - start).total_seconds() / 86400
    if days > 0:
        points = math.fsum(m["eq"] for m in messages if start <= m["ts"] <= end) * rate
        f["burn_rate"] = {"points_per_day": points / days, "points": points, "days": days}
    else:
        reasons.append("the window is empty: no burn rate")
    # the current week %: the newest reading plus this project's points since it
    newest = max(readings, key=lambda r: r.ts)
    f["as_of"] = _iso(newest.ts)
    if newest.week_reset <= now:
        reasons.append("the newest reading is from an earlier week: no current week %")
    elif (since is None or since <= newest.ts) and (until is None or until >= newest.ts):
        f["current_pct"] = newest.week_pct + math.fsum(m["eq"] for m in messages if m["ts"] > newest.ts) * rate
    else:
        reasons.append("the window does not cover the time since the newest reading: no current week %")
    costs = [r["points"] for r in by_item if r["points"] is not None]
    if not costs:
        reasons.append("no work items in the window")
        return f
    median = statistics.median(costs)
    f["items"] = {"count": len(costs), "median_points": median, "min_points": min(costs), "max_points": max(costs)}
    current = f["current_pct"]
    if until_pct is None:
        reasons.append("no --until-pct given: no items_fit and no days_to")
    elif current is not None:
        room = until_pct - current
        if room <= 0:
            f["items_fit"], f["days_to"] = 0, 0.0
        else:
            if median <= 0:
                reasons.append("the median work item costs zero points: no items_fit")
            elif math.isfinite(room / median):
                f["items_fit"] = math.floor(room / median)
            else:
                reasons.append("items_fit is not a usable count")
            burn = f["burn_rate"]
            if burn is not None and burn["points_per_day"] > 0:
                days_to = room / burn["points_per_day"]
                if math.isfinite(days_to):
                    f["days_to"] = days_to
                else:
                    reasons.append("days_to is not a usable number")
            elif burn is not None:
                reasons.append("no burn in the period: no days_to")
    return f


def build_data(
    collected: dict[str, Any],
    *,
    project: str,
    since: datetime | None,
    until: datetime | None,
    window_source: str,
    cal: qr.Calibration,
    readings: list[qr.Reading],
    skipped_lines: int,
    pattern_given: bool,
    now: datetime | None = None,
    until_pct: float | None = None,
) -> dict[str, Any]:
    messages = collected["messages"]
    rate = cal.points_per_eq
    total = math.fsum(m["eq"] for m in messages)
    if rate is not None and not math.isfinite(total * rate):
        rate = None  # points would overflow a float; no figure is better than a bare inf in the JSON

    def ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda r: (-r["eq"], r["key"]))

    by_day = sorted(_group(messages, "day", rate), key=lambda r: r["key"])
    by_role = ordered(_group(messages, "role", rate))
    by_model = ordered(_group(messages, "model", rate))
    items: dict[str, dict[str, Any]] = {}
    for m in messages:
        entry = items.setdefault(m["item"], {"kind": m["kind"], "weights": [], "roles": defaultdict(list)})
        entry["weights"].append(m["eq"])
        entry["roles"][m["role"]].append(m["eq"])
    by_item = []
    for key, entry in items.items():
        eq = math.fsum(entry["weights"])
        roles = {
            r: {"eq": math.fsum(w), "points": _points(math.fsum(w), rate)} for r, w in sorted(entry["roles"].items())
        }
        by_item.append({"key": key, "kind": entry["kind"], "eq": eq, "points": _points(eq, rate), "roles": roles})
    by_item = ordered(by_item)

    week_readings: list[qr.Reading] = []
    if rate is not None and cal.week_reset is not None:
        used = {t for p in cal.pairs for t in (p.from_ts, p.to_ts)}
        week_readings = sorted(
            (r for r in readings if r.ts in used and abs(r.week_reset - cal.week_reset) <= qr.WEEK_TOLERANCE),
            key=lambda r: r.ts,
        )
    notices: list[str] = []
    if window_source == FROM_READING and readings and since is not None:
        newest = max(readings, key=lambda r: r.ts)
        current = now or _now()
        # the window ends at --until, so one that ends before the reset is not over a week
        if newest.week_reset <= current and (until is None or newest.week_reset < until):
            notices.append(
                f"the newest reading's week reset ({_iso(newest.week_reset)}) is already past, so this window from "
                f"{_iso(since)} covers more than one week; give --since for the week you mean"
            )
    data: dict[str, Any] = {
        "window": {
            "since": _iso(since) if since else None,
            "until": _iso(until) if until else None,
            "source": window_source,
        },
        "project": pg.project_name(project),
        "notices": notices,
        "points_per_eq": rate,
        "coarse": cal.coarse if rate is not None else False,
        "rests_on": [{"ts": _iso(r.ts), "week_pct": r.week_pct} for r in week_readings],
        "readings_skipped_lines": skipped_lines,
        "caveat": CAVEAT,
        "totals": {"eq": total, "points": _points(total, rate)},
        "by_day": by_day,
        "by_item": by_item,
        "by_role": by_role,
        "by_model": by_model,
        "forecast": make_forecast(messages, by_item, rate, readings, since, until, now or _now(), until_pct),
        "unpriced": collected["unpriced"],
        "undated_messages": collected["undated"],
        "skipped_files": collected["skipped_files"],
    }
    if pattern_given or any(m["label"] for m in messages):
        data["by_label"] = ordered(_group(messages, "label", rate))
    return data


def _render_forecast(f: dict[str, Any]) -> list[str]:
    burn, items = f["burn_rate"], f["items"]
    if f["as_of"] is None and burn is None and items is None:
        reason = f["reasons"][0] if f["reasons"] else "nothing to forecast"
        return [f"Forecast: no forecast: {reason}"]
    head = "Forecast"
    if f["current_pct"] is not None:
        head += f" (from the reading of {f['as_of']}): current {f['current_pct']:.1f}%"
    lines = [head]
    if burn is not None:
        lines.append(f"  burn {burn['points_per_day']:.3f} points/day over {burn['days']:.1f} days")
    if items is not None:
        lines.append(
            f"  {items['count']} work item{'' if items['count'] == 1 else 's'}, median {items['median_points']:.3f} "
            f"points (min {items['min_points']:.3f}, max {items['max_points']:.3f})"
        )
    if f["items_fit"] is not None:
        lines.append(f"  {f['items_fit']} more work items fit before {f['until_pct']:g}%")
    if f["days_to"] is not None:
        lines.append(f"  {f['days_to']:.1f} days to {f['until_pct']:g}% at that burn")
    lines.extend(f"  note: {r}" for r in f["reasons"])
    return lines


def render(data: dict[str, Any]) -> str:
    """The table: every figure is the one in `data`, formatted."""
    rate = data["points_per_eq"]
    lines = [f"Project {data['project']}: usage {data['window']['source']}"]
    window = data["window"]
    lines.append(f"Window: {window['since'] or 'start'} to {window['until'] or 'now'}")
    lines.extend(f"note: {n}" for n in data["notices"])

    def fig(row: dict[str, Any]) -> str:
        text = f"{row['eq']:>16,.1f}"
        return text if row["points"] is None else f"{text} {row['points']:>10.3f}"

    head = "eq" if rate is None else "eq        points"
    lines.append(f"Total: {fig(data['totals']).strip()}  ({head.strip()})")
    if rate is None:
        lines.append("no calibration: points not shown")
        lines.append("rests on: none")
    else:
        lines.append(f"points per eq: {rate:.3e} = {1 / rate / 1e6:.1f}M eq per point")
        lines.append("rests on: " + "; ".join(f"{r['ts']} {r['week_pct']:g}%" for r in data["rests_on"]))
        if data["coarse"]:
            lines.append("coarse: a pair of readings spans under 3 points, the figure may be off by a few percent")
    if data["readings_skipped_lines"]:
        n = data["readings_skipped_lines"]
        lines.append(f"{n} line{'' if n == 1 else 's'} skipped in the readings file")
    for title, group in (
        ("By day:", "by_day"),
        ("By work item:", "by_item"),
        ("By role:", "by_role"),
        ("By model family:", "by_model"),
        ("By label:", "by_label"),
    ):
        if group not in data:
            continue
        lines.append(title)
        for row in data[group]:
            lines.append(f"  {row['key']:<24} {fig(row)}")
            if group == "by_item":
                for role, split in row["roles"].items():
                    lines.append(f"    - {role:<22} {fig(split)}")
    if data["undated_messages"]:
        lines.append(f"{data['undated_messages']} messages without a timestamp were left out")
    if data["skipped_files"]:
        lines.append(f"{data['skipped_files']} file(s) skipped in transcripts (links are not followed)")
    for model, counts in sorted(data["unpriced"].items()):
        lines.append(f"unpriced model, not counted: {model} ({counts['requests']} requests, {counts['tokens']} tokens)")
    lines.extend(_render_forecast(data["forecast"]))
    lines.append(f"Caveat: {data['caveat']}")
    return "\n".join(lines)


AHEAD_TEXT = "the reset is more than one window ahead of the reading"


def _no_line(reason: str) -> dict[str, Any]:
    return {"basis": PACE_LABEL, "line": False, "reason": reason, "percent_at_reset": None, "reaches_100_at": None}


def linear_pace(window: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The linear pace of one window: the percent at the reset and the instant of 100 at the same rate, from the
    used percent and the elapsed time only. Anything that would make the figure wrong gives `line` False and a
    fixed reason (never text from the file)."""
    if window["kind"] != PACE_KIND:
        return _no_line("display only")
    used, elapsed, resets = window["used_percent"], window["elapsed_percent"], window["resets_at"]
    if window["state"] == "reset":
        return _no_line("the window has reset")
    end = ql.parse_instant(resets)
    if end is None:
        return _no_line("no reset time")
    if elapsed is None:
        return _no_line(AHEAD_TEXT)
    if window["state"] != "fresh":
        return _no_line("the reading is older than its limit")
    if used is None or isinstance(used, bool) or not 0 <= used <= 100:
        return _no_line("the used percent is not between 0 and 100")
    if not math.isfinite(elapsed) or elapsed >= 100:
        return _no_line("the window is over")
    if elapsed < MIN_ELAPSED_PERCENT:
        return _no_line("the window has only just begun")
    at_reset = round(used * 100 / elapsed, 1)  # the printed figure decides, so the text cannot contradict itself
    reaches: str | None = None
    if at_reset >= 100:  # also true at 100 used, which reaches 100 now
        length = ql.WINDOW_LENGTH[window["kind"]]
        seconds = (100 - used) / used * (elapsed / 100 * length.total_seconds())
        reaches = ql.format_instant(now + timedelta(seconds=seconds))  # whole milliseconds, like every other instant
    return {
        "basis": PACE_LABEL,
        "line": True,
        "reason": None,
        "percent_at_reset": at_reset,
        "reaches_100_at": reaches,
    }


def build_pace(now: datetime, path: Path | None = None) -> dict[str, Any]:
    """Used percent and elapsed percent per window of the latest reading, both as of its `observed_at`. A window without `resets_at` has
    `elapsed_percent` None; a window whose reset time has passed is `reset` and shows no used percent."""
    latest = ql.read_state(path or ql.latest_path(), now)
    if latest["state"] != "ok":
        return {"state": "unknown", "windows": []}
    observed = ql.parse_instant(latest["observed_at"]) or now
    windows = []
    for window in latest["windows"]:
        if window["kind"] not in ql.WINDOW_LENGTH:
            continue
        # The used percent is the value at `observed_at`, so the elapsed percent and the 100% instant are taken on
        # that clock too (a reading up to the age limit old would otherwise pair an old used with a new elapsed).
        # A reset window has no used percent: it keeps `now`.
        clock = now if window["state"] == "reset" else observed
        row = {
            "kind": window["kind"],
            "used_percent": window["percent"],
            "elapsed_percent": ql.elapsed_percent(window["kind"], window["resets_at"], clock),
            "resets_at": window["resets_at"],
            "state": window["state"],
        }
        row["linear_pace"] = linear_pace(row, clock)
        windows.append(row)
    return {
        "state": "ok",
        "observed_at": latest["observed_at"],
        "age_seconds": latest["age_seconds"],
        "backtest": {"five_hour_windows": BACKTEST_WINDOWS, "beat_no_growth": False},
        "windows": windows,
    }


def _clock(stamp: str, now: datetime) -> str:
    moment = ql.parse_instant(stamp)
    if moment is None:
        return "?"
    text = moment.strftime("%H:%M UTC")
    return text if moment.date() == now.date() else f"{text} on {moment.date().isoformat()}"


def render_pace(pace: dict[str, Any], now: datetime) -> str:
    if pace["state"] != "ok":
        return "Pace: unknown (no usable quota reading on this machine)"
    lines = [f"Pace (reading of {pace['observed_at']}, age {pace['age_seconds']:g} s): used % against elapsed %"]
    for w in pace["windows"]:
        used = "reset, no percent" if w["used_percent"] is None else f"used {w['used_percent']:g}% ({w['state']})"
        if w["elapsed_percent"] is not None:
            elapsed = f"elapsed {w['elapsed_percent']:.1f}%"
        elif ql.parse_instant(w["resets_at"]) is None:
            elapsed = "elapsed n/a (no reset time)"
        else:
            elapsed = f"elapsed n/a ({AHEAD_TEXT})"
        lines.append(f"  {w['kind']}: {used}, {elapsed}")
        lin = w["linear_pace"]
        if lin["reason"] == "display only":
            continue
        if not lin["line"]:
            lines.append(f"    linear pace ({lin['basis']}): no line, {lin['reason']}")
            continue
        text = f"    linear pace ({lin['basis']}): at this pace you reach {lin['percent_at_reset']:g}% at reset"
        if lin["reaches_100_at"] is None:
            text += " / 100% not before the reset"
        else:
            text += f" / 100% at {_clock(lin['reaches_100_at'], now)}"
        lines.append(f"{text}; {BACKTEST_TEXT}")
    if not pace["windows"]:
        lines.append("  no five_hour or seven_day window in the reading")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--since", help="ISO-8601 start of the window (default: the last weekly reset of the newest reading)"
    )
    parser.add_argument("--until", help="ISO-8601 end of the window (default: now)")
    parser.add_argument(
        "--sprint-pattern", help=f"regex of a label in dispatch descriptions; default {DEFAULT_PATTERN} if it matches"
    )
    parser.add_argument(
        "--until-pct",
        help="a week percentage (above 0, up to 100): how many work items fit before it and the days to it",
    )
    parser.add_argument("--json", action="store_true", help="print the planner view as JSON")
    parser.add_argument(
        "--pace",
        action="store_true",
        help="used %% against elapsed %% of the five-hour and seven-day window, from the latest quota reading",
    )
    return parser


def _parse_flag_time(raw: str | None, flag: str) -> datetime | None:
    if raw is None:
        return None
    try:
        return qr.parse_time(raw)  # no zone means UTC, as the days are grouped by UTC; `aur` would read it as local
    except qr.ReadingError:
        raise ReportError(f"{flag} is not a valid ISO-8601 timestamp") from None


def _parse_until_pct(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip()
    try:
        # non-ASCII digits and the underscores `float()` would take ("5_0") are not accepted
        value = float(text) if text.isascii() and "_" not in text else math.nan
    except ValueError:
        value = math.nan
    if not 0 < value <= 100:
        raise ReportError("--until-pct must be a number above 0 and at most 100")
    return value


def run(args: argparse.Namespace) -> str:
    if args.pace:
        if any(v is not None for v in (args.since, args.until, args.sprint_pattern, args.until_pct)):
            raise ReportError("--pace takes no option except --json")
        now = _now()
        pace = build_pace(now)
        return json.dumps(pace, indent=2, allow_nan=False) if args.json else render_pace(pace, now)
    until_pct = _parse_until_pct(args.until_pct)
    since = _parse_flag_time(args.since, "--since")
    until = _parse_flag_time(args.until, "--until")
    pattern: re.Pattern[str] | None = None
    if args.sprint_pattern is not None:
        try:
            pattern = re.compile(args.sprint_pattern)
        except re.error:
            raise ReportError("--sprint-pattern is not a valid regular expression") from None
    try:
        readings, skipped_lines = qr.load_readings(qr.readings_path())
    except OSError:
        raise ReportError("cannot read the readings file") from None
    cal = qr.calibrate(readings)
    window_source = "since --since"
    if since is None:
        if not readings:
            raise ReportError("no readings and no --since: give --since or record a reading with quota_readings add")
        since = max(readings, key=lambda r: r.ts).week_reset - WEEK
        window_source = FROM_READING
    root = Path.home() / ".claude" / "projects"
    key, dirs = resolve_project(root)  # a malformed projects.json raises ProjectsFileError, named by `main`
    _require_listable(dirs)
    if not any(ti.discover_transcripts(d, skip_links=False) for d in dirs):
        raise ReportError("no transcripts found for this project")
    collected = collect(dirs, since, until, pattern or re.compile(DEFAULT_PATTERN))
    data = build_data(
        collected,
        project=key,
        since=since,
        until=until,
        window_source=window_source,
        cal=cal,
        readings=readings,
        skipped_lines=skipped_lines,
        pattern_given=pattern is not None,
        until_pct=until_pct,
    )
    return json.dumps(data, indent=2, allow_nan=False) if args.json else render(data)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(run(args))
    except (ReportError, pg.ProjectsFileError) as exc:
        print(f"quota_report: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("quota_report: cannot read the transcripts of this project", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
