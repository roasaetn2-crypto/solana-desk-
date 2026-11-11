"""Age labels for ticket cards: 3m, 2h 14m, 5d."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk.age import created_at_of, format_age, short_ca  # noqa: E402


class FormatAge(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(format_age(0), "0s")
        self.assertEqual(format_age(3), "3s")
        self.assertEqual(format_age(59), "59s")

    def test_minutes(self):
        self.assertEqual(format_age(60), "1m")
        self.assertEqual(format_age(3 * 60), "3m")
        self.assertEqual(format_age(59 * 60 + 59), "59m")

    def test_hours(self):
        self.assertEqual(format_age(2 * 3600 + 14 * 60), "2h 14m")
        self.assertEqual(format_age(2 * 3600), "2h")
        self.assertEqual(format_age(23 * 3600 + 5 * 60), "23h 5m")

    def test_days(self):
        self.assertEqual(format_age(5 * 86400), "5d")
        self.assertEqual(format_age(5 * 86400 + 3 * 3600), "5d")

    def test_bad_values(self):
        self.assertEqual(format_age(None), "—")
        self.assertEqual(format_age(-12), "0s")


class ShortCa(unittest.TestCase):
    def test_shortens_to_first_and_last_four(self):
        mint = "EYX9NaK8NnFc55dvRE8Hv6pu5HgCCLsunvsAsqa4pump"
        self.assertEqual(short_ca(mint), "EYX9…pump")
        self.assertEqual(short_ca("AbCdXXXXWxYz"), "AbCd…WxYz")

    def test_short_mints_pass_through(self):
        self.assertEqual(short_ca("ABC"), "ABC")
        self.assertEqual(short_ca(""), "")


class CreatedAt(unittest.TestCase):
    def test_prefers_stored_timestamp(self):
        self.assertEqual(created_at_of({"created_at": 1700000000, "age_minutes": 3}), 1700000000)

    def test_falls_back_to_age_minutes(self):
        got = created_at_of({"age_minutes": 10}, now=1_000_000)
        self.assertEqual(got, 1_000_000 - 600)


if __name__ == "__main__":
    unittest.main()
