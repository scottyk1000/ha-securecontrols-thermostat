"""Weekly program (schedule) model for Secure programmers.

Wire format (program.read 22/17 / program.write 21/17): a flat list of 7 days x 6 switch
points, Monday first. Each point is ``{"O": minutes since midnight, "T": value}`` where the
value is the target in deci-°C for heating zones, or 1/0 (on/off) for hot water. Unused
points are ``{"O": 65535, "T": 65535}``.

Home Assistant side ("friendly" format, used by get_schedule / set_schedule)::

    {"monday": [{"start": "06:00", "temperature": 20.0}, ...], ...}   # heating
    {"monday": [{"start": "05:40", "state": "on"}, ...], ...}         # hot water

A point applies from its start time until the next point (which may be on a later day).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_GROUPS = {
    "all": DAYS,
    "weekdays": DAYS[:5],
    "weekend": DAYS[5:],
}
POINTS_PER_DAY = 6
UNUSED = 65535
MINUTES_PER_DAY = 1440
MIN_TEMP_C = 5.0
MAX_TEMP_C = 30.0

KIND_HEATING = "heating"
KIND_HOT_WATER = "hot_water"


class ScheduleError(ValueError):
    """A schedule that the programmer cannot store."""


@dataclass(frozen=True)
class Point:
    minute: int  # minutes since midnight
    value: int  # deci-°C (heating) or 1/0 (hot water)


# ---------------------------------------------------------------------------
# Wire <-> points
# ---------------------------------------------------------------------------


def parse_wire(raw: list[dict[str, Any]] | None) -> dict[str, list[Point]] | None:
    """Wire list -> {day: [Point, ...]} (sentinels dropped). None if unusable."""
    if not isinstance(raw, list) or len(raw) < 7 * POINTS_PER_DAY:
        return None
    week: dict[str, list[Point]] = {}
    for d, day in enumerate(DAYS):
        chunk = raw[d * POINTS_PER_DAY : (d + 1) * POINTS_PER_DAY]
        points = []
        for e in chunk:
            if not isinstance(e, dict):
                continue
            o, t = e.get("O"), e.get("T")
            if o in (None, UNUSED) or t in (None, UNUSED, 255):
                continue
            points.append(Point(int(o), int(t)))
        week[day] = sorted(points, key=lambda p: p.minute)
    return week


def build_wire(week: dict[str, list[Point]], kind: str) -> list[dict[str, int]]:
    """{day: [Point]} -> wire list of exactly 7 x 6 entries."""
    out: list[dict[str, int]] = []
    for day in DAYS:
        points = sorted(week.get(day) or [], key=lambda p: p.minute)
        if not points:
            raise ScheduleError(f"{day}: at least one switch point is required")
        if len(points) > POINTS_PER_DAY:
            raise ScheduleError(f"{day}: at most {POINTS_PER_DAY} switch points per day")
        if kind == KIND_HEATING:
            points = _pad_heating(points)
        entries = [{"O": p.minute, "T": p.value} for p in points]
        while len(entries) < POINTS_PER_DAY:
            entries.append({"O": UNUSED, "T": UNUSED})
        out.extend(entries)
    return out


def _pad_heating(points: list[Point]) -> list[Point]:
    """Heating days always carry 6 points on the device; fill with no-op repeats.

    A repeat is the previous point's temperature one minute later, so it changes nothing.
    """
    points = list(points)
    while len(points) < POINTS_PER_DAY:
        for i, p in enumerate(points):
            nxt = points[i + 1].minute if i + 1 < len(points) else MINUTES_PER_DAY
            if nxt - p.minute >= 2:  # noqa: PLR2004
                points.insert(i + 1, Point(p.minute + 1, p.value))
                break
        else:
            raise ScheduleError("Switch points are too close together to store")
    return points


# ---------------------------------------------------------------------------
# Friendly format <-> points
# ---------------------------------------------------------------------------


def to_friendly(week: dict[str, list[Point]], kind: str, *, compact: bool = True) -> dict:
    """Points -> {"monday": [{"start": "06:00", "temperature": 20.0}], ...}.

    ``compact`` drops points that repeat the previous point's value (padding), so what
    comes out can be edited and passed straight back to set_schedule.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for day in DAYS:
        items = []
        prev = None
        for p in week.get(day) or []:
            if compact and prev is not None and p.value == prev:
                continue
            prev = p.value
            item: dict[str, Any] = {"start": _fmt_minute(p.minute)}
            if kind == KIND_HOT_WATER:
                item["state"] = "on" if p.value == 1 else "off"
            else:
                item["temperature"] = p.value / 10
            items.append(item)
        result[day] = items
    return result


