"""P3 (2026-11-19, PAPER): no candles = no confirmed scalp entry, and the momentum exit's anchor is real.

Market ANALYSIS found 0/7 clean paper scalps won. Two entries passed with `scalp_entry: no candles
(sparse_tape)`, and their momentum-break anchor came from a price snapshot above the fill (GAYTA +9.6%,
FNDR +13.1%), so GAYTA was cut at +8.7% and still lost after fees. Three fixes, each pinned here:

1. a scalp entry whose candle check is unknown (ok None) is rejected as `scalp_no_candles`;
2. entry_candle_low = min(anchor, fill): never above the price we paid;
3. the anchor and the momentum exit use REAL exchange 1m candles only (not tick buckets / tape bars).
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from config import SCALP  # noqa: E402
from desk import marketdata  # noqa: E402
from desk.broker import PaperBroker  # noqa: E402
from desk.solana_agent import SolanaAgent  # noqa: E402
import test_entry_trigger as _et  # noqa: E402
from test_confirm_entry import GOOD_DS, GOOD_T, UP, bars  # noqa: E402


def _agent():
    d = Path(tempfile.mkdtemp())
    return SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")


class RejectUnknownScalpEntry(unittest.TestCase):
    run_agent = _et.AgentWiring.run_agent

    def run_scalp(self, fin, require=True):
        with mock.patch.dict(SCALP, {"enabled": True, "entry_gate": True, "entry_mode": "moving_candle",
                                     "moving_candle_bars": 1, "moving_candle_min_body_pct": 0.0,
                                     "require_candles": require}):
            a, _ = self.run_agent(None, gate=False, fin=fin, extra={"entry_trigger_gate": False})
        return a

    def rejects(self, a, reason):
        return [e for e in a.events if e.get("reason") == reason]

    def test_no_candles_is_rejected_with_a_clear_funnel_reason(self):
        fin = {"1m": [], "5m": [], "source": "none", "cause": "gt_cooling+no_tape", "tape_ticks_5m": 0}
        a = self.run_scalp(fin)
        self.assertEqual(a.funnel["scalp_no_candles"], 1)
        self.assertEqual(a.stats["scalp_no_candles"], 1)
        self.assertEqual(a.stats["entries"], 0)
        ev = self.rejects(a, "scalp_no_candles")
        self.assertEqual(ev[0]["stage"], "entry")
        self.assertIn("gt_cooling+no_tape", ev[0]["message"])
        rows = [c for c in self.journal.call_args_list if c.kwargs.get("reason") == "scalp_no_candles"]
        self.assertTrue(rows)                        # also journaled for the review

    def test_sparse_tape_is_rejected_not_passed(self):
        now = time.time()
        b = marketdata.aggregate([{"t": int(now // 60) * 60, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 0.0}], 300)
        fin = {"1m": [], "5m": b, "source": "price_tape", "cause": None, "tape_ticks_5m": 1}
        a = self.run_scalp(fin)
        self.assertEqual(a.funnel["scalp_no_candles"], 1)
        self.assertEqual(a.stats["entries"], 0)

    def test_switch_off_restores_the_old_neutral_pass(self):
        fin = {"1m": [], "5m": [], "source": "none", "cause": "no_data", "tape_ticks_5m": 0}
        a = self.run_scalp(fin, require=False)
        self.assertEqual(a.funnel["scalp_no_candles"], 0)
        self.assertEqual(self.rejects(a, "scalp_no_candles"), [])

    def test_a_known_red_candle_keeps_its_own_reason(self):
        now = time.time()
        pr = [1.0, 1.0, 1.0, 0.99, 0.97, 0.95, 0.94, 0.92]
        end = int(now // 300) * 300 - 60
        b = marketdata.aggregate([{"t": end - 60 * (len(pr) - 1 - i), "o": p, "h": p, "l": p, "c": p, "v": 0.0}
                                  for i, p in enumerate(pr)], 300)
        fin = {"1m": [], "5m": b, "source": "price_tape", "cause": None, "tape_ticks_5m": 4}
        a = self.run_scalp(fin)
        self.assertEqual(a.funnel["scalp_no_candles"], 0)
        self.assertGreaterEqual(a.funnel["scalp_candle"], 1)

    def test_bench_sits_a_short_time_so_the_coin_is_rechecked(self):
        self.assertEqual(config.BENCH_MINUTES["scalp_no_candles"], 3)


class ConfirmStepNeedsCandles(unittest.TestCase):
    def setUp(self):
        saved = SCALP.get("require_candles")
        SCALP["require_candles"] = True
        self.addCleanup(lambda: SCALP.__setitem__("require_candles", saved))

    def test_no_candles_at_all_rejects_the_confirm_step(self):
        a = _agent()
        f = {}
        with mock.patch("desk.solana_agent.candle_chart.series", return_value={"candles": [], "source": "none"}), \
                mock.patch.object(marketdata, "finalist_bars",
                                  return_value={"1m": [], "5m": [], "source": "none", "cause": "no_data",
                                                "tape_ticks_5m": 0}):
            self.assertTrue(a._confirm_skip(dict(GOOD_T, mint="NOCANDLES"), GOOD_DS, f))
        self.assertEqual(a.funnel["scalp_no_candles"], 1)
        self.assertNotIn("entry_candle_low", f)

    def test_real_candles_still_pass(self):
        a = _agent()
        now = time.time()

        def series(mint, tf, session=None, **kw):
            return {"candles": bars(UP, 60 if tf == "1m" else 300, end=now), "source": "geckoterminal"}
        f = {}
        with mock.patch("desk.solana_agent.candle_chart.series", side_effect=series):
            self.assertFalse(a._confirm_skip(dict(GOOD_T, mint="REAL"), GOOD_DS, f))
        self.assertEqual(a.funnel["scalp_no_candles"], 0)
        self.assertGreater(f["entry_candle_low"], 0)


class AnchorFromRealCandlesOnly(unittest.TestCase):
    def confirm(self, source, via=None, series_candles=True):
        a = _agent()
        now = time.time()
        cs = bars(UP, 60, end=now)
        c5 = bars(UP, 300, end=now)

        def series(mint, tf, session=None, **kw):
            return {"candles": (cs if tf == "1m" else c5) if series_candles else [], "source": source}
        fin = {"1m": cs, "5m": c5, "source": via or source, "cause": None, "tape_ticks_5m": 5}
        if via == "cache":
            fin["via"] = "price_tape"
        f = {}
        with mock.patch("desk.solana_agent.candle_chart.series", side_effect=series), \
                mock.patch.object(marketdata, "finalist_bars", return_value=fin):
            a._confirm_skip(dict(GOOD_T, mint="ANCHOR"), GOOD_DS, f)
        return f

    def test_geckoterminal_candles_set_the_anchor(self):
        self.assertIn("entry_candle_low", self.confirm("geckoterminal"))

    def test_tick_buckets_do_not_set_the_anchor(self):
        self.assertNotIn("entry_candle_low", self.confirm("ticks"))

    def test_price_tape_bars_do_not_set_the_anchor(self):
        f = self.confirm("none", via="price_tape", series_candles=False)
        self.assertNotIn("entry_candle_low", f)

    def test_cached_tape_bars_do_not_set_the_anchor(self):
        f = self.confirm("none", via="cache", series_candles=False)
        self.assertNotIn("entry_candle_low", f)


class AnchorNeverAboveTheFill(unittest.TestCase):
    def buy(self, low):
        d = tempfile.mkdtemp()
        b = PaperBroker(Path(d) / "ledger.json")
        b.buy("MINTX", "X", 10.0, 1.0, horizon="scalp", meta={"entry_candle_low": low, "entry_liq": 5})
        return b.s["positions"]["MINTX"]

    def test_anchor_above_the_fill_is_clamped_to_the_fill(self):
        pos = self.buy(1.096)                        # GAYTA: anchor 9.6% above the price
        self.assertLessEqual(pos["entry_candle_low"], pos["entry"])
        self.assertAlmostEqual(pos["entry_candle_low"], pos["entry"])
        self.assertEqual(pos["entry_liq"], 5)        # other meta untouched

    def test_anchor_below_the_fill_is_kept(self):
        pos = self.buy(0.9)
        self.assertEqual(pos["entry_candle_low"], 0.9)

    def test_no_anchor_stays_no_anchor(self):
        d = tempfile.mkdtemp()
        b = PaperBroker(Path(d) / "ledger.json")
        b.buy("MINTY", "Y", 10.0, 1.0, horizon="scalp", meta={"entry_liq": 5})
        self.assertNotIn("entry_candle_low", b.s["positions"]["MINTY"])

    def test_agent_meta_takes_min_with_the_buy_price(self):
        src = (Path(__file__).resolve().parents[1] / "desk" / "solana_agent.py").read_text()
        self.assertIn('min(f["entry_candle_low"], buy_price)', src)


class MomentumExitArmsOnRealCandlesOnly(unittest.TestCase):
    def exit_with(self, source):
        a = _agent()
        now = time.time()
        pos = {"symbol": "G", "horizon": "scalp", "entry": 1.24, "opened": now - 300, "stage": 0,
               "entry_candle_low": 1.22, "last": 1.2}
        broke = bars(UP[:-1] + [1.21], end=now - 1)         # last closed bar under the anchor

        def series(mint, tf, session=None, **kw):
            return {"candles": broke, "source": source}
        with mock.patch("desk.solana_agent.candle_chart.series", side_effect=series):
            return a._rulebook_exit("MINTG", pos, None)

    def test_real_1m_candles_arm_the_exit(self):
        r = self.exit_with("geckoterminal")
        self.assertTrue(r and "entry candle low" in r[1], r)

    def test_tick_bucket_candles_never_fire_it(self):
        r = self.exit_with("ticks")
        self.assertFalse(r and "momentum" in str(r[1]), r)


if __name__ == "__main__":
    unittest.main()
