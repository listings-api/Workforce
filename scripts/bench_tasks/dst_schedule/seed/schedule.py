"""Scheduling helpers for the reminders service."""

from datetime import datetime, timedelta, timezone


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
