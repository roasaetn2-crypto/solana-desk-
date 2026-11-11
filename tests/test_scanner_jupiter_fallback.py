"""GT and DS 429 from Render's shared IP: Jupiter + PumpPortal keep the pool filled. No live network."""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SCAN_FETCH
from desk import pumpportal, scanner
from test_scanner_sources import FakeResp

ACTIVE_MINT = "Act1veMint1111111111111111111111111111pump"
QUIET_MINT = "Qu1etMint11111111111111111111111111111pump"


def _iso(ago_s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago_s))


def jup_row(mint=ACTIVE_MINT, symbol="CAT", buys=300, sells=200, vol=40_000, age_s=1800, liq=30_000):
    return {
        "id": mint, "symbol": symbol, "name": symbol, "launchpad": "pump.fun",
        "usdPrice": 0.0002, "mcap": 200_000, "fdv": 200_000, "liquidity": liq,
        "firstPool": {"id": mint, "createdAt": _iso(age_s)},
        "stats5m": {"buyVolume": vol / 20, "sellVolume": vol / 25, "priceChange": 3.0},
        "stats1h": {"buyVolume": vol * 0.6, "sellVolume": vol * 0.4, "numBuys": buys,
                    "numSells": sells, "numTraders": 150, "priceChange": 40.0},
        "stats6h": {"buyVolume": vol, "sellVolume": vol, "priceChange": 60.0},
        "stats24h": {"buyVolume": vol, "sellVolume": vol},
        "twitter": "https://x.com/catcoin",
    }


