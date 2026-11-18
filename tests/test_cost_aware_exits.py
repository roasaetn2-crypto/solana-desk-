"""0 wins in 30 closed paper trades (2026-11-19): the ladder ignored its own costs.

Paper fills pay 2% slippage each way plus a fixed Solana fee per swap. At a
~$2.50 ticket that is ~6% of price round trip, so the Hyper Scalp ladder's
+4% TP1 booked a loss and the "breakeven" stop at the entry fill lost another
~4% on the runner: a trade that hit TP1 and faded still closed red.
"""
import os
import tempfile
import time
import unittest

import config
from desk import exits, meme_tactics
from desk.broker import PaperBroker

HYPER = {"enabled": True, "stop_pct": 0.05, "tp1_at": 1.04, "tp1_frac": 0.5,
         "tp2_at": 1.08, "tp2_frac": 0.5, "trail_pct": 0.03, "timeout_minutes": 5,
         "max_hold_minutes": 10, "timeout_min_gain": 0.01}


class _Book(unittest.TestCase):
    def setUp(self):
        self._scalp = dict(config.SCALP)
        self._exits = {k: config.EXITS.get(k) for k in
                       ("cost_aware_breakeven", "tp_cost_floor", "tp_min_net_edge")}
        self._sol = config.EXECUTION.get("live_sol_price_usd")
        config.SCALP.update(HYPER)
        config.EXECUTION["live_sol_price_usd"] = 140.0
        d = tempfile.mkdtemp()
        self.b = PaperBroker(os.path.join(d, "paper_ledger.json"))
        self.b.reset()

    def tearDown(self):
        config.SCALP.clear()
        config.SCALP.update(self._scalp)
        config.EXITS.update(self._exits)
        config.EXECUTION["live_sol_price_usd"] = self._sol

    def trade(self, path, ticket=2.5):
        """Buy at quote 1.0, then feed quotes (as multiples of the QUOTE) a minute apart."""
        self.b.buy("M", "X", ticket, 1.0, horizon="scalp")
        t0 = time.time()
        self.b.s["positions"]["M"]["opened"] = t0
        reasons = []
        for i, px in enumerate(path):
            if "M" not in self.b.s["positions"]:
                break
            sig = self.b.exit_signal("M", {"price_usd": px}, now=t0 + 60 * (i + 1))
            if sig:
                pos = self.b.s["positions"]["M"]
                price = pos.pop("fill_at", None) or pos["last"]
                self.b.sell("M", sig[0], price, sig[1])
                reasons.append(sig[1])
        return reasons, [c["pnl_usd"] for c in self.b.s["closed"]]


class CostAwareLadder(_Book):
    def test_tp1_then_fade_is_a_win_not_a_loss(self):
        # Quote runs to +25%, TP1 fires, then the coin fades back to the entry quote.
        # (Flat $0.10/side on a $2.50 ticket is 8% round trip, so TP1 sits higher.)
        reasons, pnl = self.trade([1.25, 1.25, 1.0])
        self.assertTrue(reasons[0].startswith("TP1"), reasons)
        self.assertEqual(reasons[-1], "breakeven stop")
        self.assertEqual(len(pnl), 1)
        self.assertGreater(pnl[0], 0, "a trade that banked TP1 must not close red")

    def test_old_behaviour_was_a_guaranteed_loss(self):
        config.EXITS["tp_cost_floor"] = False
        config.EXITS["cost_aware_breakeven"] = False
        reasons, pnl = self.trade([1.07, 1.07, 0.9])
        self.assertTrue(reasons[0].startswith("TP1 at 1.04"), reasons)
        self.assertLess(pnl[0], 0)     # the bug this file guards against

    def test_tp1_never_fires_below_net_cost(self):
        # +7% on the quote is above the configured 1.04x of the fill but below cost.
        reasons, _ = self.trade([1.07])
        self.assertEqual(reasons, [])
        pos = self.b.s["positions"]["M"]
        tp1, tp2 = exits.effective_ladder(pos, exits.profile("scalp"))
        be = exits.net_breakeven_mult(pos, 0.5)
        self.assertGreater(tp1, be)
        self.assertGreater(tp1, 1.04)
        self.assertAlmostEqual(tp2 - tp1, 0.04, places=3)   # spacing kept

    def test_tp1_slice_nets_the_min_edge(self):
        reasons, _ = self.trade([1.25])
        self.assertIn("cost floor", reasons[0])
        pos = self.b.s["positions"]["M"]
        # TP1 proceeds for the slice beat its share of cost by about the edge.
        share = pos["cost"] * 0.5
        self.assertGreater(pos["proceeds"], share)

    def test_floor_does_nothing_when_ladder_already_clears_cost(self):
        config.SCALP.update({"tp1_at": 1.5, "tp2_at": 2.0})
        self.b.buy("M", "X", 2.5, 1.0, horizon="scalp")
        pos = self.b.s["positions"]["M"]
        self.assertEqual(exits.effective_ladder(pos, exits.profile("scalp")), (1.5, 2.0))

    def test_positions_without_cost_basis_keep_configured_ladder(self):
        pos = {"entry": 1.0, "qty": 10.0, "opened": time.time()}
        exits.init_state(pos, "scalp")
        pr = exits.profile("scalp")
        self.assertEqual(exits.effective_ladder(pos, pr), (pr["tp1_at"], pr["tp2_at"]))
        self.assertIsNone(exits.net_breakeven_mult(pos, 1.0))

    def test_stop_still_a_loss(self):
        reasons, pnl = self.trade([0.9])
        self.assertTrue(reasons[0].startswith("stop"))
        self.assertLess(pnl[0], 0)


