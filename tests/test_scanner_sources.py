"""Scanner fetch: DexScreener fallback, merge/dedup, limiter, cooldown. No live network."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk import scanner


class FakeResp:
    def __init__(self, status, payload=None, reason="", headers=None):
        self.status_code = status
        self.reason = reason or {200: "OK", 429: "Too Many Requests", 403: "Forbidden"}.get(status, "")
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _gt_pool(mint="MINT1", symbol="DOG", dex="pumpswap", liq=20_000):
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1800))
    return {
        "attributes": {
            "address": f"pool-{mint}",
            "name": f"{symbol} / SOL",
            "base_token_price_usd": "0.001",
            "market_cap_usd": "150000",
            "fdv_usd": "150000",
            "reserve_in_usd": str(liq),
            "volume_usd": {"h1": "50000", "h24": "90000"},
            "transactions": {"h1": {"buys": 300, "sells": 200, "buyers": 180}},
            "price_change_percentage": {"h1": "40"},
            "pool_created_at": created,
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_{mint}"}},
            "dex": {"data": {"id": dex}},
        },
    }


def _gt_body(mint="MINT1", symbol="DOG"):
    pool = _gt_pool(mint, symbol)
    return {
        "data": [pool],
        "included": [{"id": f"solana_{mint}", "type": "token",
                      "attributes": {"symbol": symbol}}],
    }


def _ds_pair(mint="MINT2", symbol="CAT", dex="raydium", liq=30_000, chain="solana"):
    return {
        "chainId": chain,
        "dexId": dex,
        "pairAddress": f"pair-{mint}",
        "baseToken": {"address": mint, "name": symbol, "symbol": symbol},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112",
                       "name": "SOL", "symbol": "SOL"},
        "priceUsd": "0.002",
        "txns": {"h1": {"buys": 80, "sells": 40}},
        "volume": {"m5": 100, "h1": 9000, "h6": 20000, "h24": 50000},
        "priceChange": {"m5": 1, "h1": 10, "h6": 20},
        "liquidity": {"usd": liq},
        "fdv": 180000,
        "marketCap": 160000,
        "pairCreatedAt": int((time.time() - 3600) * 1000),
    }


class ParseDexPair(unittest.TestCase):
    def test_solana_pair_normalizes(self):
        t = scanner.parse_dex_pair(_ds_pair())
        self.assertEqual(t["mint"], "MINT2")
        self.assertEqual(t["symbol"], "CAT")
        self.assertEqual(t["dex"], "raydium")
        self.assertEqual(t["source"], "dexscreener")
        self.assertEqual(t["liquidity_usd"], 30_000)
        self.assertEqual(t["buys_h1"], 80)
        self.assertAlmostEqual(t["age_minutes"], 60, delta=2)

    def test_pumpfun_dex_aliased(self):
        t = scanner.parse_dex_pair(_ds_pair(dex="pumpfun"))
        self.assertEqual(t["dex"], "pump-fun")

    def test_skips_other_chains_and_quote_mints(self):
        self.assertIsNone(scanner.parse_dex_pair(_ds_pair(chain="base")))
        sol = "So11111111111111111111111111111111111111112"
        self.assertIsNone(scanner.parse_dex_pair(_ds_pair(mint=sol)))


class FetchAndMerge(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()

    def _session(self, router):
        s = mock.Mock()
        s.get.side_effect = router
        return s

    def test_429_is_logged_once_and_does_not_sleep(self):
        calls = {"gt": 0, "ds": 0}

        def router(url, params=None, headers=None, timeout=None):
            if "geckoterminal" in url:
                calls["gt"] += 1
                return FakeResp(429, reason="Too Many Requests",
                                headers={"Retry-After": "1"})
            calls["ds"] += 1
            if "/latest/dex/search" in url:
                return FakeResp(200, {"pairs": [_ds_pair()]})
            if "token-profiles" in url or "token-boosts" in url:
                return FakeResp(200, [])
            if "/tokens/v1/solana" in url:
                return FakeResp(200, [_ds_pair()])
            return FakeResp(200, {"pairs": []})

        sleep = mock.Mock()
        with mock.patch.object(scanner.time, "sleep", sleep):
            tokens = scanner.new_tokens(self._session(router))
        self.assertTrue(any(t["source"] == "dexscreener" for t in tokens))
        self.assertEqual(calls["gt"], 1)  # no in-cycle retries
        self.assertTrue(any("429" in e for e in scanner.LAST["geckoterminal"]["errors"]))
        self.assertEqual(sum(1 for e in scanner.LAST["geckoterminal"]["errors"] if "429" in e), 1)
        self.assertTrue(scanner.is_cooling("gt:new_pools:1"))
        sleep.assert_not_called()

        # page 1 stays cooling. keep cycle even so page 2 is not scheduled.
        scanner._cycle = 2
        gt_before = calls["gt"]
        again = scanner.new_tokens(self._session(router))
        self.assertEqual(calls["gt"], gt_before)
        self.assertTrue(any(t["mint"] == "MINT2" for t in again))

    def test_forbidden_is_logged_with_status(self):
        def router(url, params=None, headers=None, timeout=None):
            if "geckoterminal" in url:
                return FakeResp(403, reason="Forbidden")
            if "/latest/dex/search" in url:
                return FakeResp(200, {"pairs": [_ds_pair()]})
            if "token-profiles" in url or "token-boosts" in url:
                return FakeResp(200, [])
            return FakeResp(200, {"pairs": []})

        tokens = scanner.new_tokens(self._session(router))
        self.assertTrue(tokens)
        self.assertTrue(any("403" in e and "Forbidden" in e
                            for e in scanner.LAST["geckoterminal"]["errors"]))

    def test_merge_dedupes_by_mint_preferring_geckoterminal_first(self):
        def router(url, params=None, headers=None, timeout=None):
            # Primary GT path is now new_pools (page 1 every cycle).
            if "geckoterminal" in url and "new_pools" in url:
                return FakeResp(200, _gt_body("SHARED", "DOG"))
            if "geckoterminal" in url:
                return FakeResp(200, {"data": [], "included": []})
            if "/latest/dex/search" in url:
                return FakeResp(200, {"pairs": [
                    _ds_pair(mint="SHARED", symbol="DOG", liq=99_000),
                    _ds_pair(mint="ONLYDS", symbol="CAT"),
                ]})
            if "token-profiles" in url or "token-boosts" in url:
                return FakeResp(200, [])
            return FakeResp(200, {"pairs": []})

        tokens = scanner.new_tokens(self._session(router))
        by_mint = {t["mint"]: t for t in tokens}
        self.assertIn("SHARED", by_mint)
        self.assertIn("ONLYDS", by_mint)
        self.assertEqual(by_mint["SHARED"]["source"], "geckoterminal")
        self.assertEqual(by_mint["ONLYDS"]["source"], "dexscreener")
        self.assertEqual(scanner.source_label(tokens), "geckoterminal+dexscreener")
        self.assertEqual(scanner.source_counts(tokens)["geckoterminal"], 1)
        self.assertEqual(scanner.source_counts(tokens)["dexscreener"], 1)

    def test_empty_sources_label(self):
        def router(url, params=None, headers=None, timeout=None):
            if "geckoterminal" in url:
                return FakeResp(200, {"data": [], "included": []})
            if "token-profiles" in url or "token-boosts" in url:
                return FakeResp(200, [])
            return FakeResp(200, {"pairs": []})

        tokens = scanner.new_tokens(self._session(router))
        self.assertEqual(tokens, [])
        self.assertEqual(scanner.source_label(tokens), "all-sources-empty")

    def test_one_search_query_per_cycle(self):
        searches = []

        def router(url, params=None, headers=None, timeout=None):
            if "geckoterminal" in url:
                return FakeResp(200, {"data": [], "included": []})
            if "/latest/dex/search" in url:
                searches.append((params or {}).get("q"))
                return FakeResp(200, {"pairs": []})
            if "token-profiles" in url or "token-boosts" in url:
                return FakeResp(200, [])
            return FakeResp(200, {"pairs": []})

        scanner.new_tokens(self._session(router))
        self.assertLessEqual(len(searches), 1)
        if searches:
            self.assertIn(searches[0], {"pump", "SOL"})


class LimiterAndCooldown(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()

    def test_token_bucket_burst_then_deny_then_refill(self):
        b = scanner.TokenBucket(per_min=60, burst=2)
        t0 = 1_000.0
        b.reset(now=t0)
        self.assertTrue(b.allow(1, now=t0))
        self.assertTrue(b.allow(1, now=t0))
        self.assertFalse(b.allow(1, now=t0))
        # 60/min = 1 token/sec; after 1s we can take one more
        self.assertTrue(b.allow(1, now=t0 + 1.0))
        self.assertFalse(b.allow(1, now=t0 + 1.0))

    def test_429_cools_endpoint_and_is_logged_once(self):
        hits = {"n": 0}

        def router(url, params=None, headers=None, timeout=None):
            hits["n"] += 1
            if "geckoterminal" in url:
                return FakeResp(429, reason="Too Many Requests")
            return FakeResp(200, {"pairs": []})

        s = mock.Mock()
        s.get.side_effect = router
        sleep = mock.Mock()
        with mock.patch.object(scanner.time, "sleep", sleep):
            scanner.new_tokens(s)
            first_hits = hits["n"]
            first_429 = scanner.CALLS["429"]
            scanner._cycle = 2  # even: only page 1 + one DS slot
            scanner.new_tokens(s)
        self.assertEqual(scanner.CALLS["429"], first_429)  # no second 429 log/count
        # cooling GT page 1 was not hit again; DS rotation may still fire
        self.assertLessEqual(hits["n"], first_hits + 2)
        sleep.assert_not_called()
        self.assertTrue(scanner.is_cooling("gt:new_pools:1"))

    def test_quote_many_batches_and_uses_cache(self):
        urls = []

        def router(url, params=None, headers=None, timeout=None):
            urls.append(url)
            pairs = [
                _ds_pair(mint="AAAA", symbol="A", liq=10_000),
                _ds_pair(mint="BBBB", symbol="B", liq=11_000),
            ]
            return FakeResp(200, pairs)

        s = mock.Mock()
        s.get.side_effect = router
        out = scanner.quote_many(["AAAA", "BBBB"], s)
        self.assertEqual(set(out), {"AAAA", "BBBB"})
        self.assertEqual(len(urls), 1)
        self.assertIn("/tokens/v1/solana/", urls[0])
        self.assertIn("AAAA", urls[0])
        self.assertIn("BBBB", urls[0])
        # second call is served from the short quote cache
        out2 = scanner.quote_many(["AAAA", "BBBB"], s)
        self.assertEqual(len(urls), 1)
        self.assertEqual(out2["AAAA"]["price_usd"], out["AAAA"]["price_usd"])
