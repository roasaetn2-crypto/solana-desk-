"""Ticket chart: timeframe tabs, tick resampling, cached GT fallback. No live network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import chart  # noqa: E402


class ChartTabs(unittest.TestCase):
    def test_fresh_coins_get_scalp_tabs(self):
        self.assertEqual(chart.tabs_for(horizon="scalp"), ["15s", "30s"])
        self.assertEqual(chart.tabs_for(age_hours=1.0), ["15s", "30s"])

    def test_older_coins_get_swing_tabs(self):
        self.assertEqual(chart.tabs_for(horizon="swing"), ["15m", "1H", "4H"])
        self.assertEqual(chart.tabs_for(age_hours=8.0), ["15m", "1H", "4H"])


class ChartTicks(unittest.TestCase):
    def setUp(self):
        chart.reset()
        chart._fetch_ohlcv = None

    def tearDown(self):
        chart.reset()
        chart._fetch_ohlcv = None

    def test_bucket_ticks_builds_ohlc(self):
        rows = [
            (990, 1.0, 1),
            (995, 1.2, 1),
            (998, 0.9, 1),
            (1004, 1.1, 1),
        ]
        bars = chart.bucket_ticks(rows, 15)
        self.assertEqual(len(bars), 1)
        b = bars[0]
        self.assertEqual(b["t"], 990)
        self.assertEqual(b["o"], 1.0)
        self.assertEqual(b["h"], 1.2)
        self.assertEqual(b["l"], 0.9)
        self.assertEqual(b["c"], 1.1)
        self.assertEqual(b["v"], 4)

    def test_series_uses_local_ticks_for_15s(self):
        t0 = 1_700_000_000
        for i in range(20):
            chart.record_tick("M", 1 + i * 0.01, 1, ts=t0 + i * 15)
        out = chart.series("M", "15s", horizon="scalp", entry=1.05)
        self.assertGreaterEqual(len(out["candles"]), 8)
        self.assertEqual(out["source"], "ticks")
        self.assertFalse(out["fallback"])
        self.assertEqual(out["entry"], 1.05)
        self.assertEqual(out["tabs"], ["15s", "30s"])

    def test_thin_ticks_fall_back_to_1m_network(self):
        chart.record_tick("M", 1.0, 0, ts=10)
        chart._fetch_ohlcv = lambda mint, tf: [
            {"t": 60, "o": 1, "h": 1.1, "l": 0.9, "c": 1.05, "v": 3}
        ]
        out = chart.series("M", "15s", horizon="scalp")
        self.assertTrue(out["fallback"])
        self.assertEqual(out["tf"], "1m")
        self.assertEqual(out["source"], "geckoterminal")
        self.assertIn("1m", out["tabs"])
        self.assertEqual(len(out["candles"]), 1)

    def test_swing_uses_cached_network_once(self):
        calls = {"n": 0}

        def fake(mint, tf):
            calls["n"] += 1
            return {"data": {"attributes": {"ohlcv_list": [
                [100, 1, 1.2, 0.8, 1.1, 5],
                [700, 1.1, 1.3, 1.0, 1.2, 7],
            ]}}}

        # First call hits the hook; we also verify parse.
        chart._fetch_ohlcv = fake
        a = chart.series("M", "15m", horizon="swing")
        self.assertEqual(a["source"], "geckoterminal")
        self.assertEqual(len(a["candles"]), 2)
        self.assertEqual(a["tabs"], ["15m", "1H", "4H"])
        # hook is invoked once per series() (no double-fetch)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
