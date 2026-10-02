"""Schedule maths for NextPull jobs. Pure functions: no I/O, no threads, naive *local* datetimes.

A job schedule is ``{"days": [0..6] (0 = Monday), "time": "HH:MM", "stop_by": "HH:MM" or ""}``.
A *slot* is one nominal start (a day + the time). The engine asks :func:`due_slots` which slots passed since its
last check and decides, per slot, whether to run it, catch it up late, or record it as missed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


def parse_hhmm(text: str) -> tuple[int, int]:
    """'02:00' -> (2, 0). Raises ValueError for anything else."""
    parts = str(text).strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() and len(part) in (1, 2) for part in parts):
        raise ValueError(f"Time must look like HH:MM, got {text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time out of range: {text!r}")
    return hour, minute


def clean_schedule(raw: Any) -> dict[str, Any]:
    """Whitelist and validate a schedule dict (days sorted/unique, time and stop_by valid)."""
    raw = raw if isinstance(raw, dict) else {}
    days = sorted({int(day) for day in raw.get("days", ALL_DAYS) if str(day).lstrip("-").isdigit() and 0 <= int(day) <= 6})
    if not days:
        raise ValueError("Pick at least one day")
    hour, minute = parse_hhmm(raw.get("time", "02:00"))
    stop_by = str(raw.get("stop_by") or "").strip()
    if stop_by:
        sh, sm = parse_hhmm(stop_by)
        stop_by = f"{sh:02d}:{sm:02d}"
    return {"days": days, "time": f"{hour:02d}:{minute:02d}", "stop_by": stop_by}


def _slot_on(day: datetime, hour: int, minute: int) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def occurrences(schedule: dict[str, Any], after: datetime, until: datetime) -> list[datetime]:
    """Slots strictly after ``after`` and at or before ``until``, oldest first."""
    if until <= after:
        return []
    hour, minute = parse_hhmm(schedule["time"])
    days = set(schedule["days"])
    result: list[datetime] = []
    cursor = after.replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor <= until:
        if cursor.weekday() in days:
            slot = _slot_on(cursor, hour, minute)
            if after < slot <= until:
                result.append(slot)
        cursor += timedelta(days=1)
    return result


def next_run(schedule: dict[str, Any], after: datetime) -> datetime | None:
    """First slot strictly after ``after`` (searches the next 8 days)."""
    slots = occurrences(schedule, after, after + timedelta(days=8))
    return slots[0] if slots else None


def window_end(schedule: dict[str, Any], slot: datetime) -> datetime | None:
    """When a run that belongs to ``slot`` must stop (``stop_by``), or None. A stop_by at or before the start time
    means the next day (e.g. 22:00 -> 06:00)."""
    if not schedule.get("stop_by"):
        return None
    hour, minute = parse_hhmm(schedule["stop_by"])
    end = _slot_on(slot, hour, minute)
    if end <= slot:
        end += timedelta(days=1)
    return end


def classify_slot(slot: datetime, now: datetime, *, tick_seconds: int, catch_up: bool, catch_up_hours: float, window: datetime | None) -> str:
    """'run' (on time), 'catch-up' (late but allowed), or 'missed'.

    A slot is on time when it is at most two ticks old. After that it is only run when catch-up is on, the delay is
    within ``catch_up_hours`` and the stop-by window has not already closed."""
    delay = (now - slot).total_seconds()
    if delay <= 2 * tick_seconds:
        return "run"
    if catch_up and delay <= catch_up_hours * 3600 and (window is None or now < window):
        return "catch-up"
    return "missed"


def due_slots(schedule: dict[str, Any], last_checked: datetime, now: datetime) -> list[datetime]:
    return occurrences(schedule, last_checked, now)


def describe_schedule(schedule: dict[str, Any]) -> str:
    days = schedule.get("days") or ALL_DAYS
    if sorted(days) == ALL_DAYS:
        when = "Every day"
    elif sorted(days) == [0, 1, 2, 3, 4]:
        when = "Weekdays"
    elif sorted(days) == [5, 6]:
        when = "Weekends"
    else:
        when = ", ".join(DAY_NAMES[day] for day in sorted(days))
    text = f"{when} at {schedule.get('time', '02:00')}"
    if schedule.get("stop_by"):
        text += f", stop by {schedule['stop_by']}"
    return text
