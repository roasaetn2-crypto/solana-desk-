"""Entry triggers C (VWAP reclaim) and D (EMA cross) plus the per-trigger switches (PAPER only).

A coin passes when ANY enabled trigger fires. VWAP is volume weighted when GeckoTerminal gives
volume and a plain price average (labelled vwap_kind "price_avg") on the volume-less price tape.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DESK_OFFLINE", "1")

import config  # noqa: E402
from config import RULEBOOK  # noqa: E402
from desk import analyst, chart, dip_recovery as dr, entry_trigger as et, indicators, pricetape, settings  # noqa: E402
from test_dip_recovery import candles, flat  # noqa: E402
from test_entry_trigger import AgentWiring as _AW  # noqa: E402
from test_dip_recovery import TOK  # noqa: E402
from test_entry_trigger import MINT, NOW, breakout_spec, feed, oversold_up_spec, rising_spec, tick_prices  # noqa: E402
from test_second_source import tape_fill  # noqa: E402

ALL_OFF = {"rsi_trigger": False, "breakout_trigger": False, "vwap_trigger": False, "ema_trigger": False}


def vwap_reclaim_spec(last=(0.95, 1.02, 200)):
    """Calm at 1.0, a slide to 0.95 (below the session VWAP), then the reclaim bar."""
    return flat(20, 1.0, 100) + [(1.0, 0.97, 100), (0.97, 0.95, 100), last]


def ema_spec(n):
    """Flat, a slide to 0.90, then +3% a bar. Fast EMA(9) crosses above slow EMA(21) at index 39."""
    closes = [1.0] * 25 + [1.0 - 0.01 * i for i in range(1, 11)]
    px = closes[-1]
    for _ in range(12):
        px *= 1.03
        closes.append(px)
    closes = closes[:n]
    return [(closes[i - 1] if i else closes[0], c, 100) for i, c in enumerate(closes)]


def expected_vwap(spec, now=NOW):
    bars = candles(spec, now)
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars)
    return pv / sum(b["v"] for b in bars)


class Base(unittest.TestCase):
    def setUp(self):
        chart.reset()
        pricetape.reset()
        self.addCleanup(chart.reset)
        self.addCleanup(pricetape.reset)
        p = mock.patch.dict(RULEBOOK, {**ALL_OFF, "entry_trigger_gate": True, "rsi_require_turn": True,
                                       "rsi_timeframe": "1m", "ema_timeframe": "1m", "ema_fast": 9,
                                       "ema_slow": 21, "vwap_lookback_min": 60, "rsi_period": 14,
                                       "rsi_oversold": 30, "breakout_lookback": 20,
                                       "breakout_vol_mult": 2.0})
        p.start()
        self.addCleanup(p.stop)

    def on(self, **kw):
        q = mock.patch.dict(RULEBOOK, kw)
        q.start()
        self.addCleanup(q.stop)

    def run_gt(self, spec, **kw):
        with feed(spec):
            return et.evaluate(MINT, now=NOW, **kw)


class Vwap(Base):
    def setUp(self):
        super().setUp()
        self.on(vwap_trigger=True)

    def test_reclaim_with_volume_is_a_true_vwap(self):
        spec = vwap_reclaim_spec()
        r = self.run_gt(spec)
        self.assertEqual((r["fired"], r["fired_all"]), (et.VWAP_RECLAIM, [et.VWAP_RECLAIM]))
        self.assertEqual(r["vwap_kind"], "vwap")
        self.assertAlmostEqual(r["vwap_value"], expected_vwap(spec), places=9)
        self.assertEqual(r["vwap"]["source"], "geckoterminal")
        self.assertLess(r["vwap"]["prior_close"], r["vwap_value"])
        self.assertGreater(r["vwap"]["close"], r["vwap_value"])
        self.assertIsNone(et.gate(r))

    def test_each_failure_is_no_trigger_with_its_reason(self):
        cases = {"still_below": vwap_reclaim_spec((0.95, 0.96, 200)),            # up, but still under
                 "no_up_close": vwap_reclaim_spec((1.05, 1.02, 200)),           # above, but a red bar
                 "was_not_below": flat(20, 1.0, 100) + [(1.0, 1.01, 100), (1.01, 1.02, 100), (1.02, 1.03, 100)]}
        for why, spec in cases.items():
            with self.subTest(why):
                r = self.run_gt(spec)
                self.assertIsNone(r["fired"])
                self.assertEqual((r["reason"], r["vwap"]["detail"]), (et.SKIP_NONE, why))
                self.assertEqual(et.gate(r), "no_trigger")

    def test_lookback_setting_changes_the_window(self):
        spec = vwap_reclaim_spec()
        wide = self.run_gt(spec)
        self.on(vwap_lookback_min=10)
        short = self.run_gt(spec)
        self.assertEqual((wide["vwap"]["window_min"], short["vwap"]["window_min"]), (60, 10))
        self.assertNotAlmostEqual(wide["vwap_value"], short["vwap_value"], places=6)

    def test_window_is_capped_by_pool_age(self):
        spec = vwap_reclaim_spec()
        self.assertEqual(self.run_gt(spec, age_min=12)["vwap"]["window_min"], 12)
        young = self.run_gt(spec, age_min=3)                       # fewer than 6 bars of history
        self.assertEqual((young["reason"], young["cause"]), (et.SKIP_UNKNOWN, "too_few_bars"))

    def test_tape_without_volume_uses_a_labelled_price_average(self):
        spec = vwap_reclaim_spec()
        tape_fill(MINT, spec)
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch.object(chart, "_pool_address", return_value=None):
            r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertEqual(r["fired"], et.VWAP_RECLAIM)
        self.assertEqual((r["vwap_kind"], r["vwap"]["source"]), ("price_avg", "price_tape"))
        closes = [c for _o, c, _v in spec]
        self.assertAlmostEqual(r["vwap_value"], sum(closes) / len(closes), places=9)
        # the label travels everywhere the value does
        self.assertEqual(et.tag(r)["vwap_kind"], "price_avg")
        self.assertEqual(et.facts(r)["vwap_kind"], "price_avg")
        self.assertIn("price_avg", et.describe(r))

    def test_zero_volume_candles_fall_back_to_the_tape_average(self):
        spec = [(o, c, 0) for o, c, _v in vwap_reclaim_spec()]
        tape_fill(MINT, spec)
        with feed(spec), mock.patch.object(chart, "_pool_address", return_value=None):
            r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertEqual((r["fired"], r["vwap_kind"]), (et.VWAP_RECLAIM, "price_avg"))

    def test_no_volume_and_no_tape_is_unknown_with_a_cause(self):
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch.object(chart, "_pool_address", return_value=None):
            r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertEqual(r["reason"], et.SKIP_UNKNOWN)
        self.assertTrue(et.gate(r).startswith("trigger_unknown:"), et.gate(r))

    def test_true_vwap_is_tagged_vwap(self):
        r = self.run_gt(vwap_reclaim_spec())
        self.assertEqual(et.facts(r)["vwap_kind"], "vwap")
        self.assertEqual(et.tag(r)["vwap_kind"], "vwap")
        self.assertIn("vwap ", et.describe(r))


class Ema(Base):
    def setUp(self):
        super().setUp()
        self.on(ema_trigger=True)

    def test_matches_an_independent_ema(self):
        spec = ema_spec(40)
        closes = [c for _o, c, _v in spec]
        r = self.run_gt(spec)
        self.assertEqual((r["fired"], r["ema"]["detail"]), (et.EMA_CROSS, "fast_crossed_above_slow"))
        self.assertAlmostEqual(r["ema_fast_value"], indicators.ema_series(closes, 9)[-1], places=9)
        self.assertAlmostEqual(r["ema_slow_value"], indicators.ema_series(closes, 21)[-1], places=9)
        self.assertGreater(closes[-1], r["ema_fast_value"])
        self.assertGreater(r["ema_fast_value"], r["ema_slow_value"])
        self.assertEqual(r["ema_tf"], "1m")

    def test_cross_one_bar_ago_still_counts_two_bars_ago_does_not(self):
        self.assertEqual(self.run_gt(ema_spec(41))["fired"], et.EMA_CROSS)
        old = self.run_gt(ema_spec(42))
        self.assertEqual((old["fired"], old["reason"], old["ema"]["detail"]),
                         (None, et.SKIP_NONE, "no_fresh_cross"))

    def test_no_cross_yet_is_no_trigger(self):
        r = self.run_gt(ema_spec(37))
        self.assertEqual((r["reason"], et.gate(r)), (et.SKIP_NONE, "no_trigger"))

    def test_price_must_be_above_both_emas(self):
        spec = ema_spec(40)
        spec[-1] = (spec[-1][0], spec[-1][1], 100)
        last_c = spec[-1][1]
        # a pullback bar after the cross: fast is still above slow, but the close dives under the fast EMA
        spec = spec + [(last_c, last_c * 0.93, 100)]
        r = self.run_gt(spec)
        self.assertIsNone(r["fired"])
        self.assertEqual(r["ema"]["detail"], "price_not_above_both")

    def test_periods_are_settings(self):
        fast_slow = self.run_gt(ema_spec(40))
        self.on(ema_fast=5, ema_slow=13)
        r = self.run_gt(ema_spec(40))
        self.assertEqual((r["ema"]["ema_fast_n"], r["ema"]["ema_slow_n"]), (5, 13))
        self.assertNotEqual(r["ema_fast_value"], fast_slow["ema_fast_value"])

    def test_15s_timeframe_reads_the_price_ticks(self):
        self.on(ema_timeframe="15s")
        tick_prices([c for _o, c, _v in ema_spec(40)])
        with feed([]):
            r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertEqual((r["fired"], r["ema_tf"], r["ema"]["source"]), (et.EMA_CROSS, "15s", "ticks"))

    def test_too_few_bars_is_unknown(self):
        r = self.run_gt(ema_spec(15))
        self.assertEqual((r["reason"], r["cause"]), (et.SKIP_UNKNOWN, "too_few_bars"))

    def test_a_bad_pair_is_unknown_not_a_crash(self):
        self.on(ema_fast=30, ema_slow=20)
        r = self.run_gt(ema_spec(40))
        self.assertEqual((r["reason"], r["cause"]), (et.SKIP_UNKNOWN, "bad_ema_periods"))


class AnyTrigger(Base):
    def test_defaults_are_all_on_and_shipped(self):
        src = Path(config.__file__).read_text()
        for k in ("rsi_trigger", "breakout_trigger", "vwap_trigger", "ema_trigger"):
            self.assertIn(f'"{k}": True', src)
        for k, v in (("vwap_lookback_min", 60), ("ema_fast", 9), ("ema_slow", 21)):
            self.assertIn(f'"{k}": {v}', src)
        self.assertIn('"ema_timeframe": "1m"', src)
        self.assertIn('"entry_trigger_gate": True', src)            # untouched by this change

    def test_the_other_trigger_can_carry_the_coin(self):
        self.on(rsi_trigger=True, vwap_trigger=True)
        r = self.run_gt(vwap_reclaim_spec())                         # RSI is not oversold here
        self.assertEqual((r["fired"], r["rsi"]["state"]), (et.VWAP_RECLAIM, et.NO))

    def test_two_triggers_firing_are_both_recorded_primary_first(self):
        self.on(rsi_trigger=True, ema_trigger=True)
        spec = oversold_up_spec()
        with mock.patch.object(et, "read_ema", return_value={"state": et.FIRE, "ema_fast": 2.0, "ema_slow": 1.0,
                                                              "ema_fast_n": 9, "ema_slow_n": 21, "tf": "1m"}):
            r = self.run_gt(spec)
        self.assertEqual(r["fired_all"], [et.RSI_OVERSOLD, et.EMA_CROSS])
        self.assertEqual(r["fired"], et.RSI_OVERSOLD)
        self.assertEqual(et.tag(r)["triggers"], [et.RSI_OVERSOLD, et.EMA_CROSS])
        self.assertEqual((et.tag(r)["ema_fast"], et.tag(r)["ema_slow"]), (2.0, 1.0))
        self.assertEqual(et.facts(r)["triggers_fired"], [et.RSI_OVERSOLD, et.EMA_CROSS])

    def test_unknown_only_when_no_enabled_trigger_could_be_computed(self):
        self.on(vwap_trigger=True, ema_trigger=True)
        # nothing at all: both unknown
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch.object(chart, "_pool_address", return_value=None):
            r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertEqual(r["reason"], et.SKIP_UNKNOWN)
        # EMA has too few bars but the VWAP read worked and did not fire: no_trigger, not unknown
        spec = flat(10, 1.0, 100) + [(1.0, 1.01, 100)]
        r = self.run_gt(spec)
        self.assertEqual((r["vwap"]["state"], r["ema"]["state"]), (et.NO, et.UNKNOWN))
        self.assertEqual((r["reason"], et.gate(r)), (et.SKIP_NONE, "no_trigger"))

    def test_unknown_cause_lists_each_missing_source_once(self):
        self.on(rsi_trigger=True, breakout_trigger=True, vwap_trigger=True, ema_trigger=True)
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch.object(chart, "_pool_address", return_value=None):
            r = et.evaluate(MINT, now=NOW, fetch=False)
        toks = r["cause"].split("+")
        self.assertEqual(len(toks), len(set(toks)))
        self.assertLessEqual(len(toks), 3)

    def test_all_switched_off_passes_like_the_gate_off(self):
        r = self.run_gt(rising_spec())
        self.assertIsNone(et.gate(r))
        self.assertTrue(r["none_enabled"])
        self.assertIsNone(r["fired"])

    def test_gate_off_never_skips(self):
        self.on(rsi_trigger=True, entry_trigger_gate=False)
        self.assertIsNone(et.gate(self.run_gt(rising_spec())))


class Toggles(Base):
    def test_rsi_switch(self):
        spec = oversold_up_spec()
        self.on(rsi_trigger=False, breakout_trigger=True)
        r = self.run_gt(spec)
        self.assertEqual(r["rsi"]["state"], et.OFF)
        self.assertIsNone(r["fired"])
        self.on(rsi_trigger=True)
        self.assertEqual(self.run_gt(spec)["fired"], et.RSI_OVERSOLD)

    def test_breakout_switch(self):
        spec = breakout_spec(250)
        self.on(breakout_trigger=False, rsi_trigger=True)
        r = self.run_gt(spec)
        self.assertEqual((r["breakout"]["state"], r["fired"]), (et.OFF, None))
        self.on(breakout_trigger=True)
        self.assertEqual(self.run_gt(spec)["fired"], et.BREAKOUT)

    def test_vwap_and_ema_switches(self):
        self.on(rsi_trigger=True)
        r = self.run_gt(vwap_reclaim_spec())
        self.assertEqual((r["vwap"]["state"], r["fired"]), (et.OFF, None))
        r = self.run_gt(ema_spec(40))
        self.assertEqual((r["ema"]["state"], r["fired"]), (et.OFF, None))
        self.on(vwap_trigger=True, ema_trigger=True)
        self.assertEqual(self.run_gt(vwap_reclaim_spec())["fired"], et.VWAP_RECLAIM)
        self.assertEqual(self.run_gt(ema_spec(40))["fired"], et.EMA_CROSS)

    def test_a_switched_off_trigger_costs_no_read(self):
        self.on(rsi_trigger=True)
        with mock.patch.object(et, "read_vwap") as v, mock.patch.object(et, "read_ema") as e:
            self.run_gt(rising_spec())
        v.assert_not_called()
        e.assert_not_called()


class SettingsRows(Base):
    NEW = ("rulebook.rsi_trigger", "rulebook.breakout_trigger", "rulebook.vwap_trigger",
           "rulebook.vwap_lookback_min", "rulebook.ema_trigger", "rulebook.ema_fast",
           "rulebook.ema_slow", "rulebook.ema_timeframe")

    def test_rows_bounds_and_defaults(self):
        for k in self.NEW:
            self.assertIn(k, settings.REGISTRY)
        r = settings.REGISTRY
        self.assertEqual((r["rulebook.vwap_lookback_min"].lo, r["rulebook.vwap_lookback_min"].hi), (10, 240))
        self.assertEqual((r["rulebook.ema_fast"].lo, r["rulebook.ema_fast"].hi), (3, 50))
        self.assertEqual((r["rulebook.ema_slow"].lo, r["rulebook.ema_slow"].hi), (5, 100))
        self.assertEqual(tuple(r["rulebook.ema_timeframe"].choices), ("1m", "15s"))

    def test_fast_must_stay_below_slow(self):
        vals = {"rulebook.ema_fast": 30, "rulebook.ema_slow": 20}
        self.assertTrue(any("EMA fast" in p for p in settings._cross_check(
            {**{k: s.get() for k, s in settings.REGISTRY.items()}, **vals})))
        ok = {**{k: s.get() for k, s in settings.REGISTRY.items()}}
        self.assertFalse(any("EMA fast" in p for p in settings._cross_check(ok)))

    def test_proposal_never_applies_without_confirm(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DESK_DATA_DIR": tmp}):
            settings.reset_runtime()
            self.on(vwap_trigger=True, ema_trigger=True)
            out = settings.create([{"setting": "rulebook.vwap_trigger", "proposed": False},
                                   {"setting": "rulebook.ema_fast", "proposed": 5}], "t", "desk_ai")
            self.assertTrue(out["ok"], out)
            self.assertTrue(RULEBOOK["vwap_trigger"])
            self.assertEqual(RULEBOOK["ema_fast"], 9)
            settings.confirm(settings.listing()["pending"][0]["id"], "romz")
            self.assertFalse(RULEBOOK["vwap_trigger"])
            self.assertEqual(RULEBOOK["ema_fast"], 5)
            settings.reset_runtime()

    def test_a_crossed_pair_is_refused_on_proposal(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DESK_DATA_DIR": tmp}):
            settings.reset_runtime()
            out = settings.create([{"setting": "rulebook.ema_fast", "proposed": 40},
                                   {"setting": "rulebook.ema_slow", "proposed": 30}], "t", "desk_ai")
            self.assertFalse(out["ok"], out)
            self.assertEqual((RULEBOOK["ema_fast"], RULEBOOK["ema_slow"]), (9, 21))
            settings.reset_runtime()


class AgentWiring(_AW):
    """The same harness the RSI / breakout wiring tests use, with the new triggers in play."""

    def test_vwap_entry_is_tagged_everywhere(self):
        a, _ = self.run_agent(vwap_reclaim_spec(), extra={**ALL_OFF, "vwap_trigger": True})
        self.assertEqual(self.skips(a), [], [e["message"] for e in list(a.events)[:10]])
        self.assertEqual((a.stats["entries"], a.stats["trigger_pass"], a.stats["trigger_pass_vwap"]), (1, 1, 1))
        for k in ("trigger_pass_rsi", "trigger_pass_breakout", "trigger_pass_ema"):
            self.assertEqual(a.stats[k], 0, k)
        dec = a.decisions[0]
        self.assertEqual(dec["entry_trigger"]["trigger"], et.VWAP_RECLAIM)
        self.assertEqual(dec["entry_trigger"]["vwap_kind"], "vwap")
        self.assertIsNotNone(dec["entry_trigger"]["vwap"])
        self.assertEqual(dec["sizes"]["entry_trigger"]["trigger"], et.VWAP_RECLAIM)
        self.assertTrue(any(f["t"] == "trigger_vwap" for f in dec["flags"]))
        buy = [e for e in a.events if e["kind"] == "entry"][0]
        self.assertEqual((buy["trigger"], buy["vwap_kind"]), (et.VWAP_RECLAIM, "vwap"))
        row = self.entry_rows()[0]
        self.assertEqual((row["trigger"], row["vwap_kind"]), (et.VWAP_RECLAIM, "vwap"))
        pos = a.broker.s["positions"][TOK["mint"]]
        self.assertEqual((pos["entry_trigger"], pos["entry_vwap_kind"]), (et.VWAP_RECLAIM, "vwap"))
        self.assertEqual(pos["entry_triggers"], [et.VWAP_RECLAIM])

    def test_ema_entry_is_tagged_and_counted(self):
        a, _ = self.run_agent(ema_spec(40), extra={**ALL_OFF, "ema_trigger": True})
        self.assertEqual((a.stats["entries"], a.stats["trigger_pass_ema"]), (1, 1), [e["message"] for e in list(a.events)[:10]])
        buy = [e for e in a.events if e["kind"] == "entry"][0]
        self.assertEqual(buy["trigger"], et.EMA_CROSS)
        self.assertGreater(buy["ema_fast"], buy["ema_slow"])
        pos = a.broker.s["positions"][TOK["mint"]]
        self.assertEqual((pos["entry_trigger"], pos["entry_ema_fast"] > pos["entry_ema_slow"]), (et.EMA_CROSS, True))
        self.assertTrue(any(f["t"] == "trigger_ema" for f in a.decisions[0]["flags"]))

    def test_rsi_pass_bumps_its_own_counter(self):
        a, _ = self.run_agent(oversold_up_spec(), extra=ALL_OFF | {"rsi_trigger": True})
        self.assertEqual((a.stats["trigger_pass"], a.stats["trigger_pass_rsi"], a.stats["trigger_pass_breakout"]),
                         (1, 1, 0))
        a, _ = self.run_agent(breakout_spec(250), extra=ALL_OFF | {"breakout_trigger": True})
        self.assertEqual((a.stats["trigger_pass_breakout"], a.stats["trigger_pass_rsi"]), (1, 0))

    def test_nothing_fires_is_no_trigger_and_the_none_enabled_gate_passes(self):
        a, _ = self.run_agent(rising_spec(), extra={**ALL_OFF, "vwap_trigger": True, "ema_trigger": True})
        self.assertEqual(a.stats["entries"], 0)
        self.assertIn("no_trigger", self.skips(a))
        self.assertEqual((a.stats["trigger_none"], a.stats["trigger_unknown"]), (1, 0))
        a, _ = self.run_agent(rising_spec(), extra=ALL_OFF)
        self.assertEqual(a.stats["trigger_unknown"], 0)
        self.assertEqual(a.stats["trigger_none"], 0)

    def test_analyst_gets_the_vwap_and_ema_facts(self):
        with mock.patch.object(analyst, "analyze_token", wraps=analyst.analyze_token) as spy:
            self.run_agent(vwap_reclaim_spec(), extra={**ALL_OFF, "vwap_trigger": True})
        ctx = spy.call_args.kwargs["context"]["entry_trigger"]
        self.assertEqual((ctx["trigger"], ctx["vwap_kind"]), (et.VWAP_RECLAIM, "vwap"))
        self.assertIn("vwap", ctx)
        info = {"fired": et.EMA_CROSS, "fired_all": [et.EMA_CROSS], "ema_fast_value": 1.2, "ema_slow_value": 1.1,
                "ema_tf": "1m", "ema": {"ema_fast_n": 9, "ema_slow_n": 21},
                "vwap_value": 0.99, "vwap_kind": "price_avg", "vwap": {"close": 1.0}}
        out = analyst.token_state(TOK, {}, {}, {"scorecard": {}, "dev": {}, "ds": {},
                                                "entry_trigger": et.facts(info)})
        for needle in ('"vwap_kind": "price_avg"', '"ema_fast": 1.2', '"ema_periods": "9/21"'):
            self.assertIn(needle, out)

    def test_live_mode_runs_none_of_it(self):
        with mock.patch.object(et, "evaluate") as ev, mock.patch.object(et, "read_vwap") as v, \
                mock.patch.object(et, "read_ema") as e:
            a, load = self.run_agent(None, mode="live", extra={"vwap_trigger": True, "ema_trigger": True})
        for m in (ev, v, e):
            m.assert_not_called()
        load.assert_not_called()
        for row in self.entry_rows():
            self.assertNotIn("vwap", row)
            self.assertNotIn("ema_fast", row)


del _AW                      # keep unittest from collecting the imported base harness twice
if __name__ == "__main__":
    unittest.main()