def from_friendly(periods: list[dict[str, Any]], kind: str) -> list[Point]:
    """One day's friendly periods -> validated points."""
    if not isinstance(periods, list) or not periods:
        raise ScheduleError("Each day needs a list of at least one period")
    points: list[Point] = []
    for period in periods:
        if not isinstance(period, dict) or "start" not in period:
            raise ScheduleError(f"Each period needs a 'start' time: {period!r}")
        minute = _parse_minute(period["start"])
        if kind == KIND_HOT_WATER:
            state = period.get("state")
            if isinstance(state, bool):
                value = int(state)
            elif str(state).lower() in ("on", "off"):
                value = 1 if str(state).lower() == "on" else 0
            else:
                raise ScheduleError(f"Hot water periods need state: on/off ({period!r})")
        else:
            if "temperature" not in period:
                raise ScheduleError(f"Heating periods need a temperature ({period!r})")
            temp = float(period["temperature"])
            if not MIN_TEMP_C <= temp <= MAX_TEMP_C:
                raise ScheduleError(f"Temperature {temp} is outside {MIN_TEMP_C}-{MAX_TEMP_C} °C")
            if round(temp * 2) != temp * 2:
                raise ScheduleError(f"Temperature {temp} must be in 0.5 °C steps")
            value = round(temp * 10)
        points.append(Point(minute, value))
    minutes = [p.minute for p in points]
    if minutes != sorted(minutes) or len(set(minutes)) != len(minutes):
        raise ScheduleError("Start times must be in order and not repeat")
    if len(points) > POINTS_PER_DAY:
        raise ScheduleError(f"At most {POINTS_PER_DAY} periods per day")
    return points


def expand_days(days: list[str] | str) -> list[str]:
    """['weekdays'] / 'saturday' / ['mon', 'tue'] -> canonical day names."""
    if isinstance(days, str):
        days = [days]
    out: list[str] = []
    for d in days:
        key = str(d).strip().lower()
        if key in DAY_GROUPS:
            out.extend(DAY_GROUPS[key])
            continue
        match = [day for day in DAYS if day.startswith(key[:3])] if len(key) >= 3 else []  # noqa: PLR2004
        if len(match) != 1:
            raise ScheduleError(f"Unknown day '{d}'")
        out.append(match[0])
    return list(dict.fromkeys(out))


def merge(
    current: dict[str, list[Point]], changes: dict[str, list[Point]]
) -> dict[str, list[Point]]:
    """Replace only the changed days."""
    return {day: changes.get(day, current.get(day, [])) for day in DAYS}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def value_at(week: dict[str, list[Point]], now: datetime) -> int | None:
    """Program value in force at ``now`` (carries over from earlier days)."""
    minute = now.hour * 60 + now.minute
    today = now.weekday()
    passed = [p for p in week.get(DAYS[today]) or [] if p.minute <= minute]
    if passed:
        return passed[-1].value
    for back in range(1, 8):
        earlier = week.get(DAYS[(today - back) % 7]) or []
        if earlier:
            return earlier[-1].value
    return None


def segments(
    week: dict[str, list[Point]], start: datetime, end: datetime
) -> list[tuple[datetime, datetime, int]]:
    """Contiguous (start, end, value) blocks overlapping [start, end), merged when equal.

    ``start`` must be timezone aware; blocks use its timezone (local wall-clock times).
    """
    tz = start.tzinfo
    day = start.date() - timedelta(days=1)
    midnight = datetime.combine(day, time.min, tz)
    initial = value_at(week, midnight - timedelta(minutes=1))
    changes: list[tuple[datetime, int]] = [] if initial is None else [(midnight, initial)]
    while datetime.combine(day, time.min, tz) < end:
        base = datetime.combine(day, time.min, tz)
        for p in week.get(DAYS[day.weekday()]) or []:
            if not changes or changes[-1][1] != p.value:
                changes.append((base + timedelta(minutes=p.minute), p.value))
        day += timedelta(days=1)
    out = []
    for i, (at, value) in enumerate(changes):
        until = changes[i + 1][0] if i + 1 < len(changes) else end
        if until > start and at < end:
            out.append((max(at, start), min(until, end), value))
    return out


def _fmt_minute(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _parse_minute(value: Any) -> int:
    if isinstance(value, time):
        return value.hour * 60 + value.minute
    text = str(value).strip()
    try:
        parts = [int(x) for x in text.split(":")]
    except ValueError as err:
        raise ScheduleError(f"Invalid time '{value}' (use HH:MM)") from err
    if len(parts) < 2 or not (0 <= parts[0] < 24 and 0 <= parts[1] < 60):  # noqa: PLR2004
        raise ScheduleError(f"Invalid time '{value}' (use HH:MM)")
    return parts[0] * 60 + parts[1]
