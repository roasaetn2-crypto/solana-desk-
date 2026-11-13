"""Tests for the deterministic BUY/SELL signal engine and its backtest helper."""
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DESK_OFFLINE", "1")

import config  # noqa: E402
from desk import signal_engine as se  # noqa: E402
from desk import signal_backtest as sb  # noqa: E402

GOOD = {"mint": "GOODMINT", "symbol": "GOOD", "price_usd": 0.001, "mcap_usd": 300_000,
        "liquidity_usd": 40_000, "volume_m5": 5000, "volume_h1": 90_000, "volume_h6": 150_000,
        "volume_h24": 300_000, "buys_h1": 500, "sells_h1": 300, "buyers_h1": 220,
        "change_m5": 3, "change_h1": 60, "change_h6": 80, "age_minutes": 45}
GOOD_FACTS = {"mint_authority": False, "freeze_authority": False, "top_wallet_pct": 0.03,
              "top_10_pct": 0.2, "holders": 400, "supply": 1e9, "decimals": 6}
GOOD_DEV = {"hard_no": [], "dev_score": 90, "dev_hold_pct": 0.01, "creator_prev_rugs": 0,
            "sniper_pct": 0.05, "reasons": []}
GOOD_LINKS = {"x": "https://x.com/a", "telegram": "https://t.me/a", "website": "https://a.b"}
CLEAN = {"chips": [], "block": [], "halve": [], "unknown": [], "parts": {}}

WEAK = {"mint": "WEAKMINT", "symbol": "WEAK", "price_usd": 0.001, "mcap_usd": 300_000,
        "liquidity_usd": 8_000, "volume_m5": 20, "volume_h1": 400, "volume_h6": 3_000,
        "volume_h24": 40_000, "buys_h1": 30, "sells_h1": 120, "buyers_h1": 10,
        "change_m5": -6, "change_h1": -40, "change_h6": -55, "age_minutes": 300}


def _sig(token=GOOD, facts=GOOD_FACTS, dev=GOOD_DEV, links=GOOD_LINKS, sc=None,
         security=CLEAN, thesis=None, holding=False):
    return se.evaluate(token, facts, links, thesis=thesis, scorecard=sc or {"total": 72},
                       security=security, regime={"tag": "NEUTRAL"}, dev=dev, holding=holding)


class TestSignalEngine:
    def test_good_token_is_a_buy(self):
        sig = _sig(thesis={"score": 7, "verdict": "valid_setup"})
        assert sig["action"] == "BUY"
        assert sig["direction"] == "bullish"
        assert sig["confidence"] >= 60
        assert sig["win_probability"] >= 0.52
        assert sig["plan"]["entry"] and sig["plan"]["stop"] < sig["plan"]["entry"]
        assert sig["plan"]["tp1"] > sig["plan"]["entry"]
        assert "BUY" in sig["signal"]

    def test_weak_token_is_bearish_and_not_a_buy(self):
        sig = _sig(token=WEAK, facts={}, dev={}, links={}, sc={"total": 30})
        assert sig["action"] in ("AVOID", "HOLD")
        assert sig["direction"] == "bearish"
        assert sig["confidence"] >= 60
        ok, why = se.allows_entry(sig)
        assert not ok

    def test_hard_block_always_wins(self):
        block = {"chips": [{"t": "closable", "level": "block", "src": "goplus"}],
                 "block": ["closable"], "halve": [], "unknown": [], "parts": {}}
        sig = _sig(security=block)
        assert sig["action"] == "AVOID"
        assert sig["hard_block"] is True
        ok, why = se.allows_entry(sig)
        assert not ok and "hard" in why

    def test_confidence_is_conviction_in_direction(self):
        good = _sig(thesis={"score": 7, "verdict": "valid_setup"})
        weak = _sig(token=WEAK, facts={}, dev={}, links={}, sc={"total": 30})
        # both a strong bullish and a strong bearish read are high-confidence
        assert good["confidence"] >= 60
        assert weak["confidence"] >= 60

    def test_signal_text_and_prompt_present(self):
        sig = _sig(thesis={"score": 7, "verdict": "valid_setup"})
        assert sig["signal"].startswith("🟢 BUY")
        assert "BUY" in sig["prompt"]
        assert "take profit" in sig["prompt"].lower()

    def test_should_sell_on_bearish_flip(self):
        sig = {"action": "SELL", "direction": "bearish", "confidence": 82}
        sell, why = se.should_sell(sig)
        assert sell and "SELL" in why

    def test_weak_read_is_bearish(self):
        sig = _sig(token=WEAK, facts={}, dev={}, links={}, sc={"total": 30}, holding=True)
        assert sig["direction"] == "bearish"
        # below the sell bar it holds rather than dumping; a deeper read sells
        sell, _ = se.should_sell(sig)
        assert sell is (sig["action"] == "SELL" and sig["confidence"] >= config.SIGNAL["sell_confidence"])

    def test_should_not_sell_a_buy(self):
        sig = _sig(thesis={"score": 7, "verdict": "valid_setup"}, holding=True)
        sell, _ = se.should_sell(sig)
        assert not sell

    def test_gate_off_allows_entry(self):
        weak = _sig(token=WEAK, facts={}, dev={}, links={}, sc={"total": 30})
        with mock.patch.dict(config.SIGNAL, {"enabled": False}):
            ok, why = se.allows_entry(weak)
        assert ok and "off" in why

    def test_require_bullish_toggle(self):
        weak = _sig(token=WEAK, facts={}, dev={}, links={}, sc={"total": 30})
        with mock.patch.dict(config.SIGNAL, {"require_bullish": False}):
            ok, _ = se.allows_entry(weak)
        # even with require_bullish off, a non-BUY action is still refused
        assert not ok


