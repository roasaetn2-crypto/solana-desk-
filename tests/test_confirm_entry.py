"""Confirmed scalp entry checklist + momentum-break exit + realistic stop fill (2026-11-19)."""
import time
import unittest

import config
from desk import confirm, exits

NOW = 1_800_000_000.0


def bars(closes, tf=60, end=NOW, opens=None, lows=None):
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        o = (opens[i] if opens else (closes[i - 1] if i else c))
        lo = lows[i] if lows else min(o, c)
        out.append({"t": end - (n - i) * tf, "o": o, "h": max(o, c), "l": lo, "c": c, "v": 1})
    return out


UP = [1.0 + 0.01 * i for i in range(25)]
DOWN = [2.0 - 0.02 * i for i in range(25)]
GOOD_T = {"symbol": "OK", "volume_h1": 20_000, "liquidity_usd": 20_000, "age_minutes": 90}
GOOD_DS = {"buys_m5": 30, "sells_m5": 20, "change_m5": 6}


class Checklist(unittest.TestCase):
    def test_clean_uptrend_passes_all_five(self):
        r = confirm.checklist(GOOD_T, GOOD_DS, bars(UP), bars(UP, 300), NOW)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["passed"], 5)

    def test_1m_downtrend_fails_trend(self):
        r = confirm.checklist(GOOD_T, GOOD_DS, bars(DOWN), bars(UP, 300), NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "confirm_trend")

    def test_5m_disagreeing_fails_trend(self):
        r = confirm.checklist(GOOD_T, GOOD_DS, bars(UP), bars(DOWN, 300), NOW)
        self.assertEqual(r["fail"], "trend")

    def test_sellers_in_control_fails_buy(self):
        r = confirm.checklist(GOOD_T, {"buys_m5": 10, "sells_m5": 20, "change_m5": 3},
                              bars(UP), bars(UP, 300), NOW)
        self.assertEqual(r["fail"], "buy")

    def test_dead_pool_fails_turnover(self):
        t = dict(GOOD_T, volume_h1=1_000)
        r = confirm.checklist(t, GOOD_DS, bars(UP), bars(UP, 300), NOW)
        self.assertEqual(r["fail"], "turnover")

    def test_young_coin_turnover_is_pro_rata(self):
        t = dict(GOOD_T, volume_h1=1_000, liquidity_usd=20_000, age_minutes=10)   # 0.05 vs 0.025
        self.assertTrue(confirm.turnover_state(t)["ok"])

    def test_falling_knife(self):
        c = UP[:20] + [1.20, 1.18, 1.16, 1.14]
        self.assertFalse(confirm.knife_state(bars(c), NOW)["ok"])
        crash = bars(UP[:-1] + [UP[-2] * 0.85])
        self.assertFalse(confirm.knife_state(crash, NOW)["ok"])

    def test_vertical_is_overextended(self):
        r = confirm.extended_state({}, {"change_m5": 55}, [], NOW)
        self.assertFalse(r["ok"])
        wick = bars(UP[:-1] + [UP[-2] * 1.25])
        self.assertFalse(confirm.extended_state({}, {"change_m5": 5}, wick, NOW)["ok"])

    def test_missing_data_is_na_not_a_block(self):
        r = confirm.checklist({"symbol": "NEW"}, {}, [], [], NOW)
        self.assertTrue(r["ok"])
        self.assertEqual(r["known"], 0)
        self.assertTrue(all(c["ok"] is None for c in r["checks"].values()))

    def test_stale_candles_are_na(self):
        old = bars(DOWN, end=NOW - 3600)
        self.assertIsNone(confirm.trend_state(old, NOW, 60)["ok"])

    def test_check_can_be_switched_off(self):
        saved = dict(config.CONFIRM["checks"])
        try:
            config.CONFIRM["checks"]["buy"] = False
            r = confirm.checklist(GOOD_T, {"buys_m5": 1, "sells_m5": 20}, bars(UP), bars(UP, 300), NOW)
            self.assertTrue(r["ok"])
            self.assertEqual(r["checks"]["buy"]["why"], "off")
        finally:
            config.CONFIRM["checks"].clear()
            config.CONFIRM["checks"].update(saved)

    def test_entry_candle_low(self):
        b = bars(UP, lows=[x * 0.99 for x in UP])
        self.assertAlmostEqual(confirm.entry_candle_low(b, [], NOW), UP[-1] * 0.99)
        self.assertIsNone(confirm.entry_candle_low([], [], NOW))


