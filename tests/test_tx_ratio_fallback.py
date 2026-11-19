"""rulebook.tx_ratio_gate falls back to GeckoTerminal's 5m buy/sell counts when
DexScreener has none. No data anywhere is still a skip (tx_ratio_unknown)."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_rulebook as tr  # noqa: E402  (also loads isolate_env via conftest / env defaults)
from desk import rulebook, scanner  # noqa: E402
from legacy_book import legacy_book  # noqa: E402
from test_scanner_sources import FakeResp  # noqa: E402

POOL = "POOLADDR111"
DS_MISSING = {"available": False}
DS_NO_M5 = {**tr.GOOD_DS, "buys_m5": 0, "sells_m5": 0}


def gt_body(buys, sells):
    return {"data": {"attributes": {"address": POOL,
                                    "transactions": {"m5": {"buys": buys, "sells": sells}}}}}


def fb(buys, sells):
    return {"available": True, "source": "geckoterminal", "buys_m5": buys, "sells_m5": sells}


@legacy_book
class AgentFallback(unittest.TestCase):
    run_agent = tr.AgentWiring.run_agent
    skips = tr.AgentWiring.skips

    def setUp(self):
        tr.on()

    def _run(self, ds, fallback):
        with mock.patch.object(scanner, "gt_tx_m5", return_value=fallback) as m:
            a = self.run_agent(ds=ds)
        return a, m

    def test_fallback_used_when_dexscreener_missing_and_gate_passes(self):
        a, m = self._run(DS_MISSING, fb(40, 20))
        m.assert_called_once()
        self.assertEqual(self.skips(a), [])
        self.assertEqual(a.stats["entries"], 1)

    def test_fallback_used_when_dexscreener_has_zero_counts(self):
        a, m = self._run(DS_NO_M5, fb(40, 20))
        m.assert_called_once()
        self.assertEqual(self.skips(a), [])

    def test_gate_fails_on_fallback_numbers(self):
        a, _ = self._run(DS_MISSING, fb(24, 20))        # 1.2 is not > 1.2
        self.assertEqual(self.skips(a), ["tx_ratio_low"])
        self.assertEqual(a.stats["entries"], 0)

    def test_still_skips_when_every_source_is_missing(self):
        a, m = self._run(DS_MISSING, None)
        m.assert_called_once()
        self.assertEqual([r.split(":")[0] for r in self.skips(a)], ["tx_ratio_unknown"])
        self.assertTrue(self.skips(a)[0].startswith("tx_ratio_unknown:"))      # with a named cause
        self.assertEqual(a.stats["entries"], 0)
        a2, _ = self._run(DS_MISSING, fb(0, 0))
        self.assertEqual([r.split(":")[0] for r in self.skips(a2)], ["tx_ratio_unknown"])
        self.assertIn("gt_zero_txns", self.skips(a2)[0])

    def test_fallback_not_asked_when_dexscreener_has_data(self):
        a, m = self._run(tr.GOOD_DS, fb(1, 100))
        m.assert_not_called()
        self.assertEqual(self.skips(a), [])
        low = {**tr.GOOD_DS, "buys_m5": 10, "sells_m5": 20}
        a2, m2 = self._run(low, fb(100, 1))
        m2.assert_not_called()                           # DexScreener's own numbers decide
        self.assertEqual(self.skips(a2), ["tx_ratio_low"])

    def test_gate_off_never_asks(self):
        tr.RULEBOOK["tx_ratio_gate"] = False
        a, m = self._run(DS_MISSING, fb(1, 100))
        m.assert_not_called()
        self.assertEqual(self.skips(a), [])

    def test_gate_fed_by_fallback_dict_directly(self):
        self.assertIsNone(rulebook.tx_ratio_gate(fb(40, 20)))
        self.assertEqual(rulebook.tx_ratio_gate(fb(10, 20)), "tx_ratio_low")


class GtTxM5(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()
        p = mock.patch("desk.netutil.offline", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.urls = []

    def session(self, resp=None):
        s = mock.Mock()

        def get(url, params=None, headers=None, timeout=None):
            self.urls.append(url)
            return resp or FakeResp(200, gt_body(30, 10))
        s.get.side_effect = get
        return s

    def test_reads_m5_counts_from_the_pool_endpoint(self):
        r = scanner.gt_tx_m5({"pool": POOL}, self.session())
        self.assertEqual(r, fb(30, 10))
        self.assertEqual(self.urls, [f"{scanner.GT}/networks/solana/pools/{POOL}"])

    def test_cached_per_pool_so_no_request_flood(self):
        s, now = self.session(), time.time()
        for i in range(5):
            self.assertEqual(scanner.gt_tx_m5({"pool": POOL}, s, now + i), fb(30, 10))
        self.assertEqual(len(self.urls), 1)
        scanner.gt_tx_m5({"pool": POOL}, s, now + scanner.GT_TX_TTL_S + 1)
        self.assertEqual(len(self.urls), 2)

    def test_own_cap_limits_distinct_pools(self):
        s, now = self.session(), time.time()
        got = [scanner.gt_tx_m5({"pool": f"P{i}"}, s, now) for i in range(6)]
        self.assertLess(len(self.urls), 3)
        self.assertEqual(sum(1 for g in got if g), len(self.urls))

    def test_429_cools_and_returns_none_without_retry(self):
        s = self.session(FakeResp(429, reason="Too Many Requests", headers={"Retry-After": "60"}))
        now = time.time()
        self.assertIsNone(scanner.gt_tx_m5({"pool": POOL}, s, now))
        self.assertIsNone(scanner.gt_tx_m5({"pool": "OTHER"}, s, now + 1))
        self.assertEqual(len(self.urls), 1)
        self.assertTrue(scanner.host_cooling("geckoterminal", now + 1))

    def test_no_data_cases_return_none(self):
        self.assertIsNone(scanner.gt_tx_m5({}, self.session()))                          # no pool
        self.assertIsNone(scanner.gt_tx_m5({"pool": POOL}, self.session(
            FakeResp(200, {"data": {"attributes": {"transactions": {}}}}))))            # no m5
        scanner.reset_runtime()
        self.assertIsNone(scanner.gt_tx_m5({"pool": POOL}, self.session(FakeResp(500))))  # error
        with mock.patch("desk.netutil.offline", return_value=True):
            scanner.reset_runtime()
            s = self.session()
            self.assertIsNone(scanner.gt_tx_m5({"pool": POOL}, s))
            s.get.assert_not_called()

    def test_cache_is_bounded(self):
        s = self.session()
        with mock.patch.object(scanner, "GT_TX_MAX", 3), \
                mock.patch.object(scanner, "_TX_BUCKET", scanner.TokenBucket(10_000, 10_000)), \
                mock.patch.dict(scanner._BUCKETS, {"geckoterminal": scanner.TokenBucket(10_000, 10_000)}):
            for i in range(10):
                scanner.gt_tx_m5({"pool": f"P{i}"}, s)
        self.assertLessEqual(len(scanner._TX_CACHE), 3)


if __name__ == "__main__":
    unittest.main()
