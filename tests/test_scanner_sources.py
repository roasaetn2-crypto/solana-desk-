"""Scanner fetch: DexScreener fallback, merge/dedup, visible 429s. No live network."""
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
        scanner._gt_skip_until = 0.0
        scanner._reset_last()

    def _session(self, router):
        s = mock.Mock()
        s.get.side_effect = router
        return s

    def test_429_is_logged_and_does_not_raise(self):
        calls = {"n": 0}

        def router(url, params=None, headers=None, timeout=None):
            calls["n"] += 1
            if "geckoterminal" in url:
                return FakeResp(429, reason="Too Many Requests")
            if url.endswith("/latest/dex/search"):
                return FakeResp(200, {"pairs": [_ds_pair()]})
            if "token-profiles" in url or "token-boosts" in url:
                return FakeResp(200, [])
            return FakeResp(200, {"pairs": []})

        with mock.patch.object(scanner.time, "sleep"):
            tokens = scanner.new_tokens(self._session(router))
        self.assertTrue(any(t["source"] == "dexscreener" for t in tokens))
        self.assertTrue(any("429" in e for e in scanner.LAST["geckoterminal"]["errors"]))
        report = scanner.fetch_report()
        self.assertTrue(any("429" in w for w in report["warnings"]))
        self.assertGreater(scanner.LAST["geckoterminal"]["calls"], 0)
        # cooldown latched so the next cycle skips GeckoTerminal for a minute
        self.assertGreater(scanner._gt_skip_until, time.time())

    def test_forbidden_is_logged_with_status(self):
        def router(url, params=None, headers=None, timeout=None):
            if "geckoterminal" in url:
                return FakeResp(403, reason="Forbidden")
            if url.endswith("/latest/dex/search"):
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
            if "geckoterminal" in url and "new_pools" in url:
                return FakeResp(200, {"data": [], "included": []})
            if "geckoterminal" in url:
                return FakeResp(200, _gt_body("SHARED", "DOG"))
            if url.endswith("/latest/dex/search"):
                # same mint as GT, plus a unique DS mint
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
        self.assertEqual(scanner.source_label(tokens), "geckoterminal+dexscreener-empty")
