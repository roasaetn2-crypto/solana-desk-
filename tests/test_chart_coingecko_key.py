"""Keyed CoinGecko Demo OHLCV: header, fallback, daily budget, pacing, redaction."""
import logging
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from desk import chart, scanner
from test_scanner_sources import FakeResp

SECRET = "CG-test-secret-key-123456"


def _body(n=12):
    t0 = int(time.time()) // 900 * 900
    rows = [[t0 - 900 * (n - i), 1.0 + i, 1.5 + i, 0.5 + i, 1.2 + i, 10] for i in range(n)]
    return {"data": {"attributes": {"ohlcv_list": rows}}}


class Keyed(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()
        chart.reset()
        chart._fetch_ohlcv = None
        self.env = mock.patch.dict(os.environ, {"COINGECKO_DEMO_API_KEY": SECRET})
        self.env.start()
        self.now = time.time()
        for mint, pool in (("MINTA", "POOLA"), ("MINTB", "POOLB")):
            scanner._POOL[mint] = {"token": {"pool": pool}, "at": self.now}
        self.calls = []            # (url, params, headers)
        self.cg_status = 200
        self.cg_raises = None
        s = mock.Mock()
        s.get.side_effect = self._get
        self.s = s

    def tearDown(self):
        self.env.stop()
        chart.reset()
        scanner.reset_runtime()

    def _get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, dict(headers or {})))
        if "coingecko.com" in url:
            if self.cg_raises:
                raise self.cg_raises
            if self.cg_status == 429:
                return FakeResp(429, headers={"Retry-After": "30"})
            if self.cg_status != 200:
                return FakeResp(self.cg_status)
            return FakeResp(200, _body())
        return FakeResp(200, _body())

    def cg_calls(self):
        return [c for c in self.calls if "coingecko.com" in c[0]]

    def gt_calls(self):
        return [c for c in self.calls if "geckoterminal.com" in c[0]]

    def test_keyed_call_uses_onchain_endpoint_and_header_only(self):
        out = chart.prefetch(self.s, [("MINTA", "scalp")], now=self.now)
        self.assertEqual(out, ("MINTA", "1m"))
        self.assertEqual(len(self.cg_calls()), 1)
        self.assertEqual(len(self.gt_calls()), 0)
        url, params, headers = self.cg_calls()[0]
        self.assertEqual(url, "https://api.coingecko.com/api/v3/onchain/networks/solana/pools/POOLA/ohlcv/minute")
        self.assertEqual(headers["x-cg-demo-api-key"], SECRET)
        self.assertNotIn(SECRET, url)
        self.assertNotIn(SECRET, repr(params))
        self.assertEqual(params["aggregate"], 1)

    def test_candles_are_cached_and_charts_cost_nothing(self):
        chart.prefetch(self.s, [("MINTA", "swing")], now=self.now)
        n = len(self.calls)
        for _ in range(5):
            r = chart.series("MINTA", "15m", horizon="swing", session=self.s)
        self.assertEqual(len(self.calls), n)
        self.assertGreater(len(r["candles"]), 8)

    def test_no_key_uses_public_path_only(self):
        with mock.patch.dict(os.environ, {"COINGECKO_DEMO_API_KEY": ""}):
            chart.prefetch(self.s, [("MINTA", "scalp")], now=self.now)
        self.assertEqual(len(self.cg_calls()), 0)
        self.assertEqual(len(self.gt_calls()), 1)
        self.assertNotIn("x-cg-demo-api-key", self.gt_calls()[0][2])

    def test_public_request_never_carries_the_key(self):
        self.cg_status = 500
        chart.prefetch(self.s, [("MINTA", "scalp")], now=self.now)
        self.assertEqual(len(self.gt_calls()), 1)
        url, params, headers = self.gt_calls()[0]
        self.assertNotIn("x-cg-demo-api-key", headers)
        self.assertNotIn(SECRET, url + repr(params) + repr(headers))

    def test_keyed_429_falls_back_to_public_and_cools_keyed_host_separately(self):
        self.cg_status = 429
        chart.prefetch(self.s, [("MINTA", "scalp")], now=self.now)
        self.assertEqual(len(self.cg_calls()), 1)
        self.assertEqual(len(self.gt_calls()), 1)          # same call fell back
        self.assertFalse(chart.cg_available(self.now + 1))  # keyed host cooling
        self.assertFalse(scanner.host_cooling("geckoterminal", self.now + 1))  # public host unaffected
        chart.prefetch(self.s, [("MINTB", "scalp")], now=self.now + 30)
        self.assertEqual(len(self.cg_calls()), 1)          # still cooling, no more keyed calls
        self.assertGreaterEqual(len(self.gt_calls()), 2)
        self.assertEqual(chart.cg_stats(self.now + 1)["r429"], 1)

    def test_keyed_error_and_exception_fall_back(self):
        self.cg_raises = requests.ConnectionError(f"boom {SECRET}")
        out = chart.prefetch(self.s, [("MINTA", "scalp")], now=self.now)
        self.assertEqual(out, ("MINTA", "1m"))
        self.assertEqual(len(self.gt_calls()), 1)
        self.assertGreater(len(chart._OHLCV["POOLA:1m"]["candles"]), 8)

    def test_daily_budget_is_enforced_then_public_path(self):
        t = time.mktime(time.strptime("2026-11-12 01:00:00", "%Y-%m-%d %H:%M:%S")) - time.timezone
        with mock.patch.object(chart, "CG_MIN_GAP_S", 0.0):      # isolate the cap from pacing
            for _ in range(chart.CG_DAILY_BUDGET + 25):
                chart._fetch_and_store("POOLA", "1m", self.s, t)
                t += 1
        self.assertEqual(len(self.cg_calls()), chart.CG_DAILY_BUDGET)   # exactly the cap, no more
        self.assertEqual(chart.cg_stats(t)["used_today"], chart.CG_DAILY_BUDGET)
        self.assertFalse(chart.cg_available(t))
        self.assertGreaterEqual(len(self.gt_calls()), 1)               # the rest used the public path

    def test_budget_resets_on_new_utc_day(self):
        chart._CG["day"] = "1999-01-01"
        chart._CG["used"] = chart.CG_DAILY_BUDGET
        self.assertTrue(chart.cg_available(self.now))
        self.assertEqual(chart.cg_stats(self.now)["used_today"], 0)

    def test_budget_math_30s_cycle_stays_under_cap(self):
        # one position, a 30s cycle for a full UTC day: calls must stay <= daily budget
        day0 = time.mktime(time.strptime("2026-11-12 00:00:00", "%Y-%m-%d %H:%M:%S")) - time.timezone
        pos = [("MINTA", "scalp")]
        t = day0
        with mock.patch("time.time", lambda: t):
            while t < day0 + 86400:
                chart.prefetch(self.s, pos, now=t)
                t += 30
        n = len(self.cg_calls())
        self.assertLessEqual(n, chart.CG_DAILY_BUDGET)
        self.assertGreater(n, 200)
        self.assertLess(n * 31, 10_000)                     # a 31 day month stays under 10k

    def test_pacing_between_keyed_calls(self):
        pos = [("MINTA", "scalp"), ("MINTB", "scalp")]
        self.assertIsNotNone(chart.prefetch(self.s, pos, now=self.now))
        self.assertIsNone(chart.prefetch(self.s, pos, now=self.now + 30))
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(chart.prefetch(self.s, pos, now=self.now + chart.CG_MIN_GAP_S + 1))
        self.assertEqual(len(self.cg_calls()), 2)

    def test_dashboard_miss_does_not_bypass_pacing(self):
        chart.prefetch(self.s, [("MINTA", "scalp")], now=self.now)
        self.assertEqual(len(self.cg_calls()), 1)
        chart.series("MINTB", "1m", horizon="scalp", session=self.s)   # cold miss right after
        self.assertEqual(len(self.cg_calls()), 1)

    def test_key_never_in_logs(self):
        h = logging.Handler()
        seen = []
        h.emit = lambda rec: seen.append(rec.getMessage())
        root = logging.getLogger()
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        try:
            chart.prefetch(self.s, [("MINTA", "scalp")], now=self.now)              # ok
            self.cg_raises = requests.ConnectionError(f"boom {SECRET}")
            chart.prefetch(self.s, [("MINTB", "scalp")], now=self.now + 400)         # exception
            self.cg_raises = None
            self.cg_status = 429
            chart.prefetch(self.s, [("MINTA", "swing")], now=self.now + 800)         # 429
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        self.assertTrue(seen)
        self.assertTrue(any("coingecko" in m for m in seen))
        for m in seen:
            self.assertNotIn(SECRET, m)
        self.assertNotIn(SECRET, repr(scanner.LAST))
        self.assertNotIn(SECRET, repr(chart.cg_stats()))

    def test_redact_helper(self):
        self.assertNotIn(SECRET, chart._redact(f"url?k={SECRET}"))


if __name__ == "__main__":
    unittest.main()
