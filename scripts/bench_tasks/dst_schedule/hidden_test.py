import unittest
from datetime import date, datetime, timezone

from schedule import daily_times


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def instants(results):
    return [r.astimezone(timezone.utc) for r in results]


class DailyTimesTest(unittest.TestCase):
    def test_returns_aware_datetimes_one_per_day(self):
        result = daily_times(date(2026, 6, 1), 3, 9, 0, "America/New_York")
        self.assertEqual(len(result), 3)
        for item in result:
            self.assertIsNotNone(item.utcoffset())
        self.assertEqual(instants(result), [utc(2026, 6, 1, 13), utc(2026, 6, 2, 13), utc(2026, 6, 3, 13)])

    def test_zero_days_is_empty(self):
        self.assertEqual(daily_times(date(2026, 6, 1), 0, 9, 0, "UTC"), [])

    def test_wall_clock_holds_across_spring_forward(self):
        result = daily_times(date(2026, 3, 7), 3, 9, 0, "America/New_York")
        self.assertEqual(instants(result), [utc(2026, 3, 7, 14), utc(2026, 3, 8, 13), utc(2026, 3, 9, 13)])

    def test_wall_clock_holds_across_fall_back(self):
        result = daily_times(date(2026, 10, 31), 3, 9, 0, "America/New_York")
        self.assertEqual(instants(result), [utc(2026, 10, 31, 13), utc(2026, 11, 1, 14), utc(2026, 11, 2, 14)])

    def test_nonexistent_local_time_keeps_the_offset_before_the_change(self):
        result = daily_times(date(2026, 3, 8), 1, 2, 30, "America/New_York")
        self.assertEqual(instants(result), [utc(2026, 3, 8, 7, 30)])

    def test_repeated_local_time_uses_the_first_one(self):
        result = daily_times(date(2026, 11, 1), 1, 1, 30, "America/New_York")
        self.assertEqual(instants(result), [utc(2026, 11, 1, 5, 30)])

    def test_europe_london_spring_forward(self):
        result = daily_times(date(2026, 3, 28), 3, 8, 0, "Europe/London")
        self.assertEqual(instants(result), [utc(2026, 3, 28, 8), utc(2026, 3, 29, 7), utc(2026, 3, 30, 7)])

    def test_half_hour_shift_lord_howe(self):
        result = daily_times(date(2026, 4, 4), 3, 12, 0, "Australia/Lord_Howe")
        self.assertEqual(instants(result), [utc(2026, 4, 4, 1, 0), utc(2026, 4, 5, 1, 30), utc(2026, 4, 6, 1, 30)])

    def test_zone_without_dst(self):
        result = daily_times(date(2026, 3, 7), 3, 9, 0, "Asia/Kolkata")
        self.assertEqual(instants(result), [utc(2026, 3, 7, 3, 30), utc(2026, 3, 8, 3, 30), utc(2026, 3, 9, 3, 30)])

    def test_month_and_year_rollover(self):
        result = daily_times(date(2026, 12, 30), 4, 23, 45, "UTC")
        self.assertEqual(
            instants(result),
            [utc(2026, 12, 30, 23, 45), utc(2026, 12, 31, 23, 45), utc(2027, 1, 1, 23, 45), utc(2027, 1, 2, 23, 45)],
        )

    def test_leap_day(self):
        result = daily_times(date(2028, 2, 28), 3, 6, 0, "UTC")
        self.assertEqual([r.date() for r in result], [date(2028, 2, 28), date(2028, 2, 29), date(2028, 3, 1)])

    def test_unknown_timezone_is_a_value_error(self):
        for bad in ("Mars/Olympus", "", "not a zone"):
            with self.subTest(tz=bad):
                with self.assertRaises(ValueError):
                    daily_times(date(2026, 6, 1), 1, 9, 0, bad)

    def test_out_of_range_inputs_are_value_errors(self):
        for hour, minute, days in ((24, 0, 1), (-1, 0, 1), (9, 60, 1), (9, -1, 1), (9, 0, -1)):
            with self.subTest(hour=hour, minute=minute, days=days):
                with self.assertRaises(ValueError):
                    daily_times(date(2026, 6, 1), days, hour, minute, "UTC")


if __name__ == "__main__":
    unittest.main()
