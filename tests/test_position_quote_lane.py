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
