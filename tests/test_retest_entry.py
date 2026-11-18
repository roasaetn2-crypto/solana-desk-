"""Breakout-retest entry, and the candle-key bug that left the live dip gate switched off."""
import pytest

from desk import meme_tactics, settings


def _bars(short: bool):
    """20 flat bars (high 1.02, vol 10), a volume breakout, a retest dip, a bounce."""
    rows = [(1.0, 1.02, 0.99, 1.01, 10.0)] * 20
    rows += [(1.02, 1.11, 1.02, 1.10, 30.0),     # breakout: closes above 1.02 on 3x volume
             (1.09, 1.09, 1.015, 1.03, 10.0),    # retest: low back inside 3% of 1.02, level holds
             (1.03, 1.08, 1.03, 1.07, 12.0)]     # bounce: green, closes above the level
    keys = ("o", "h", "l", "c", "v") if short else ("open", "high", "low", "close", "vol")
    return [dict(zip(keys, r)) for r in rows]


@pytest.mark.parametrize("short", [True, False])
def test_bar_high_reads_both_candle_spellings(short):
    c = _bars(short)[0]
    assert meme_tactics.bar_high(c) == 1.02          # short keys used to return None here


def test_live_style_candles_now_give_a_20_bar_high():
    # exactly what solana_agent does with chart.py candles (short keys)
    highs = [h for h in (meme_tactics.bar_high(c) for c in _bars(True)[-20:]) if h]
    assert highs and max(highs) == 1.11


@pytest.mark.parametrize("short", [True, False])
def test_retest_passes_on_breakout_retest_bounce(short):
    st = meme_tactics.retest_state(_bars(short))
    assert st["ok"] is True and abs(st["resistance"] - 1.02) < 1e-9


def test_retest_needs_the_volume_surge():
    b = _bars(True)
    b[20]["v"] = 11.0                                # breakout on ordinary volume
    assert meme_tactics.retest_state(b)["ok"] is False


def test_retest_fails_when_the_level_does_not_hold():
    b = _bars(True)
    b[21]["c"] = 0.95                                # closed back under the broken level
    b[21]["l"] = 0.94
    assert meme_tactics.retest_state(b)["ok"] is False


def test_retest_fails_without_a_bounce():
    b = _bars(True)
    b[22]["c"], b[22]["o"] = 1.03, 1.06              # latest bar is red
    assert meme_tactics.retest_state(b)["ok"] is False


def test_retest_never_passes_on_short_or_missing_data():
    assert meme_tactics.retest_state([])["ok"] is False
    assert meme_tactics.retest_state(_bars(True)[:10])["ok"] is False


def _sig():
    return {"action": "BUY", "direction": "bullish", "confidence": 70,
            "take_profit": {"stop_pct": 0.3, "tp1_at": 1.25, "tp1_frac": 0.5, "tp2_at": 1.6,
                            "tp2_frac": 0.5, "trail_pct": 0.15},
            "plan": {}, "reasons": [], "warnings": []}


def _flags(monkeypatch, **kw):
    import config
    for k, v in kw.items():
        monkeypatch.setitem(config.MEME, k, v)


def test_dip_or_retest_lets_a_retest_in_when_the_dip_rule_says_no(monkeypatch):
    _flags(monkeypatch, pullback_entry=True, retest_entry=True)
    f = {"high_20": 100.0, "retest": {"ok": True, "why": "retest of broken resistance 1.02"}}
    out = meme_tactics.apply(_sig(), {"price_usd": 100.0}, f)     # at the high: dip rule alone refuses
    assert out["action"] == "BUY"
    assert any("retest" in r for r in out["reasons"])


def test_retest_off_changes_nothing(monkeypatch):
    _flags(monkeypatch, pullback_entry=True, retest_entry=False)
    f = {"high_20": 100.0, "retest": {"ok": True, "why": "x"}}
    assert meme_tactics.apply(_sig(), {"price_usd": 100.0}, f)["action"] == "HOLD"


def test_retest_on_but_no_retest_still_blocks_the_top(monkeypatch):
    _flags(monkeypatch, pullback_entry=True, retest_entry=True)
    f = {"high_20": 100.0, "retest": {"ok": False, "why": "no breakout-retest"}}
    assert meme_tactics.apply(_sig(), {"price_usd": 100.0}, f)["action"] == "HOLD"


def test_dip_still_passes_without_a_retest(monkeypatch):
    _flags(monkeypatch, pullback_entry=True, retest_entry=True)
    assert meme_tactics.apply(_sig(), {"price_usd": 75.0}, {"high_20": 100.0})["action"] == "BUY"


def test_settings_exist_and_default_off():
    assert settings.resolve("meme.retest_entry").get() is False
    for k in ("meme.retest_volume_mult", "meme.retest_zone_pct"):
        assert settings.resolve(k, owner=True).owner_only
