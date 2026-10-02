from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import helpers  # noqa: F401  (import path)
from scheduler import clean_schedule, classify_slot, describe_schedule, due_slots, next_run, occurrences, parse_hhmm, window_end

DAILY = {"days": [0, 1, 2, 3, 4, 5, 6], "time": "02:00", "stop_by": ""}


def at(day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second)   # 2026-10-01 is a Thursday


class TimeParsing(unittest.TestCase):
    def test_valid_and_invalid(self) -> None:
        self.assertEqual(parse_hhmm("02:00"), (2, 0))
        self.assertEqual(parse_hhmm("7:5"), (7, 5))
        for bad in ("", "25:00", "12:60", "12", "ab:cd", "12:00:00", "-1:00"):
            with self.assertRaises(ValueError, msg=bad):
                parse_hhmm(bad)

    def test_clean_schedule(self) -> None:
        self.assertEqual(clean_schedule({"days": [3, 1, 1, "5"], "time": "2:05", "stop_by": "6:30"}), {"days": [1, 3, 5], "time": "02:05", "stop_by": "06:30"})
        self.assertEqual(clean_schedule({})["days"], list(range(7)))
        with self.assertRaises(ValueError):
            clean_schedule({"days": []})
        with self.assertRaises(ValueError):
            clean_schedule({"days": [9]})
        with self.assertRaises(ValueError):
            clean_schedule({"time": "99:00"})


class Occurrences(unittest.TestCase):
    def test_bounds_are_after_exclusive_until_inclusive(self) -> None:
        self.assertEqual(occurrences(DAILY, at(1, 1), at(1, 2)), [at(1, 2)])
        self.assertEqual(occurrences(DAILY, at(1, 2), at(1, 3)), [])
        self.assertEqual(occurrences(DAILY, at(1, 2, 0, 1), at(2, 2)), [at(2, 2)])
        self.assertEqual(occurrences(DAILY, at(2), at(1)), [])

    def test_several_days_in_order(self) -> None:
        self.assertEqual(occurrences(DAILY, at(1, 3), at(4, 3)), [at(2, 2), at(3, 2), at(4, 2)])

    def test_weekday_filter(self) -> None:
        weekend = {"days": [5, 6], "time": "09:30", "stop_by": ""}
        self.assertEqual(occurrences(weekend, at(1), at(8)), [at(3, 9, 30), at(4, 9, 30)])   # Sat 3rd, Sun 4th

    def test_next_run(self) -> None:
        self.assertEqual(next_run(DAILY, at(1, 1, 59)), at(1, 2))
        self.assertEqual(next_run(DAILY, at(1, 2)), at(2, 2))
        weekdays = {"days": [0, 1, 2, 3, 4], "time": "02:00", "stop_by": ""}
        self.assertEqual(next_run(weekdays, at(2, 3)), at(5, 2))   # Fri 2nd evening -> Monday 5th
        self.assertIsNone(next_run({"days": [], "time": "02:00", "stop_by": ""}, at(1)))

    def test_due_slots_equals_occurrences(self) -> None:
        self.assertEqual(due_slots(DAILY, at(1, 1), at(1, 3)), [at(1, 2)])


class Windows(unittest.TestCase):
    def test_stop_by_same_day_and_overnight(self) -> None:
        self.assertEqual(window_end({**DAILY, "stop_by": "06:00"}, at(1, 2)), at(1, 6))
        night = {**DAILY, "time": "22:00", "stop_by": "06:00"}
        self.assertEqual(window_end(night, at(1, 22)), at(2, 6))
        self.assertEqual(window_end({**DAILY, "stop_by": "02:00"}, at(1, 2)), at(2, 2))   # equal means a full day
        self.assertIsNone(window_end(DAILY, at(1, 2)))


class Classification(unittest.TestCase):
    def kw(self, **extra):
        return {"tick_seconds": 10, "catch_up": True, "catch_up_hours": 12, "window": None} | extra

    def test_on_time(self) -> None:
        self.assertEqual(classify_slot(at(1, 2), at(1, 2, 0, 5), **self.kw()), "run")
        self.assertEqual(classify_slot(at(1, 2), at(1, 2, 0, 20), **self.kw()), "run")   # two ticks late is still on time

    def test_catch_up_allowed_and_denied(self) -> None:
        self.assertEqual(classify_slot(at(1, 2), at(1, 5), **self.kw()), "catch-up")
        self.assertEqual(classify_slot(at(1, 2), at(1, 5), **self.kw(catch_up=False)), "missed")
        self.assertEqual(classify_slot(at(1, 2), at(1, 15), **self.kw()), "missed")   # 13 h > 12 h
        self.assertEqual(classify_slot(at(1, 2), at(1, 14), **self.kw(catch_up_hours=12)), "catch-up")

    def test_window_closed_means_missed(self) -> None:
        self.assertEqual(classify_slot(at(1, 2), at(1, 7), **self.kw(window=at(1, 6))), "missed")
        self.assertEqual(classify_slot(at(1, 2), at(1, 5), **self.kw(window=at(1, 6))), "catch-up")


class Describe(unittest.TestCase):
    def test_text(self) -> None:
        self.assertEqual(describe_schedule(DAILY), "Every day at 02:00")
        self.assertEqual(describe_schedule({"days": [0, 1, 2, 3, 4], "time": "08:00", "stop_by": ""}), "Weekdays at 08:00")
        self.assertEqual(describe_schedule({"days": [5, 6], "time": "08:00", "stop_by": ""}), "Weekends at 08:00")
        self.assertEqual(describe_schedule({"days": [0, 3], "time": "01:30", "stop_by": "05:00"}), "Mon, Thu at 01:30, stop by 05:00")


if __name__ == "__main__":
    unittest.main()
