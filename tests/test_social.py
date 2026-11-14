"""The social reader must be free, keyless, fail-open, and never a hidden gate.

`desk/social.py` reads ticker-keyed attention from Telegram public previews and
DexScreener's boosts/profiles feeds. None of those need an API key. These tests
pin the four properties that make it safe to run on a live desk:

  1. parsing is pure and correct (views, tickers, word boundaries),
  2. every network failure becomes "unknown", never an exception,
  3. the score is bounded 0-100 and deterministic,
  4. `apply()` records a pulse but cannot change a signal while the gate is off.

The last one matters most: a social number that has not been measured is a
story. Until the measurement clears the bar, this module is a camera.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from desk import social  # noqa: E402


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_views_parse_k_m_b():
    assert social._views_to_int("57.7K") == 57700
    assert social._views_to_int("1.2M") == 1200000
    assert social._views_to_int("2B") == 2000000000
    assert social._views_to_int("") == 0
    assert social._views_to_int(None) == 0
    assert social._views_to_int("junk") == 0


def test_parse_telegram_pulls_text_time_views():
    page = (
        '<div class="tgme_widget_message_text js-message_text">Buy $WIF now</div>'
        '<div class="tgme_widget_message_views">12.5K</div>'
        '<div class="tgme_widget_message_text js-message_text">gm</div>'
        '<div class="tgme_widget_message_views">3</div>'
    )
    msgs = social.parse_telegram(page)
    assert len(msgs) == 2
    assert msgs[0]["text"] == "Buy $WIF now"
    assert msgs[0]["views"] == 12500
    assert msgs[1]["views"] == 3


def test_parse_telegram_strips_tags_and_empty_blocks():
    page = ('<div class="tgme_widget_message_text"><b>bold</b> <a href="#">link</a></div>'
            '<div class="tgme_widget_message_text">   </div>')
    msgs = social.parse_telegram(page)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "bold link"


def test_parse_telegram_handles_empty_input():
    assert social.parse_telegram("") == []
    assert social.parse_telegram(None) == []


# --------------------------------------------------------------------------- #
# ticker matching
# --------------------------------------------------------------------------- #
def test_ticker_matches_dollar_and_bare_forms():
    pat = social.ticker_pattern("WIF")
    assert pat.search("buy $WIF")
    assert pat.search("WIF to the moon")
    assert not pat.search("WIFEY is different")
    assert not pat.search("SWIFT")


def test_ticker_is_case_insensitive():
    pat = social.ticker_pattern("wif")
    assert pat.search("$WIF pumping")


def test_ticker_rejects_junk():
    assert social.ticker_pattern("") is None
    assert social.ticker_pattern("$") is None
    assert social.ticker_pattern("a") is None          # too short
    assert social.ticker_pattern("waytoolongticker") is None
    assert social.ticker_pattern("bad ticker") is None  # space


def test_count_mentions_sums_views():
    msgs = [{"text": "buy $WIF", "views": 100},
            {"text": "gm", "views": 999},
            {"text": "WIF again", "views": 50}]
    c = social.count_mentions(msgs, "WIF")
    assert c["messages"] == 2
    assert c["views"] == 150
    assert len(c["samples"]) == 2


def test_count_mentions_with_bad_ticker_is_zero():
    assert social.count_mentions([{"text": "x", "views": 5}], "")["messages"] == 0


# --------------------------------------------------------------------------- #
# fail-open
# --------------------------------------------------------------------------- #
def test_offline_everything_is_unknown(monkeypatch):
    monkeypatch.setenv("DESK_OFFLINE", "1")
    social._cache.clear()
    assert social.telegram("binance_announcements")["available"] is False
    assert social.dex_boosts()["available"] is False
    assert social.dex_profiles()["available"] is False


def test_telegram_with_no_channel_is_unknown():
    r = social.telegram("")
    assert r["available"] is False
    assert r["messages"] == []


def test_presence_with_no_mint_is_all_false():
    p = social.presence("")
    assert p["boosted"] is False and p["profiled"] is False


def test_pulse_when_disabled(monkeypatch):
    monkeypatch.setitem(config.SOCIAL, "enabled", False)
    p = social.pulse("WIF")
    assert p["available"] is False
    assert p["why"] == "social off"


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #
def test_score_is_bounded_and_deterministic():
    for pulse in ({"messages": 0}, {"messages": 1000, "views": 10**9,
                                    "boosted": True, "has_x": True, "has_telegram": True},
                  {"messages": 5, "views": 30000}):
        s = social.score(pulse)
        assert 0 <= s <= 100
        assert s == social.score(pulse)


def test_score_rewards_messages_views_and_boost():
    base = social.score({"messages": 0, "views": 0})
    assert social.score({"messages": 5, "views": 0}) > base
    assert social.score({"messages": 0, "views": 30000}) > base
    assert social.score({"messages": 0, "views": 0, "boosted": True}) > base


def test_score_saturates_at_100():
    huge = {"messages": 10**6, "views": 10**12, "boosted": True,
            "has_x": True, "has_telegram": True}
    assert social.score(huge) == 100


# --------------------------------------------------------------------------- #
# apply(): a camera, not a gate
# --------------------------------------------------------------------------- #
def _sig(action="BUY"):
    return {"action": action, "warnings": [], "reasons": []}


def test_apply_records_pulse_without_changing_action(monkeypatch):
    monkeypatch.setitem(config.SOCIAL, "gate", False)
    monkeypatch.setitem(config.SOCIAL, "shadow", True)
    monkeypatch.setattr(social, "pulse", lambda *a, **k: {
        "available": True, "score": 0, "ticker": "WIF", "messages": 0})
    sig = social.apply(_sig(), {"symbol": "WIF", "mint": "M" * 32}, {})
    assert sig["action"] == "BUY"
    assert sig["social_pulse"]["score"] == 0


def test_apply_is_a_noop_when_shadow_off(monkeypatch):
    monkeypatch.setitem(config.SOCIAL, "shadow", False)
    monkeypatch.setitem(config.SOCIAL, "gate", False)
    monkeypatch.setattr(social, "pulse", lambda *a, **k: {"available": True, "score": 99})
    sig = social.apply(_sig(), {"symbol": "WIF"}, {})
    assert "social_pulse" not in sig
    assert sig["action"] == "BUY"


def test_gate_can_only_downgrade_a_buy(monkeypatch):
    monkeypatch.setitem(config.SOCIAL, "gate", True)
    monkeypatch.setitem(config.SOCIAL, "min_score", 50)
    monkeypatch.setattr(social, "pulse", lambda *a, **k: {"available": True, "score": 0})
    sig = social.apply(_sig("BUY"), {"symbol": "WIF"}, {})
    assert sig["action"] == "HOLD"
    assert "social gate" in sig["warnings"][0]
    # a HOLD must never become a BUY
    sig2 = social.apply(_sig("HOLD"), {"symbol": "WIF"}, {})
    assert sig2["action"] == "HOLD"


def test_gate_is_neutral_when_social_is_unavailable(monkeypatch):
    monkeypatch.setitem(config.SOCIAL, "gate", True)
    monkeypatch.setitem(config.SOCIAL, "min_score", 50)
    monkeypatch.setattr(social, "pulse", lambda *a, **k: {"available": False, "score": 0})
    sig = social.apply(_sig("BUY"), {"symbol": "WIF"}, {})
    assert sig["action"] == "BUY"


def test_apply_survives_a_pulse_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network on fire")
    monkeypatch.setattr(social, "pulse", boom)
    sig = social.apply(_sig(), {"symbol": "WIF"}, {})
    assert sig["action"] == "BUY"


def test_apply_without_a_ticker_returns_unchanged():
    sig = _sig()
    assert social.apply(sig, {"mint": "M" * 32}, {}) is sig


# --------------------------------------------------------------------------- #
# config defaults
# --------------------------------------------------------------------------- #
def test_social_ships_shadow_only():
    assert config.SOCIAL["enabled"] is True
    assert config.SOCIAL["shadow"] is True
    assert config.SOCIAL["gate"] is False, "a social gate must be earned, not shipped"


def test_watchlist_is_nonempty_and_clean():
    chans = config.SOCIAL["channels"]
    assert chans
    for c in chans:
        assert c and not c.startswith("@") and " " not in c


def test_signal_engine_tags_social_pulse(monkeypatch):
    """The engine must record the pulse on a real evaluate() call."""
    from desk import signal_engine
    monkeypatch.setenv("DESK_OFFLINE", "1")
    monkeypatch.setitem(config.SOCIAL, "enabled", True)
    monkeypatch.setitem(config.SOCIAL, "shadow", True)
    sig = signal_engine.evaluate(
        {"mint": "M" * 32, "symbol": "TEST", "name": "Test", "price_usd": 1.0},
        {"holders": 500}, {})
    assert "social_pulse" in sig
    assert sig["social_pulse"]["ticker"] == "TEST"
    assert sig["action"] in ("BUY", "HOLD", "SELL", "AVOID")
