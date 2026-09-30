"""Scheduling helpers for the reminders service."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def add_days(dt, days):
    """Move an aware datetime forward by `days` whole days."""
    return (dt.astimezone(timezone.utc) + timedelta(days=days)).astimezone(dt.tzinfo)


def is_weekend(dt):
    """True on Saturday and Sunday."""
    return dt.weekday() >= 5


def next_weekday(dt):
    """The next Monday-to-Friday day after `dt`, at the same time of day."""
    step = add_days(dt, 1)
    while is_weekend(step):
        step = add_days(step, 1)
    return step


def daily_times(start, days, hour, minute, tz_name):
    if days < 0:
        raise ValueError("days must not be negative")
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"unknown timezone {tz_name!r}") from exc
    at = time(hour, minute)
    return [datetime.combine(start + timedelta(days=n), at, tzinfo=zone) for n in range(days)]
