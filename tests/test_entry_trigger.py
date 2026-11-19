"""Entry-trigger gate (desk/entry_trigger.py), PAPER only.

RSI math on known series, the oversold trigger (with and without the up-turn), the breakout
trigger (resistance + volume multiple), 15s -> 1m fallback, unknown / no_trigger skips, default ON
in paper, live untouched, proposal-then-Confirm settings, and the tag on the buy event, the
journal row, the position and the analyst facts.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DESK_OFFLINE", "1")

import config  # noqa: E402
from config import RULEBOOK  # noqa: E402
from desk import analyst, chart, dip_recovery as dr, entry_trigger as et, indicators, scanner, settings  # noqa: E402
from desk.solana_agent import SolanaAgent  # noqa: E402
from legacy_book import legacy_book  # noqa: E402
from test_dip_recovery import CLEAN, DEV, DS, JUP, TOK, candles, flat  # noqa: E402

NOW = 1_800_000_000.0 + 20
MINT = "TRIGMINT" + "x" * 30
KEYS = ("rulebook.entry_trigger_gate", "rulebook.rsi_period", "rulebook.rsi_oversold",
        "rulebook.rsi_require_turn", "rulebook.rsi_timeframe", "rulebook.breakout_lookback",
        "rulebook.breakout_vol_mult")


def falling(n=24, start=1.0, step=0.012, v=100):
    px, out = start, []
    for _ in range(n):
        out.append((px, px * (1 - step), v))
        px *= 1 - step
    return out


def oversold_up_spec():
    spec = falling()
    last = spec[-1][1]
    return spec + [(last, last * 1.004, 100)]          # still deeply oversold, last bar closes up


def oversold_down_spec():
    return falling(25)                                  # oversold and the last bar is still falling


def rising_spec(n=25):
    px, out = 1.0, []
    for _ in range(n):
        out.append((px, px * 1.01, 100))
        px *= 1.01
    return out


def breakout_spec(vol=250, close=1.05):
    return flat(20, 1.0, 100) + [(1.0, close, vol)]


def wilder(closes, period=14):
    """Independent textbook Wilder RSI for the last close."""
    ch = [b - a for a, b in zip(closes, closes[1:])]
    ag = sum(max(c, 0) for c in ch[:period]) / period
    al = sum(max(-c, 0) for c in ch[:period]) / period
    for c in ch[period:]:
        ag = (ag * (period - 1) + max(c, 0)) / period
        al = (al * (period - 1) + max(-c, 0)) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def feed(spec, now=NOW):
    cs = candles(spec, now)
    return mock.patch.object(dr, "load_candles", return_value=(cs, now if cs else None))


def tick_prices(prices, now=NOW, mint=MINT):
    """One price tick per 15s bucket, newest at `now`."""
    n = len(prices)
    start = (int(now) // 15) * 15 - (n - 1) * 15
    for i, px in enumerate(prices):
        chart.record_tick(mint, px, 0.0, ts=start + 15 * i + 1)


class Base(unittest.TestCase):
    def setUp(self):
        chart.reset()
        self.addCleanup(chart.reset)
        p = mock.patch.dict(RULEBOOK, {"entry_trigger_gate": True, "rsi_require_turn": True,
                                       "rsi_timeframe": "15s"})
        p.start()
        self.addCleanup(p.stop)


class RsiMath(unittest.TestCase):
    CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
              45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]

    def test_known_series(self):
        got = indicators.rsi(self.CLOSES, 14)
        self.assertAlmostEqual(got, wilder(self.CLOSES), places=6)
        self.assertAlmostEqual(indicators.rsi(self.CLOSES[:15], 14), 70.46, delta=0.1)   # textbook value

    def test_extremes_and_short_input(self):
        self.assertEqual(indicators.rsi([1.0 - 0.01 * i for i in range(20)], 14), 0.0)
        self.assertEqual(indicators.rsi([1.0 + 0.01 * i for i in range(20)], 14), 100.0)
        self.assertIsNone(indicators.rsi([1.0] * 10, 14))


class Oversold(Base):
    def run_1m(self, spec, **cfg):
        with mock.patch.dict(RULEBOOK, {"rsi_timeframe": "1m", **cfg}), feed(spec):
            return et.evaluate(MINT, now=NOW)

    def test_oversold_and_turning_up_fires(self):
        r = self.run_1m(oversold_up_spec())
        self.assertEqual(r["fired"], et.RSI_OVERSOLD)
        self.assertLess(r["rsi_value"], 30)
        self.assertEqual((r["rsi_tf"], r["rsi_period"]), ("1m", 14))
        self.assertIsNone(et.gate(r))

    def test_still_falling_needs_the_turn(self):
        r = self.run_1m(oversold_down_spec())
        self.assertIsNone(r["fired"])
        self.assertEqual((r["reason"], r["rsi"]["detail"]), (et.SKIP_NONE, "no_turn_yet"))
        self.assertEqual(et.gate(r), "no_trigger")
        self.assertEqual(self.run_1m(oversold_down_spec(), rsi_require_turn=False)["fired"], et.RSI_OVERSOLD)

    def test_not_oversold_is_no_trigger(self):
        r = self.run_1m(rising_spec())
        self.assertEqual((r["fired"], r["reason"]), (None, et.SKIP_NONE))
        self.assertEqual(r["rsi"]["detail"], "not_oversold")

    def test_threshold_and_period_are_settings(self):
        spec = oversold_up_spec()
        rsi = self.run_1m(spec)["rsi_value"]
        self.assertIsNone(self.run_1m(spec, rsi_oversold=rsi - 0.5)["fired"])
        self.assertEqual(self.run_1m(spec, rsi_period=7)["rsi_period"], 7)

    def test_15s_ticks_are_the_default_timeframe(self):
        px = [b[1] for b in oversold_up_spec()]
        tick_prices(px)
        with feed([]):
            r = et.evaluate(MINT, now=NOW)
        self.assertEqual((r["fired"], r["rsi_tf"]), (et.RSI_OVERSOLD, "15s"))
        self.assertFalse(r["rsi"]["fell_back"])

    def test_thin_15s_falls_back_to_1m(self):
        tick_prices([1.0, 0.9, 0.8])                          # 3 ticks: far too thin
        with feed(oversold_up_spec()):
            r = et.evaluate(MINT, now=NOW)
        self.assertEqual((r["fired"], r["rsi_tf"]), (et.RSI_OVERSOLD, "1m"))
        self.assertTrue(r["rsi"]["fell_back"])

    def test_stale_15s_ticks_do_not_count(self):
        tick_prices([b[1] for b in oversold_up_spec()], now=NOW - 600)
        with feed([]):
            r = et.evaluate(MINT, now=NOW)
        self.assertIsNone(r["rsi_value"])

    def test_insufficient_everywhere_is_trigger_unknown(self):
        tick_prices([1.0, 0.9])
        with feed(flat(4)):                                    # 4 real 1m candles: too few
            r = et.evaluate(MINT, now=NOW)
        self.assertEqual((r["fired"], r["reason"]), (None, et.SKIP_UNKNOWN))
        self.assertEqual(et.gate(r), "trigger_unknown")
        with feed([]):
            self.assertEqual(et.evaluate(MINT, now=NOW)["reason"], et.SKIP_UNKNOWN)

    def test_stale_1m_candles_are_unknown(self):
        cs = candles(oversold_up_spec(), NOW - 1000)
        with mock.patch.object(dr, "load_candles", return_value=(cs, NOW - 1000)), \
                mock.patch.dict(RULEBOOK, {"rsi_timeframe": "1m"}):
            self.assertEqual(et.evaluate(MINT, now=NOW)["reason"], et.SKIP_UNKNOWN)


class Breakout(Base):
    def go(self, spec, **cfg):
        with mock.patch.dict(RULEBOOK, cfg), feed(spec):
            return et.evaluate(MINT, now=NOW)

    def test_break_above_resistance_on_2x_volume_fires(self):
        r = self.go(breakout_spec(250))
        self.assertEqual(r["fired"], et.BREAKOUT)
        self.assertGreaterEqual(r["vol_mult"], 2.0)
        self.assertTrue(r["breakout"]["above"])
        self.assertIsNone(et.gate(r))

    def test_weak_volume_or_no_break_does_not_fire(self):
        weak = self.go(breakout_spec(150))
        self.assertEqual((weak["fired"], weak["reason"], weak["breakout"]["detail"]),
                         (None, et.SKIP_NONE, "weak_volume"))
        under = self.go(breakout_spec(400, close=0.99))
        self.assertEqual((under["fired"], under["breakout"]["detail"]), (None, "below_resistance"))

    def test_multiple_and_lookback_are_settings(self):
        self.assertEqual(self.go(breakout_spec(250), breakout_vol_mult=3.0)["breakout"]["state"], et.NO)
        self.assertEqual(self.go(breakout_spec(150), breakout_vol_mult=1.2)["fired"], et.BREAKOUT)
        # a spike older than the lookback no longer sets resistance
        spec = [(1.0, 1.3, 100), (1.3, 1.0, 100)] + flat(18, 1.0, 100) + [(1.0, 1.05, 250)]
        self.assertEqual(self.go(spec, breakout_lookback=20)["breakout"]["state"], et.NO)
        self.assertEqual(self.go(spec, breakout_lookback=10)["breakout"]["state"], et.FIRE)

    def test_too_few_candles_or_no_volume_baseline_is_unknown(self):
        self.assertEqual(self.go(flat(5) + [(1.0, 1.1, 300)])["breakout"]["state"], et.UNKNOWN)
        silent = self.go(flat(20, 1.0, 0) + [(1.0, 1.05, 250)])
        self.assertEqual(silent["breakout"]["detail"], "no_volume_baseline")

    def test_15s_ticks_carry_no_volume_so_breakout_is_1m_only(self):
        tick_prices([1.0] * 20 + [1.2])
        with feed([]):
            r = et.evaluate(MINT, now=NOW)
        self.assertEqual(r["breakout"]["state"], et.UNKNOWN)

    def test_cache_only_read_never_calls_the_network(self):
        with mock.patch.object(chart, "_pool_address", return_value="POOLX"), \
                mock.patch.object(chart, "_fetch_and_store") as fetch:
            b = et.read_breakout(MINT, NOW, None, fetch=False)
            fetch.assert_not_called()
            self.assertEqual(b["state"], et.UNKNOWN)
            chart._OHLCV[chart._ohlcv_key("POOLX", "1m")] = {"at": NOW - 5, "candles": candles(breakout_spec(), NOW)}
            self.addCleanup(chart._OHLCV.clear)
            self.assertEqual(et.read_breakout(MINT, NOW, None, fetch=False)["state"], et.FIRE)
            fetch.assert_not_called()

    def test_evaluate_never_raises(self):
        with mock.patch.object(dr, "load_candles", side_effect=RuntimeError("boom")):
            self.assertEqual(et.evaluate(MINT, now=NOW)["reason"], et.SKIP_UNKNOWN)


class GateAndFacts(Base):
    def test_off_never_blocks(self):
        with mock.patch.dict(RULEBOOK, {"entry_trigger_gate": False}):
            self.assertIsNone(et.gate({"fired": None, "reason": et.SKIP_NONE}))
            self.assertIsNone(et.gate(None))

    def test_unknown_info_blocks_when_on(self):
        self.assertEqual(et.gate(None), "trigger_unknown")

    def test_analyst_facts_carry_rsi_and_volume(self):
        info = {"fired": et.BREAKOUT, "rsi_value": 41.2, "rsi_period": 14, "rsi_tf": "15s", "vol_mult": 2.6,
                "breakout": {"above": True}}
        out = analyst.token_state(TOK, {}, {}, {"scorecard": {}, "dev": {}, "ds": {},
                                                "entry_trigger": et.facts(info)})
        self.assertIn('"entry_trigger"', out)
        self.assertIn('"rsi14": 41.2', out)
        self.assertIn('"volume_vs_avg": 2.6', out)
        self.assertNotIn("entry_trigger", analyst.token_state(TOK, {}, {}, {"scorecard": {}, "dev": {}, "ds": {}}))


class Settings(unittest.TestCase):
    def test_defaults_are_on_in_config_and_registry(self):
        src = Path(config.__file__).read_text()
        self.assertIn('"entry_trigger_gate": True', src)
        saved = {k: v for k, v in RULEBOOK.items()}                 # the fixture only turns bools off
        self.assertEqual(saved.get("rsi_period"), 14)
        self.assertEqual(saved.get("rsi_oversold"), 30)
        self.assertEqual(saved.get("rsi_timeframe"), "15s")
        self.assertEqual(saved.get("breakout_lookback"), 20)
        self.assertEqual(saved.get("breakout_vol_mult"), 2.0)
        for k in KEYS:
            self.assertIn(k, settings.REGISTRY)
            self.assertEqual(settings.REGISTRY[k].group, "Rulebook")
            self.assertFalse(settings.REGISTRY[k].owner_only)
        R = settings.REGISTRY
        self.assertEqual((R["rulebook.rsi_period"].lo, R["rulebook.rsi_period"].hi), (5, 30))
        self.assertEqual((R["rulebook.rsi_oversold"].lo, R["rulebook.rsi_oversold"].hi), (10, 50))
        self.assertEqual((R["rulebook.breakout_lookback"].lo, R["rulebook.breakout_lookback"].hi), (5, 60))
        self.assertEqual((R["rulebook.breakout_vol_mult"].lo, R["rulebook.breakout_vol_mult"].hi), (1.2, 10.0))
        self.assertEqual(R["rulebook.rsi_timeframe"].choices, ("15s", "1m"))

    def test_proposal_then_confirm_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DESK_DATA_DIR": tmp}), \
                mock.patch.dict(RULEBOOK, {"entry_trigger_gate": True, "rsi_period": 14, "rsi_oversold": 30,
                                           "rsi_timeframe": "15s", "breakout_vol_mult": 2.0,
                                           "breakout_lookback": 20, "rsi_require_turn": True}):
            settings.reset_runtime()
            out = settings.create([{"setting": "rulebook.entry_trigger_gate", "proposed": False},
                                   {"setting": "rulebook.rsi_oversold", "proposed": 25},
                                   {"setting": "rulebook.rsi_period", "proposed": 99},
                                   {"setting": "rulebook.rsi_timeframe", "proposed": "1M"},
                                   {"setting": "rulebook.rsi_require_turn", "proposed": "off"},
                                   {"setting": "rulebook.breakout_vol_mult", "proposed": "3x"},
                                   {"setting": "rulebook.breakout_lookback", "proposed": 30}],
                                  "t", "desk_ai")
            self.assertTrue(out["ok"], out)
            self.assertTrue(RULEBOOK["entry_trigger_gate"])             # a proposal changes nothing
            self.assertEqual(RULEBOOK["rsi_oversold"], 30)
            self.assertEqual(RULEBOOK["rsi_timeframe"], "15s")
            pid = settings.listing()["pending"][0]["id"]
            settings.confirm(pid, "romz")
            self.assertFalse(RULEBOOK["entry_trigger_gate"])
            self.assertEqual(RULEBOOK["rsi_oversold"], 25)
            self.assertEqual(RULEBOOK["rsi_period"], 30)                # clamped to the bound
            self.assertEqual(RULEBOOK["rsi_timeframe"], "1m")
            self.assertFalse(RULEBOOK["rsi_require_turn"])
            self.assertEqual(RULEBOOK["breakout_lookback"], 30)
            settings.reset_runtime()

    def test_bad_timeframe_is_not_accepted(self):
        with self.assertRaises(ValueError):
            settings.REGISTRY["rulebook.rsi_timeframe"].coerce("5m")

    def test_bench_times_are_short(self):
        self.assertEqual(config.BENCH_MINUTES["no_trigger"], 2)
        self.assertEqual(config.BENCH_MINUTES["trigger_unknown"], 2)


@legacy_book
class AgentWiring(unittest.TestCase):
    def run_agent(self, spec, gate=True, mode=None, ticks=None, extra=None, ds=None, cache=False):
        chart.reset()
        self.addCleanup(chart.reset)
        d = Path(tempfile.mkdtemp())
        a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        if mode:
            a.mode = mode
        now = time.time()
        if ticks:
            tick_prices(ticks, now=now, mint=TOK["mint"])
        facts = {"mint_authority": False, "freeze_authority": False, "top_wallet_pct": 0.03,
                 "top_10_pct": 0.2, "holders": 400, "supply": 1e9, "decimals": 6}
        quote = {"price_usd": 0.00105, "liquidity_usd": 39_000, "volume_h6": 1, "volume_h24": 1}
        cs = [] if spec is None else candles(spec, now)
        load = mock.Mock(return_value=(cs, now if cs else None))
        self.journal = mock.Mock(return_value={})
        patches = [mock.patch.object(scanner, "new_tokens", return_value=[dict(TOK)]),
                   mock.patch.object(scanner, "socials", return_value={"x": "https://x.com/a", "telegram": None, "website": None}),
                   mock.patch.object(scanner, "quote", return_value=quote),
                   mock.patch("desk.chain.facts", return_value=facts),
                   mock.patch("desk.dexscreener.facts", return_value=dict(ds or DS)),
                   mock.patch("desk.devcheck.investigate", return_value=dict(DEV)),
                   mock.patch("desk.solana_agent.alert"),
                   mock.patch("desk.solana_agent.journal", self.journal),
                   mock.patch("desk.solana_agent.signals.security", return_value=CLEAN),
                   mock.patch("desk.solana_agent.signals.entry_impact", return_value=JUP),
                   mock.patch.object(dr, "load_candles", load),
                   mock.patch.dict(RULEBOOK, {"entry_trigger_gate": gate, "rsi_timeframe": "1m",
                                              **(extra or {})})]
        if cache:                          # the cache-only read the no_chase counter uses
            chart._OHLCV[chart._ohlcv_key("POOLNC", "1m")] = {"at": now, "candles": cs}
            patches += [mock.patch.object(chart, "_pool_address", return_value="POOLNC"),
                        mock.patch("desk.solana_agent.candle_chart.series", return_value={"candles": []})]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        a.run_once()
        return a, load

    def skips(self, a):
        return [e["reason"] for e in a.events if e.get("stage") == "rulebook"]

    def entry_rows(self):
        return [c.kwargs for c in self.journal.call_args_list if c.args and c.args[0] == "entry"]

    def test_oversold_entry_is_tagged_everywhere(self):
        a, _ = self.run_agent(oversold_up_spec())
        self.assertEqual(self.skips(a), [], [e["message"] for e in list(a.events)[:10]])
        self.assertEqual((a.stats["entries"], a.stats["trigger_pass"]), (1, 1))
        dec = a.decisions[0]
        self.assertEqual(dec["entry_trigger"]["trigger"], et.RSI_OVERSOLD)
        self.assertLess(dec["entry_trigger"]["rsi"], 30)
        self.assertEqual(dec["sizes"]["entry_trigger"]["trigger"], et.RSI_OVERSOLD)
        self.assertTrue(any(f["t"] == "trigger_rsi" for f in dec["flags"]))
        buy = [e for e in a.events if e["kind"] == "entry"][0]
        self.assertEqual(buy["trigger"], et.RSI_OVERSOLD)
        self.assertLess(buy["rsi"], 30)
        self.assertEqual(buy["rsi_tf"], "1m")
        row = self.entry_rows()[0]
        self.assertEqual((row["trigger"], row["rsi_tf"]), (et.RSI_OVERSOLD, "1m"))
        self.assertLess(row["rsi"], 30)
        pos = a.broker.s["positions"][TOK["mint"]]
        self.assertEqual(pos["entry_trigger"], et.RSI_OVERSOLD)
        self.assertLess(pos["entry_rsi"], 30)

    def test_breakout_entry_carries_the_volume_multiple(self):
        a, _ = self.run_agent(breakout_spec(250))
        self.assertEqual(a.stats["entries"], 1, [e["message"] for e in list(a.events)[:10]])
        buy = [e for e in a.events if e["kind"] == "entry"][0]
        self.assertEqual(buy["trigger"], et.BREAKOUT)
        self.assertGreaterEqual(buy["vol_mult"], 2.0)
        self.assertGreaterEqual(self.entry_rows()[0]["vol_mult"], 2.0)
        self.assertTrue(any(f["t"] == "trigger_breakout" for f in a.decisions[0]["flags"]))
        self.assertGreaterEqual(a.broker.s["positions"][TOK["mint"]]["entry_vol_mult"], 2.0)

    def test_grok_prompt_gets_rsi_and_volume(self):
        with mock.patch.object(analyst, "analyze_token", wraps=analyst.analyze_token) as spy:
            self.run_agent(breakout_spec(250))
        ctx = spy.call_args.kwargs["context"]["entry_trigger"]
        self.assertEqual(ctx["trigger"], et.BREAKOUT)
        self.assertIn("rsi14", ctx)
        self.assertGreaterEqual(ctx["volume_vs_avg"], 2.0)

    def test_nothing_fired_skips_no_trigger(self):
        a, _ = self.run_agent(rising_spec())
        self.assertEqual(self.skips(a), ["no_trigger"])
        self.assertEqual((a.stats["entries"], a.stats["trigger_none"]), (0, 1))
        self.assertTrue(any(e["message"] == "gate: $DIPC skip no_trigger" for e in a.events))
        self.assertEqual(self.entry_rows(), [])

    def test_no_data_skips_trigger_unknown(self):
        a, _ = self.run_agent(None)
        self.assertEqual(self.skips(a), ["trigger_unknown"])
        self.assertEqual((a.stats["entries"], a.stats["trigger_unknown"]), (0, 1))

    def test_gate_off_does_not_block(self):
        a, _ = self.run_agent(rising_spec(), gate=False)
        self.assertEqual(self.skips(a), [])
        self.assertEqual(a.stats["entries"], 1)

    def test_live_mode_is_untouched(self):
        with mock.patch.object(et, "evaluate") as ev:
            a, load = self.run_agent(None, mode="live")
        ev.assert_not_called()
        load.assert_not_called()
        self.assertNotIn("no_trigger", self.skips(a))
        self.assertNotIn("trigger_unknown", self.skips(a))
        for row in self.entry_rows():
            self.assertNotIn("trigger", row)

    def test_every_call_site_is_behind_a_paper_check(self):
        src = Path(sys.modules["desk.solana_agent"].__file__).read_text()
        i = src.index("entry_trigger.evaluate(")
        self.assertIn('self.mode != "live"', src[max(0, i - 200):i])

    def test_breakout_blocked_by_no_chase_is_counted(self):
        hot = dict(DS, change_m5=60)                                # +60% 5m: no_chase will skip
        a, _ = self.run_agent(breakout_spec(250), ds=hot, extra={"no_chase_gate": True}, cache=True)
        self.assertEqual(self.skips(a), ["no_chase"])
        self.assertEqual(a.stats["breakout_blocked_no_chase"], 1)
        self.assertEqual(a.stats["entries"], 0)
        self.assertTrue(any(e.get("reason") == "breakout_blocked_no_chase" for e in a.events))

    def test_no_chase_without_a_breakout_is_not_counted(self):
        hot = dict(DS, change_m5=60)
        a, _ = self.run_agent(rising_spec(), ds=hot, extra={"no_chase_gate": True}, cache=True)
        self.assertEqual(self.skips(a), ["no_chase"])
        self.assertEqual(a.stats["breakout_blocked_no_chase"], 0)


if __name__ == "__main__":
    unittest.main()
