"""Unit and missing-data checks; fixtures are synthetic."""

import unittest
from datetime import datetime, timedelta

from feasibility.reconcile import CFS_TO_CMS, archive_time, number, select, stats


class DataTests(unittest.TestCase):
    def test_blank_and_negative_sentinels_are_not_zero(self):
        for value in (None, "", "  ", "-99999", "-999999", "NaN", "Infinity"):
            self.assertIsNone(number(value))
        self.assertEqual(number("0"), 0.0)

    def test_cfs_is_not_m3s(self):
        self.assertAlmostEqual(1000 * CFS_TO_CMS, 28.316846592)

    def test_time_conversion_explicitly_uses_summer_offset(self):
        time = archive_time("7/9/2022 0:00")
        self.assertEqual(time.utcoffset(), timedelta(hours=-6))
        self.assertEqual(time.timestamp(), datetime.fromisoformat("2022-07-09T06:00:00+00:00").timestamp())

    def test_window_is_half_open_and_missing_peak_stays_null(self):
        start = archive_time("7/9/2022 0:00")
        end = start + timedelta(days=1)
        rows = [{"timestamp": start, "x": None}, {"timestamp": end, "x": 10}]
        selected = select(rows, start, end)
        self.assertEqual(len(selected), 1)
        self.assertIsNone(stats(selected, "x")["maximum"])


if __name__ == "__main__":
    unittest.main()
