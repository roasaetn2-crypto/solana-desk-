"""Regression: quoting several mints in one process must not poison itself.

The tokens-v1 quote endpoint takes its mint list in the URL. It used to cache
its body under the single key "ds:tokens-v1", so the first mint quoted in a
90s window was served back for every other mint, quote() returned None, and
every approved entry died at the confirm step as "no_confirm_quote".
"""
import unittest

from desk import scanner


class QuoteCacheIsolation(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()

    def tearDown(self):
        scanner.reset_runtime()

    def test_varying_url_is_not_served_from_the_shared_body_cache(self):
        """Two different chunk URLs must not share one cached body."""
        now = 1_000_000.0
        body_a = [{"baseToken": {"address": "MintA"}, "priceUsd": "1.0",
                   "liquidity": {"usd": 50_000}}]

        scanner._store_cache("ds:tokens-v1", body_a, now)
        cached, fresh = scanner._cached_body("ds:tokens-v1", now)
        self.assertTrue(fresh)
        self.assertEqual(cached, body_a)

        # The quote path must not consult that slot at all: it fetches live and
        # relies on the per-mint caches instead.
        import inspect
        code = inspect.getsource(scanner.quote_many)
        self.assertNotIn("_fetch_endpoint", code,
                         "quote_many must not use the shared body cache: its URL "
                         "varies per mint chunk")
        self.assertIn("_fetch_live", code)

    def test_per_mint_quote_cache_is_keyed_by_mint(self):
        """A warm per-mint quote must be reused without a network call."""
        now = 1_000_000.0
        q = {"price_usd": 0.5, "liquidity_usd": 10_000}
        scanner._QUOTE_CACHE["MintX"] = {"exp": now + 60, "q": q}
        out = scanner.quote_many(["MintX"], session=None)
        self.assertIn("MintX", out)
        self.assertEqual(out["MintX"]["price_usd"], 0.5)

    def test_pairs_cache_is_keyed_by_mint(self):
        now = 1_000_000.0
        scanner._PAIR_CACHE["MintY"] = {"exp": now + 60, "pairs": [
            {"baseToken": {"address": "MintY"}, "priceUsd": "3.0",
             "liquidity": {"usd": 20_000}}]}
        self.assertIsNotNone(scanner.cached_raw_pairs("MintY", now))
        self.assertIsNone(scanner.cached_raw_pairs("MintZ", now))


if __name__ == "__main__":
    unittest.main()