class MomentumExit(unittest.TestCase):
    def pos(self, **kw):
        p = {"entry": 1.24, "opened": NOW - 300, "stage": 0, "entry_candle_low": 1.22}
        p.update(kw)
        return p

    def test_close_under_entry_candle_low_exits(self):
        c = bars(UP[:-1] + [1.21], end=NOW - 1)     # last bar closed (ends at NOW-1)
        self.assertIn("entry candle low", confirm.momentum_exit(self.pos(), c, NOW) or "")

    def test_close_under_9ema_while_red_exits(self):
        c = bars(UP[:-3] + [1.22, 1.22, 1.18], end=NOW - 1)
        why = confirm.momentum_exit(self.pos(entry_candle_low=1.0), c, NOW)
        self.assertIn("9 EMA", why or "")

    def test_healthy_trade_holds(self):
        c = bars(UP, end=NOW - 1)
        self.assertIsNone(confirm.momentum_exit(self.pos(entry=1.2, entry_candle_low=1.19), c, NOW))

    def test_not_before_min_hold_and_not_after_tp1(self):
        c = bars(UP[:-1] + [1.0], end=NOW - 1)
        self.assertIsNone(confirm.momentum_exit(self.pos(opened=NOW - 30), c, NOW))
        self.assertIsNone(confirm.momentum_exit(self.pos(stage=1), c, NOW))

    def test_no_candles_no_forced_exit(self):
        self.assertIsNone(confirm.momentum_exit(self.pos(), [], NOW))

    def test_forming_bar_is_ignored(self):
        c = bars(UP[:-1] + [1.0], end=NOW + 30)     # last bar still forming
        why = confirm.momentum_exit(self.pos(entry_candle_low=None, entry=1.0), c, NOW)
        self.assertIsNone(why)


class StopFill(unittest.TestCase):
    def setUp(self):
        self.saved = {k: config.EXITS.get(k) for k in ("stop_fill_mode", "stop_gap_cap_pct")}

    def tearDown(self):
        config.EXITS.update(self.saved)

    def run_stop(self, price):
        pos = {"entry": 1.0, "opened": time.time(), "last": 1.0, "peak": 1.0}
        exits.init_state(pos, "scalp")
        sig = exits.evaluate(pos, {"price_usd": price, "volume_h24": 1, "volume_h6": 1}, time.time())
        self.assertTrue(sig and sig[1].startswith("stop"), sig)
        return pos

    def test_realistic_small_gap_fills_at_the_print(self):
        config.EXITS.update({"stop_fill_mode": "realistic", "stop_gap_cap_pct": 0.05})
        probe = {"entry": 1.0}
        exits.init_state(probe, "scalp")
        stop = probe["stop_price"]
        pos = self.run_stop(stop * 0.98)
        self.assertAlmostEqual(pos["fill_at"], stop * 0.98)

    def test_realistic_rug_is_capped(self):
        config.EXITS.update({"stop_fill_mode": "realistic", "stop_gap_cap_pct": 0.05})
        pos = self.run_stop(0.10)
        self.assertAlmostEqual(pos["fill_at"], pos["stop_price"] * 0.95)

    def test_stop_mode_keeps_old_behaviour(self):
        config.EXITS["stop_fill_mode"] = "stop"
        pos = self.run_stop(0.10)
        self.assertEqual(pos["fill_at"], pos["stop_price"])

    def test_market_mode_has_no_fill_override(self):
        config.EXITS["stop_fill_mode"] = "market"
        self.assertNotIn("fill_at", self.run_stop(0.10))


if __name__ == "__main__":
    unittest.main()


class AgentWiring(unittest.TestCase):
    def agent(self):
        import tempfile
        from pathlib import Path
        from desk.solana_agent import SolanaAgent
        d = Path(tempfile.mkdtemp())
        return SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")

    def test_skip_is_logged_counted_and_shown(self):
        from unittest import mock
        a = self.agent()
        now = time.time()

        def series(mint, tf, session=None):
            return {"candles": bars(DOWN, 60 if tf == "1m" else 300, end=now)}
        with mock.patch("desk.solana_agent.candle_chart.series", side_effect=series):
            f = {}
            t = dict(GOOD_T, mint="MINTDOWN")
            self.assertTrue(a._confirm_skip(t, GOOD_DS, f))
        self.assertEqual(a.confirm_stats["checked"], 1)
        self.assertEqual(a.confirm_stats["confirm_trend"], 1)
        self.assertEqual(a.funnel["confirm_trend"], 1)
        self.assertEqual(a.confirm_recent[0]["checks"]["trend"][0], "FAIL")

    def test_pass_records_entry_candle_low(self):
        from unittest import mock
        a = self.agent()
        now = time.time()

        def series(mint, tf, session=None):
            return {"candles": bars(UP, 60 if tf == "1m" else 300, end=now)}
        with mock.patch("desk.solana_agent.candle_chart.series", side_effect=series):
            f = {}
            self.assertFalse(a._confirm_skip(dict(GOOD_T, mint="MINTUP"), GOOD_DS, f))
        self.assertTrue(f["entry_candle_low"] > 0)
        self.assertEqual(a.confirm_stats["passed"], 1)

    def test_health_shows_confirm(self):
        import app as app_mod
        from fastapi.testclient import TestClient
        d = TestClient(app_mod.app).get("/api/health").json()
        self.assertIn("confirm", d)
        self.assertIn("checked", d["confirm"])
