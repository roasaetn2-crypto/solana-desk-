"""The memecoin-guide tactics must stay OFF until a measurement says otherwise.

The public "high-profit meme bot" guide proposes four behavioural rules on top
of its indicator toolkit (which this repo already tested and rejected). These
tests pin three things:

  1. every tactic ships disabled in config,
  2. the gate functions behave as described (pullback / flash-pump),
  3. the evaluation harness computes the same verdict bar as the indicator gate.

Nothing here asserts that a tactic *works* -- that is the job of
``scripts/meme_tactics_eval.py`` on real candles. These tests only stop a tactic
from being quietly switched on, or the bar from being quietly moved.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
from backtest_gates import flash_ok, pullback_ok  # noqa: E402


def test_tactics_ship_disabled():
    """No memecoin tactic may be on by default."""
    for key in ("pullback_entry", "flash_pump_guard", "atr_stop", "vol_ceiling"):
        assert config.MEME[key] is False, f"{key} must default to False"


def test_pullback_gate_blocks_the_top():
    hi = 100.0
    at_top = {"price": 100.0, "high_20": hi}
    deep_dip = {"price": 75.0, "high_20": hi}
    assert pullback_ok({}, at_top, {}) == (False, "at_high")
    assert pullback_ok({}, deep_dip, {}) == (True, "")


def test_pullback_gate_is_neutral_without_a_high():
    """No 20-bar high yet -> do not invent a rejection."""
    assert pullback_ok({}, {"price": 1.0, "high_20": None}, {}) == (True, "")


def test_flash_gate_blocks_a_vertical_candle():
    thr = config.MEME["flash_pump_pct"]
    spike = {"chg_5m": thr + 0.01}
    calm = {"chg_5m": thr - 0.01}
    assert flash_ok({}, spike, {}) == (False, "flash_pump")
    assert flash_ok({}, calm, {}) == (True, "")


def test_flash_gate_is_neutral_on_the_first_bar():
    assert flash_ok({}, {"chg_5m": None}, {}) == (True, "")


def test_harness_uses_the_same_bar_as_the_indicator_gate():
    from desk import indicator_backtest as ib
    import meme_tactics_eval as mt

    assert mt.ib.MIN_TRADES == ib.MIN_TRADES == 30
    assert mt.ib.MIN_WIN_LIFT == ib.MIN_WIN_LIFT == 0.05
    assert mt.ib.MIN_PF == ib.MIN_PF == 1.2


def test_verdict_rejects_a_filter_that_only_shrinks_the_sample():
    from desk import indicator_backtest as ib

    baseline = {"trades": 100, "win_rate": 0.20, "profit_factor": 0.9}
    shrunk = {"trades": 40, "win_rate": 0.21, "profit_factor": 0.9}
    assert ib.verdict(shrunk, baseline).startswith("DO-NOT-ADD")


def test_verdict_rejects_high_win_rate_with_low_profit_factor():
    """The pullback rule's real result: win rate up, still losing money."""
    from desk import indicator_backtest as ib

    baseline = {"trades": 3629, "win_rate": 0.154, "profit_factor": 0.503}
    pullback = {"trades": 424, "win_rate": 0.269, "profit_factor": 0.745}
    verdict = ib.verdict(pullback, baseline)
    assert verdict.startswith("DO-NOT-ADD")
    assert "PF" in verdict


# --------------------------------------------------------------------------- #
# desk/meme_tactics.py -- the live wiring
# --------------------------------------------------------------------------- #
def _sig(action="BUY"):
    return {"action": action, "direction": "bullish", "confidence": 70,
            "take_profit": {"stop_pct": 0.20, "tp1_at": 1.5, "tp1_frac": 0.33,
                            "tp2_at": 2.0, "tp2_frac": 0.5, "trail_pct": 0.20},
            "plan": {}, "reasons": [], "warnings": []}


def _with_flags(monkeypatch, **flags):
    import config
    for k, v in flags.items():
        monkeypatch.setitem(config.MEME, k, v)


def test_apply_is_a_noop_when_every_tactic_is_off(monkeypatch):
    from desk import meme_tactics
    _with_flags(monkeypatch, pullback_entry=False, flash_pump_guard=False,
                atr_stop=False, vol_ceiling=False)
    sig = _sig()
    out = meme_tactics.apply(sig, {"price_usd": 100.0}, {"high_20": 100.0})
    assert out["action"] == "BUY"
    assert out["take_profit"]["tp1_at"] == 1.5


def test_pullback_gate_downgrades_a_buy_at_the_high(monkeypatch):
    from desk import meme_tactics
    _with_flags(monkeypatch, pullback_entry=True)
    out = meme_tactics.apply(_sig(), {"price_usd": 100.0}, {"high_20": 100.0})
    assert out["action"] == "HOLD"
    assert "meme tactic" in out["warnings"][0]


def test_pullback_gate_lets_a_deep_dip_through_and_swaps_the_ladder(monkeypatch):
    from desk import meme_tactics
    _with_flags(monkeypatch, pullback_entry=True)
    out = meme_tactics.apply(_sig(), {"price_usd": 75.0}, {"high_20": 100.0})
    assert out["action"] == "BUY"
    assert out["take_profit"]["stop_pct"] == 0.30
    assert out["take_profit"]["tp1_at"] == 2.0
    assert out["take_profit"]["tp2_at"] == 3.0


def test_pullback_gate_is_neutral_when_the_high_is_unknown(monkeypatch):
    from desk import meme_tactics
    _with_flags(monkeypatch, pullback_entry=True)
    out = meme_tactics.apply(_sig(), {"price_usd": 100.0}, {})
    assert out["action"] == "BUY"
    assert any("unchecked" in w for w in out["warnings"])


def test_a_gate_never_raises_a_hold_into_a_buy(monkeypatch):
    from desk import meme_tactics
    _with_flags(monkeypatch, pullback_entry=True)
    out = meme_tactics.apply(_sig("HOLD"), {"price_usd": 50.0}, {"high_20": 100.0})
    assert out["action"] == "HOLD"


def test_flash_guard_blocks_a_chase(monkeypatch):
    from desk import meme_tactics
    _with_flags(monkeypatch, flash_pump_guard=True)
    out = meme_tactics.apply(_sig(), {"price_usd": 1.0, "change_m5": 0.40}, {})
    assert out["action"] == "HOLD"


def test_exit_overrides_only_apply_when_the_package_is_on(monkeypatch):
    from desk import exits, meme_tactics
    _with_flags(monkeypatch, pullback_entry=False)
    assert meme_tactics.exit_overrides() == {}
    assert exits.profile("scalp")["tp1_at"] == 1.5
    _with_flags(monkeypatch, pullback_entry=True)
    assert exits.profile("scalp")["tp1_at"] == 2.0
    assert exits.profile("scalp")["stop_pct"] == 0.30


def test_atr_stop_is_clamped():
    from desk import meme_tactics
    assert meme_tactics.atr_stop_pct(0.001) == 0.08
    assert meme_tactics.atr_stop_pct(0.90) == 0.60
    assert meme_tactics.atr_stop_pct(None) is None