class TestSignalBacktest:
    def _path(self, trend, n=140, seed=3):
        import random
        rng = random.Random(seed)
        price = 0.001
        candles = []
        for i in range(n):
            o = price
            price = max(1e-9, price * (1 + trend + rng.gauss(0, 0.015)))
            candles.append({"ts": 1_700_000_000 + i * 300, "open": o,
                            "high": max(o, price), "low": min(o, price),
                            "close": price, "vol": abs(rng.gauss(5000, 1500))})
        return {"candles": candles, "horizon": "scalp", "name": "T"}

    def test_simulate_returns_a_real_outcome(self):
        path = self._path(0.0)
        t = sb.simulate(path["candles"], 40, "scalp")
        assert t is not None
        assert -100.0 < t["ret_pct"] < 500.0
        assert t["reasons"]

    def test_run_backtest_produces_buckets_and_sweep(self):
        paths = [self._path(0.004, seed=s) for s in range(4)] + \
                [self._path(-0.004, seed=100 + s) for s in range(4)]
        rep = sb.run_backtest(paths, bar_minutes=5, step=5, min_history=30)
        assert rep["entries"] > 0
        assert "overall" in rep and "win_rate" in rep["overall"]
        assert rep["by_confidence"]
        assert rep["gate"]["all_entries"]["trades"] >= rep["gate"]["signal_buys_only"]["trades"]
        assert rep["tp_sweep"] and "profile (default)" in rep["tp_sweep"]

    def test_features_only_use_the_past(self):
        path = self._path(0.002)
        i = 60
        f = sb.features_from_candles(path["candles"], i, bar_minutes=5)
        # price must equal the entry candle's close, never a later one
        assert f["price_usd"] == path["candles"][i]["close"]
        assert f["age_minutes"] == 5 * (i + 1)


class TestSettingsRegistered:
    def test_signal_and_tp_settings_exist(self):
        from desk import settings
        for k in ("signal.enabled", "signal.min_confidence", "signal.min_win_probability",
                  "signal.require_bullish", "signal.sell_on_bearish", "signal.sell_confidence",
                  "exits.tp_plan", "exits.global.tp1_at", "exits.single.tp_at"):
            assert k in settings.REGISTRY, k

    def test_tp_plan_choice_coerces(self):
        from desk import settings
        s = settings.REGISTRY["exits.tp_plan"]
        assert s.coerce("global")[0] == "global"
        try:
            s.coerce("nonsense")
        except ValueError:
            pass
        else:
            raise AssertionError("bad choice should raise")

    def test_exits_profile_honors_tp_plan(self):
        from desk import exits
        with mock.patch.dict(config.EXITS, {"take_profit_mode": "single"}):
            pr = exits.profile("scalp")
            assert pr["tp2_frac"] == 0.0
            assert pr["tp1_at"] == config.EXITS["single"]["tp_at"]
        with mock.patch.dict(config.EXITS, {"take_profit_mode": "global"}):
            pr = exits.profile("scalp")
            assert pr["tp1_at"] == config.EXITS["global"]["tp1_at"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
