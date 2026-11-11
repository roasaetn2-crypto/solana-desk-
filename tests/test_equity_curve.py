"""Equity curve records only real points and does not invent history."""
from __future__ import annotations

import sys
import unittest
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk.equity_curve import record_mark, record_point, window  # noqa: E402


class RecordAndWindow(unittest.TestCase):
    def test_records_exact_equity(self):
        series = deque()
        record_point(series, 1000, 1000.00, 0)
        record_point(series, 1060, 995.59, -4.41)
        self.assertEqual(list(series), [
            {"t": 1000.0, "equity": 1000.0, "day_pnl": 0.0},
            {"t": 1060.0, "equity": 995.59, "day_pnl": -4.41},
        ])

    def test_short_history_is_shown_for_any_tab(self):
        series = deque([
            {"t": 1000.0, "equity": 1000.0},
            {"t": 1060.0, "equity": 995.59},
        ])
        pts, _ = window(series, [], "1W", now=1100.0)
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[-1]["equity"], 995.59)

    def test_window_filters_when_history_is_long_enough(self):
        series = deque([
            {"t": 0.0, "equity": 1000.0},
            {"t": 10_000.0, "equity": 990.0},
            {"t": 10_500.0, "equity": 995.59},
        ])
        pts, _ = window(series, [], "1H", now=10_800.0)
        self.assertEqual([p["t"] for p in pts], [10_000.0, 10_500.0])

    def test_empty_window_falls_back_to_existing_points(self):
        series = deque([{"t": 100.0, "equity": 1000.0}])
        pts, _ = window(series, [], "LIVE", now=100_000.0)
        self.assertEqual(pts, [{"t": 100.0, "equity": 1000.0}])

    def test_marks_carry_real_trade_fields(self):
        marks = deque()
        record_mark(marks, 50, "buy", "NIHAO", usd=33.33, mint="Abc")
        record_mark(marks, 80, "sell", "NIHAO", usd=33.33, pnl_usd=1.2, fraction=1.0, mult=1.04)
        self.assertEqual(marks[0]["kind"], "buy")
        self.assertEqual(marks[1]["pnl_usd"], 1.2)
        self.assertNotIn("mcap_usd", marks[0])  # omitted when unknown, never invented


if __name__ == "__main__":
    unittest.main()