def launch(mint, at):
    pumpportal.ingest(json.dumps({"txType": "create", "mint": mint,
                                  "traderPublicKey": "Creator1111111111111111111111111111111", "initialBuy": 1}),
                      now=at)


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class JupiterFallback(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()
        pumpportal.reset()
        self.clock = Clock()
        self.urls = []
        self.p = mock.patch.object(scanner, "_jup_enabled", lambda: True)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        pumpportal.reset()

    def _session(self, jup_status=200, search_rows=None, list_rows=None):
        def router(url, params=None, headers=None, timeout=None):
            self.urls.append((url, dict(params or {})))
            if "jup.ag" in url:
                if jup_status != 200:
                    return FakeResp(jup_status)
                if url.endswith("/search"):
                    wanted = set((params or {}).get("query", "").split(","))
                    rows = search_rows if search_rows is not None else [jup_row()]
                    return FakeResp(200, [r for r in rows if r["id"] in wanted])
                return FakeResp(200, list_rows if list_rows is not None else [])
            return FakeResp(429, reason="Too Many Requests")   # GT and DS: Render IP throttled
        s = mock.Mock()
        s.get.side_effect = router
        return s

    def _cycles(self, n, s, step=30):
        out = []
        with mock.patch("time.time", self.clock):
            for _ in range(n):
                out = scanner.new_tokens(s)
                self.clock.t += step
        return out

    def test_parse_jup_token_maps_fields_and_passes_free_kill(self):
        t = scanner.parse_jup_token(jup_row(), "pumpportal")
        self.assertEqual(t["mint"], ACTIVE_MINT)
        self.assertEqual(t["source"], "pumpportal")
        self.assertEqual(t["dex"], "pump-fun")
        self.assertEqual(t["buys_h1"], 300)
        self.assertEqual(t["sells_h1"], 200)
        self.assertAlmostEqual(t["volume_h1"], 40_000)
        self.assertAlmostEqual(t["age_minutes"], 30, delta=1)
        self.assertIsNone(scanner.free_kill(t))
        grad = dict(jup_row(), graduatedPool="PoolX")
        self.assertEqual(scanner.parse_jup_token(grad)["dex"], "pumpswap")
        self.assertEqual(scanner.parse_jup_token(grad)["pool"], "PoolX")
        self.assertIsNone(scanner.parse_jup_token({"id": "So11111111111111111111111111111111111111112"}))
        self.assertIsNone(scanner.parse_jup_token("junk"))

    def test_gt_and_ds_429_still_yield_pumpportal_candidates(self):
        launch(ACTIVE_MINT, self.clock.t - 1800)
        tokens = self._cycles(1, self._session())
        self.assertEqual([t["mint"] for t in tokens], [ACTIVE_MINT])
        self.assertEqual(scanner.source_label(tokens), "pumpportal")
        rep = scanner.fetch_report()
        self.assertEqual(rep["pumpportal"]["pools"], 1)
        self.assertIn("geckoterminal", rep["cooling"])
        self.assertIn("dexscreener", rep["cooling"])
        self.assertNotIn("jupiter", rep["cooling"])

    def test_jupiter_discovery_list_is_a_source_too(self):
        tokens = self._cycles(1, self._session(list_rows=[jup_row("L1stMint111111111111111111111111111pump")]))
        self.assertEqual(scanner.source_label(tokens), "jupiter")

    def test_dead_launches_and_old_list_rows_stay_out_of_the_pool(self):
        launch(QUIET_MINT, self.clock.t - 900)
        old = jup_row("0ldMint11111111111111111111111111111111", age_s=90 * 86400)
        tokens = self._cycles(1, self._session(
            search_rows=[jup_row(QUIET_MINT, buys=1, sells=0, vol=50)], list_rows=[old]))
        self.assertEqual(tokens, [])
        self.assertEqual(scanner.source_label(tokens), "all-sources-empty")

    def test_too_young_launches_are_not_hydrated(self):
        launch(ACTIVE_MINT, self.clock.t - 30)             # 30s old, under min_age
        self._cycles(1, self._session())
        self.assertFalse([u for u, _ in self.urls if "jup.ag" in u and u.endswith("/search")])

    def test_cooldowns_are_independent_per_host(self):
        launch(ACTIVE_MINT, self.clock.t - 1800)
        self._cycles(1, self._session(jup_status=429))
        now = self.clock.t
        for host in ("geckoterminal", "dexscreener", "jupiter"):
            self.assertTrue(scanner.host_cooling(host, now), host)
        scanner._HOST_COOL["geckoterminal"]["until"] = 0
        self.assertFalse(scanner.host_cooling("geckoterminal", now))
        self.assertTrue(scanner.host_cooling("jupiter", now))
        self.assertTrue(scanner.is_cooling("jup:pumpportal", now))
        self.assertFalse(scanner.is_cooling("gt:new_pools:1", now))

    def test_jupiter_call_budget_over_ten_minutes(self):
        for i in range(400):
            launch(f"M{i:04d}" + "x" * 38, self.clock.t - 600 - i)
        self._cycles(20, self._session(search_rows=[]))  # 20 x 30s
        jup = [u for u, _ in self.urls if "jup.ag" in u]
        self.assertLessEqual(len(jup), SCAN_FETCH["jup_per_min"] * 10 + SCAN_FETCH["jup_burst"])
        for u, params in self.urls:
            if "jup.ag" in u and u.endswith("/search"):
                self.assertLessEqual(len(params["query"].split(",")), 100)

    def test_hot_launches_are_repriced_first_and_cold_ones_rarely(self):
        launch(QUIET_MINT, self.clock.t - 900)
        launch(ACTIVE_MINT, self.clock.t - 1800)
        rows = [jup_row(), jup_row(QUIET_MINT, buys=1, sells=0, vol=50)]
        s = self._session(search_rows=rows)
        self._cycles(1, s, step=61)
        self._cycles(1, s, step=61)
        searches = [p["query"].split(",") for u, p in self.urls if "jup.ag" in u and u.endswith("/search")]
        self.assertEqual(len(searches), 2)
        self.assertEqual(set(searches[0]), {ACTIVE_MINT, QUIET_MINT})
        self.assertEqual(searches[1], [ACTIVE_MINT])       # quiet one waits pp_cold_recheck_s
        self.clock.t += SCAN_FETCH["pp_cold_recheck_s"]
        self._cycles(1, s)
        last = [p["query"].split(",") for u, p in self.urls if "jup.ag" in u and u.endswith("/search")][-1]
        self.assertIn(QUIET_MINT, last)

    def test_quotes_fall_back_to_jupiter_when_dexscreener_429s(self):
        with mock.patch("time.time", self.clock):
            q = scanner.quote_many([ACTIVE_MINT], self._session())
        self.assertIn(ACTIVE_MINT, q)
        self.assertAlmostEqual(q[ACTIVE_MINT]["price_usd"], 0.0002)
        self.assertEqual(scanner.socials(ACTIVE_MINT, self._session())["x"], "https://x.com/catcoin")

    def test_jupiter_switches_base_after_a_404(self):
        self._cycles(1, self._session(jup_status=404))
        self.assertTrue(scanner._jup_endpoints()[0]["url"].startswith(scanner.JUP_BASES[1]))

    def test_jitter_never_undercuts_retry_after(self):
        for _ in range(200):
            self.assertGreaterEqual(scanner._jitter(100.0), 100.0)
            self.assertLessEqual(scanner._jitter(100.0), 115.0 + 1e-9)

    def test_offline_disables_jupiter(self):
        self.p.stop()
        try:
            self.assertFalse(scanner._jup_enabled())       # conftest sets DESK_OFFLINE=1
        finally:
            self.p.start()


class RecentMints(unittest.TestCase):
    def setUp(self):
        pumpportal.reset()

    def test_window_and_order(self):
        now = 1_000_000.0
        launch("A" * 44, now - 7 * 3600)
        launch("B" * 44, now - 3600)
        launch("C" * 44, now - 600)
        launch("D" * 44, now - 30)
        self.assertEqual(pumpportal.recent_mints(now, 120, 6 * 3600), ["C" * 44, "B" * 44])
        self.assertEqual(pumpportal.recent_mints(now, 120, 6 * 3600, limit=1), ["C" * 44])


if __name__ == "__main__":
    unittest.main()
