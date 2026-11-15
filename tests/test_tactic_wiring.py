"""The memecoin tactics must read facts the live loop actually produces.

Two bugs came out of the indicator audit (docs/reference/INDICATOR-USE.md):

1. `MEME["atr_stop"]` and `MEME["vol_ceiling"]` read `facts["atr_pct"]`, which
   nothing in the live loop ever set. Switching either one on was a silent
   no-op: the ceiling never blocked and the stop never moved.
2. The pullback gate took the 20-bar high from whatever `series()` returned by
   default, which is the 15-second scalp tab. Twenty 15s bars is a 5-minute
   window, but the rule was measured on 5-minute bars -- a 100-minute window.
   The live desk was therefore running a different rule than the one that
   scored 68.9% on 11 pools.

These tests pin the fixes: a real 5-minute timeframe, an ATR computed from the
same bars, and both switches actually moving the signal when they are on.
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolate_env  # noqa: E402,F401  # drop live CoinGecko key (unittest does not load conftest)
from desk import chart, meme_tactics  # noqa: E402


def _bars(n=40, base=1.0, step=0.01):
    """n five-minute candles with a known, widening range."""
    t0 = int(time.time()) // 300 * 300
    return [
        {"t": t0 - 300 * (n - i),
         "o": base + step * i, "h": base + step * i + 0.05,
         "l": base + step * i - 0.05, "c": base + step * i, "v": 10.0}
        for i in range(n)
    ]


def _body(candles):
    rows = [[c["t"], c["o"], c["h"], c["l"], c["c"], c["v"]] for c in candles]
    return {"data": {"attributes": {"ohlcv_list": rows}}}


class FiveMinuteTimeframe(unittest.TestCase):
    def setUp(self):
        chart.reset()
        chart._fetch_ohlcv = None

    def tearDown(self):
        chart.reset()
        chart._fetch_ohlcv = None

    def test_5m_is_a_real_network_timeframe(self):
        self.assertIn("5m", chart._TF)
        bucket, spec = chart._TF["5m"]
        self.assertEqual(bucket, 300)                    # 5 minutes, not 15 seconds
        self.assertEqual(spec, ("minute", 5))
        self.assertIn("5m", chart.REFRESH_S)
        self.assertIn("5m", chart.LAST_GOOD_S)

    def test_series_honours_the_5m_request(self):
        chart._fetch_ohlcv = lambda mint, tf: _body(_bars(30))
        cs = chart.series("MINT5M", "5m")
        self.assertEqual(cs["tf"], "5m")
        self.assertEqual(cs["source"], "geckoterminal")
        self.assertEqual(len(cs["candles"]), 30)

    def test_20_bars_of_5m_is_a_100_minute_window(self):
        """The whole point: 20 bars must span ~100 minutes, not 5."""
        chart._fetch_ohlcv = lambda mint, tf: _body(_bars(30))
        cs = chart.series("MINT5M", "5m")
        last20 = cs["candles"][-20:]
        span_min = (last20[-1]["t"] - last20[0]["t"]) / 60.0
        self.assertEqual(span_min, 19 * 5)               # 95 min between bar opens

    def test_5m_is_not_a_dashboard_tab(self):
        """It exists for the gate, not for the chart; tabs must not grow."""
        self.assertNotIn("5m", chart.tabs_for(horizon="scalp"))
        self.assertNotIn("5m", chart.tabs_for(horizon="swing"))


class AtrPctFromCandles(unittest.TestCase):
    def test_matches_the_offline_indicator_library(self):
        """Same number the eval measured: indicators.atr / close."""
        from desk import indicators
        candles = _bars(40)
        longform = [{"high": c["h"], "low": c["l"], "close": c["c"]} for c in candles]
        want = indicators.atr(longform) / candles[-1]["c"]
        got = meme_tactics.atr_pct_from(candles)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, want, places=9)

    def test_accepts_long_form_candles_too(self):
        """chart.py uses h/l/c, indicators use high/low/close; both must work."""
        candles = _bars(40)
        longform = [{"high": c["h"], "low": c["l"], "close": c["c"]} for c in candles]
        self.assertAlmostEqual(meme_tactics.atr_pct_from(candles),
                               meme_tactics.atr_pct_from(longform), places=12)

    def test_flat_candles_have_zero_atr(self):
        flat = [{"h": 1.0, "l": 1.0, "c": 1.0} for _ in range(20)]
        self.assertEqual(meme_tactics.atr_pct_from(flat), 0.0)

    def test_too_few_bars_is_none_not_a_guess(self):
        self.assertIsNone(meme_tactics.atr_pct_from(_bars(5)))
        self.assertIsNone(meme_tactics.atr_pct_from([]))
        self.assertIsNone(meme_tactics.atr_pct_from(None))

    def test_zero_price_bars_are_skipped_not_averaged_in(self):
        """A 0.0 bar is junk from a failed tick; it must not poison the ATR."""
        good = _bars(20)
        bad = good + [{"h": 0.0, "l": 0.0, "c": 0.0}]
        self.assertAlmostEqual(meme_tactics.atr_pct_from(bad),
                               meme_tactics.atr_pct_from(good), places=12)


class SwitchesActuallyWork(unittest.TestCase):
    """With the fact present, both switches must change the signal."""

    def setUp(self):
        self._saved = dict(meme_tactics.MEME)

    def tearDown(self):
        meme_tactics.MEME.clear()
        meme_tactics.MEME.update(self._saved)

    def _sig(self):
        return {
            "action": "BUY",
            "take_profit": {"stop_pct": 0.30, "tp1_at": 1.25, "tp1_frac": 0.5,
                            "tp2_at": 1.6, "tp2_frac": 0.5, "trail_pct": 0.15},
            "plan": {}, "reasons": [], "warnings": [],
        }

    def _off(self):
        for k in ("pullback_entry", "pullback_shadow", "flash_pump_guard",
                  "atr_stop", "vol_ceiling"):
            meme_tactics.MEME[k] = False

    def test_atr_stop_widens_the_stop_when_fed(self):
        self._off()
        meme_tactics.MEME["atr_stop"] = True
        meme_tactics.MEME["atr_stop_k"] = 1.5
        out = meme_tactics.apply(self._sig(), {"price_usd": 1.0},
                                 {"atr_pct": 0.25})
        self.assertAlmostEqual(out["take_profit"]["stop_pct"], 0.375)

    def test_atr_stop_still_inert_without_the_fact(self):
        """Neutral on missing data is correct; it just must not be the norm."""
        self._off()
        meme_tactics.MEME["atr_stop"] = True
        out = meme_tactics.apply(self._sig(), {"price_usd": 1.0}, {})
        self.assertEqual(out["take_profit"]["stop_pct"], 0.30)

    def test_vol_ceiling_blocks_a_wild_coin_when_fed(self):
        self._off()
        meme_tactics.MEME["vol_ceiling"] = True
        meme_tactics.MEME["vol_ceiling_pct"] = 0.20
        out = meme_tactics.apply(self._sig(), {"price_usd": 1.0},
                                 {"atr_pct": 0.35})
        self.assertEqual(out["action"], "HOLD")
        self.assertIn("too_volatile", out["tactic_block"])

    def test_vol_ceiling_lets_a_calm_coin_through(self):
        self._off()
        meme_tactics.MEME["vol_ceiling"] = True
        out = meme_tactics.apply(self._sig(), {"price_usd": 1.0},
                                 {"atr_pct": 0.05})
        self.assertEqual(out["action"], "BUY")


class AgentFeedsTheFacts(unittest.TestCase):
    """A source guard: the cycle must ask for 5m and set atr_pct.

    Running a full agent cycle needs network seats, so this pins the wiring at
    the source level. It is narrow on purpose -- it fails if someone reverts to
    `series(mint, session=...)` with no timeframe, which is the exact bug.
    """

    def setUp(self):
        self.src = (Path(__file__).resolve().parents[1]
                    / "desk" / "solana_agent.py").read_text(encoding="utf-8")

    def test_cycle_requests_5m_candles(self):
        self.assertIn('candle_chart.series(t["mint"], "5m"', self.src)

    def test_cycle_sets_atr_pct(self):
        self.assertIn('f["atr_pct"] = a', self.src)

    def test_cycle_keeps_the_pullback_high_from_those_bars(self):
        self.assertIn('f["high_20"] = max(highs)', self.src)


if __name__ == "__main__":
    unittest.main()
