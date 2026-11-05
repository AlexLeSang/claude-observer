"""ISO-8601 instant helpers shared by `quota_latest` and `excerpt_project`.

Pure: no file, no clock, no environment.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

_INSTANT = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?(?:(Z)|([+-])([0-9]{2}):([0-9]{2}))",
    re.ASCII,
)


def parse_instant(value: object) -> datetime | None:
    """An ISO-8601 instant with a zone (the engine's `Z` form or an offset) as an aware UTC datetime, or None.
    Strict: ASCII digits only, a real calendar date and time."""
    if not isinstance(value, str):
        return None
    match = _INSTANT.fullmatch(value)
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, _z, sign, off_h, off_m = match.groups()
    try:
        micro = int((fraction or "0").ljust(6, "0")[:6])
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=UTC)
        if sign is not None:
            if int(off_h) > 23 or int(off_m) > 59:
                return None
            offset = timedelta(hours=int(off_h), minutes=int(off_m))
            moment = moment - offset if sign == "+" else moment + offset
    except (ValueError, OverflowError):
        return None
    return moment


def format_instant(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalise_instant(value: object) -> str | None:
    """The canonical form (UTC, `Z`, milliseconds) of an instant, or None when it is not one."""
    moment = parse_instant(value)
    return None if moment is None else format_instant(moment)
