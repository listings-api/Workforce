Customers want reminders at the same local time every day, wherever they live. In `schedule.py`, add `daily_times(start, days, hour, minute, tz_name)`.

`start` is a `datetime.date`. It returns a list of `days` timezone-aware `datetime` objects, one per consecutive calendar day beginning at `start`, each at `hour:minute` local wall-clock time in the IANA timezone `tz_name` (for example `"America/New_York"`). The local time must stay the same every day, including across daylight-saving changes.

Two edge cases. If a local time does not exist on some day (the clocks jump forward), keep the offset that applied just before the change, so 02:30 becomes 03:30. If a local time happens twice (the clocks go back), use the first one.

Raise `ValueError` for an unknown timezone name, for an `hour` or `minute` out of range, and for a negative `days`. `days=0` returns an empty list.

Add tests for it to the repo's existing test file.
