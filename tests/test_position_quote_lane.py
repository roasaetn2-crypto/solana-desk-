"""Open-position prices must not be starved by scanner discovery (Last == Entry for minutes)."""
import time

from desk import scanner


class _R:
    def json(self):
        return [{"ok": 1}]


def _ep():
    return {"id": "ds:tokens-v1", "source": "dexscreener", "url": "http://x", "params": None, "label": "t"}


def test_positions_lane_works_when_shared_budget_is_empty_and_host_is_cooling(monkeypatch):
    scanner.reset() if hasattr(scanner, "reset") else None
    now = time.time()
    monkeypatch.setattr(scanner, "_get_once", lambda *a, **k: _R())
    scanner._BUCKETS["dexscreener"].tokens = 0.0
    scanner._BUCKETS["dexscreener"].t = now
    scanner._HOST_COOL["dexscreener"] = {"until": now + 120, "strikes": 1}
    try:
        assert scanner._fetch_live(None, _ep(), now) is None                      # scanning is starved
        assert scanner._fetch_live(None, _ep(), now, positions=True) == [{"ok": 1}]  # positions are not
    finally:
        scanner._HOST_COOL.pop("dexscreener", None)
        scanner._BUCKETS = scanner._new_buckets()


def test_positions_lane_has_its_own_limit(monkeypatch):
    now = time.time()
    monkeypatch.setattr(scanner, "_get_once", lambda *a, **k: _R())
    scanner._BUCKETS = scanner._new_buckets()
    scanner._BUCKETS["dexscreener_pos"].tokens = 0.0
    scanner._BUCKETS["dexscreener_pos"].t = now
    assert scanner._fetch_live(None, _ep(), now, positions=True) is None          # bounded, not unlimited
    scanner._BUCKETS = scanner._new_buckets()


def test_unindexed_mint_is_priced_by_jupiter_not_its_stale_entry_quote(monkeypatch):
    """DexScreener answers 200 with no pair for a fresh pool: Last must not stay at Entry."""
    scanner._QUOTE_CACHE.clear(); scanner._PAIR_CACHE.clear()
    mint = "MintNotIndexedYet11111111111111111111111111"
    scanner._QUOTE_CACHE[mint] = {"exp": 0.0, "q": {"price_usd": 1.0}}          # the stale ENTRY quote
    monkeypatch.setattr(scanner, "_fetch_live", lambda *a, **k: [])               # 200 OK, zero pairs
    monkeypatch.setattr(scanner, "_jup_quotes", lambda s, m, n: {mint: {"price_usd": 1.25}})
    out = scanner.quote_many([mint], None)
    assert out[mint]["price_usd"] == 1.25
    scanner._QUOTE_CACHE.clear()


def test_unindexed_mint_with_no_jupiter_price_still_falls_back_to_stale(monkeypatch):
    scanner._QUOTE_CACHE.clear(); scanner._PAIR_CACHE.clear()
    mint = "MintNotIndexedYet22222222222222222222222222"
    scanner._QUOTE_CACHE[mint] = {"exp": 0.0, "q": {"price_usd": 1.0}}
    monkeypatch.setattr(scanner, "_fetch_live", lambda *a, **k: [])
    monkeypatch.setattr(scanner, "_jup_quotes", lambda s, m, n: {})
    assert scanner.quote_many([mint], None)[mint]["price_usd"] == 1.0
    scanner._QUOTE_CACHE.clear()