class ScalpEntryRule(unittest.TestCase):
    def bars(self, *ocs):
        return [{"t": 1000 + 300 * i, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 1}
                for i, (o, c) in enumerate(ocs)]

    def test_moving_candle_accepts_green_rising_bar(self):
        r = meme_tactics.scalp_entry_state(self.bars((1, 1.0), (1.0, 1.05)), "moving_candle", 1, 0.003)
        self.assertIs(r["ok"], True)

    def test_moving_candle_rejects_red_crash(self):
        r = meme_tactics.scalp_entry_state(self.bars((1, 1.0), (1.0, 0.34)), "moving_candle", 1, 0.003)
        self.assertIs(r["ok"], False)
        self.assertIn("red", r["why"])

    def test_moving_candle_rejects_doji_and_lower_close(self):
        self.assertIs(meme_tactics.scalp_entry_state(
            self.bars((1, 1.0), (1.0, 1.001)), "moving_candle", 1, 0.003)["ok"], False)
        self.assertIs(meme_tactics.scalp_entry_state(
            self.bars((1, 1.2), (1.0, 1.1)), "moving_candle", 1, 0.003)["ok"], False)

    def test_no_or_stale_candles_are_neutral(self):
        self.assertIsNone(meme_tactics.scalp_entry_state([], "moving_candle")["ok"])
        b = self.bars((1, 1.0), (1.0, 1.05))
        self.assertIsNone(meme_tactics.scalp_entry_state(b, "moving_candle", 1, 0.0,
                                                         now=b[-1]["t"] + 3600)["ok"])
        self.assertIs(meme_tactics.scalp_entry_state(b, "moving_candle", 1, 0.0,
                                                     now=b[-1]["t"] + 60)["ok"], True)

    def test_long_key_candles_and_other_modes(self):
        long = [{"open": 1, "high": 1, "low": 1, "close": 1}, {"open": 1, "high": 1, "low": 0.9, "close": 0.95}]
        self.assertIs(meme_tactics.scalp_entry_state(long, "dip")["ok"], True)
        self.assertIs(meme_tactics.scalp_entry_state(long, "moving_candle")["ok"], False)
        up = self.bars((1, 1.0), (1.0, 1.2))
        self.assertIs(meme_tactics.scalp_entry_state(up, "breakout", 1)["ok"], True)


class AgentReadsEntryMode(unittest.TestCase):
    def test_agent_source_enforces_entry_mode(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "desk", "solana_agent.py")).read()
        self.assertIn("scalp_entry_state(", src)
        self.assertIn('"scalp_candle"', src)
        self.assertIn("scalp_candle", config.BENCH_MINUTES)


if __name__ == "__main__":
    unittest.main()
