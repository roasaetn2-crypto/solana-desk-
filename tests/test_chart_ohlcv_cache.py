"""Dashboard charts read a last-good OHLCV cache filled by one prefetch per cycle."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolate_env  # noqa: E402,F401  # drop live CoinGecko key (unittest does not load conftest)
from desk import chart, scanner
from test_scanner_sources import FakeResp


def _body(n=12, base=1000):
    t0 = int(time.time()) // 900 * 900
    rows = [[t0 - 900 * (n - i), 1.0 + i, 1.5 + i, 0.5 + i, 1.2 + i, 10] for i in range(n)]
    return {"data": {"attributes": {"ohlcv_list": rows}}}


class OhlcvCache(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()
        chart.reset()
        chart._fetch_ohlcv = None
        self.now = time.time()
        for mint, pool in (("MINTA", "POOLA"), ("MINTB", "POOLB")):
            scanner._POOL[mint] = {"token": {"pool": pool}, "at": self.now}
        self.urls = []
        self.mode = "ok"
        s = mock.Mock()
        s.get.side_effect = self._get
        self.s = s

    def tearDown(self):
        chart.reset()
        scanner.reset_runtime()

    def _get(self, url, params=None, headers=None, timeout=None):
        self.urls.append(url)
        if self.mode == "429":
            return FakeResp(429, reason="Too Many Requests")
        return FakeResp(200, _body())

    def _at(self, dt):
        return mock.patch("time.time", lambda: self.now + dt)

    def test_prefetch_spends_one_call_per_cycle_and_charts_cost_nothing(self):
        pos = [("MINTA", "swing"), ("MINTB", "scalp")]
        with self._at(0):
            self.assertIsNotNone(chart.prefetch(self.s, pos, now=self.now))
        self.assertEqual(len(self.urls), 1)
        with self._at(1):
            self.assertIsNotNone(chart.prefetch(self.s, pos, now=self.now + 1))
        self.assertEqual(len(self.urls), 2)
        # 15m for A and 1m for B are warm; dashboard polls add no requests.
        with self._at(2):
            for _ in range(10):
                a = chart.series("MINTA", "15m", horizon="swing", session=self.s)
                b = chart.series("MINTB", "1m", horizon="scalp", session=self.s)
        self.assertEqual(len(self.urls), 2)
        self.assertGreater(len(a["candles"]), 8)
        self.assertEqual(a["source"], "geckoterminal")
        self.assertGreater(len(b["candles"]), 8)

    def test_429_still_returns_last_good_candles(self):
        with self._at(0):
            chart.prefetch(self.s, [("MINTA", "swing")], now=self.now)
        self.mode = "429"
        with self._at(400):  # past the 5 min refresh age, inside the 15 min last-good age
            chart.prefetch(self.s, [("MINTA", "swing")], now=self.now + 400)
            out = chart.series("MINTA", "15m", horizon="swing", session=self.s)
        self.assertEqual(out["source"], "geckoterminal")
        self.assertGreater(len(out["candles"]), 8)

    def test_last_good_ages_out_at_15m_but_1h_and_4h_are_still_served_without_http(self):
        for i in range(3):   # one per cycle, 30s apart, as the agent does
            with self._at(30 * i):
                chart.prefetch(self.s, [("MINTA", "swing")], now=self.now + 30 * i)
        self.assertEqual(len(self.urls), 3)
        self.mode = "429"
        with self._at(3600):
            self.assertGreater(len(chart._network_ohlcv("MINTA", "1H", self.s)), 8)
            self.assertGreater(len(chart._network_ohlcv("MINTA", "4H", self.s)), 8)
            self.assertEqual(len(self.urls), 3)          # served from last-good, no request
            chart._network_ohlcv("MINTA", "15m", self.s)  # 1h old: outside the 15 min window
            self.assertEqual(len(self.urls), 4)

    def test_cold_polls_during_429_are_one_request_not_one_per_poll(self):
        self.mode = "429"
        with self._at(0):
            for _ in range(10):
                chart.series("MINTA", "15m", horizon="swing", session=self.s)
        self.assertEqual(len(self.urls), 1)

    def test_prefetch_skips_when_host_is_cooling(self):
        self.mode = "429"
        with self._at(0):
            chart.prefetch(self.s, [("MINTA", "swing")], now=self.now)
            self.assertIsNone(chart.prefetch(self.s, [("MINTA", "swing")], now=self.now + 1))
        self.assertEqual(len(self.urls), 1)


if __name__ == "__main__":
    unittest.main()
