import unittest
from datetime import datetime, timezone

import schedule


class ScheduleTest(unittest.TestCase):
    def test_is_weekend(self):
        self.assertTrue(schedule.is_weekend(datetime(2026, 1, 3, tzinfo=timezone.utc)))
        self.assertFalse(schedule.is_weekend(datetime(2026, 1, 5, tzinfo=timezone.utc)))

    def test_next_weekday_skips_the_weekend(self):
        friday = datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(schedule.next_weekday(friday), datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
