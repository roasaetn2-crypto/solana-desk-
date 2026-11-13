"""A bad quote must never reprice the book.

Regression for 2026-11-14: the dashboard showed

    equity   $549,195,436.20
    day P&L  +$549,195,369.45   (+343,247,105.91%)
    multiple 3,432,471.477x

on a $160 paper bankroll. One position was marked from a quote that was not a
price for that token. Three independent guards are tested here:

  desk/exits.py: plausible()   a single impossible tick is not accepted
  desk/broker.py: _value()     a mark outside the sane band is valued at cost
  desk/broker.py: _repair()    an already-corrupt ledger heals on load
"""
import json
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from desk.broker import PaperBroker  # noqa: E402

MINT = "So11111111111111111111111111111111111111112"
START = float(config.PAPER["starting_equity_usd"])


def quote(price, liq=50_000.0, vol=100_000.0):
    return {"price_usd": price, "liquidity_usd": liq, "volume_h6": vol,
            "volume_h24": vol * 4, "volume_m5": vol / 12}


class BadPrintIsNotAMove(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper_ledger.json"
        self.b = PaperBroker(self.path)
        self.b.buy(MINT, "TEST", 4.0, 1e-4, horizon="swing")
        self.pos = self.b.s["positions"][MINT]
        self.good = self.pos["last"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_20pct_tick_is_a_move_and_is_accepted(self):
        sig = self.b.exit_signal(MINT, quote(self.good * 1.2))
        self.assertIsNone(sig)
        self.assertAlmostEqual(self.pos["last"], self.good * 1.2, places=12)

    def test_a_1e9_tick_is_ignored(self):
        """The exact shape of the bug: one print, 1e9 times the last price."""
        before = self.b.equity()
        sig = self.b.exit_signal(MINT, quote(self.good * 1e9))
        self.assertIsNone(sig, "a bad print must not trigger an exit")
        self.assertAlmostEqual(self.pos["last"], self.good, places=12,
                               msg="last must not move on an impossible print")
        self.assertAlmostEqual(self.b.equity(), before, places=2,
                               msg="equity must not move on an impossible print")

    def test_a_crash_by_8_orders_of_magnitude_is_ignored_too(self):
        before = self.b.equity()
        self.b.exit_signal(MINT, quote(self.good * 1e-8))
        self.assertAlmostEqual(self.pos["last"], self.good, places=12)
        self.assertAlmostEqual(self.b.equity(), before, places=2)

    def test_the_book_stays_believable_after_the_bad_print(self):
        self.b.exit_signal(MINT, quote(self.good * 1e9))
        eq = self.b.equity()
        self.assertLess(eq, START * 2, f"equity {eq} should stay near the bankroll")

    def test_two_agreeing_reads_accept_a_real_gap_up(self):
        """A genuine gap-up must not be blocked forever: two reads that agree win."""
        target = self.good * 100
        self.assertIsNone(self.b.exit_signal(MINT, quote(target)))
        self.assertAlmostEqual(self.pos["last"], self.good, places=12)
        self.b.exit_signal(MINT, quote(target * 1.01))
        self.assertAlmostEqual(self.pos["last"], target * 1.01, places=12)

    def test_two_reads_that_disagree_are_still_rejected(self):
        self.b.exit_signal(MINT, quote(self.good * 1e9))
        self.b.exit_signal(MINT, quote(self.good * 1e3))     # agrees with nothing
        self.assertAlmostEqual(self.pos["last"], self.good, places=12)

    def test_repeated_bad_prints_close_at_the_last_good_price(self):
        sig = None
        for _ in range(config.EXITS["missing_quote_closes"]):
            sig = self.b.exit_signal(MINT, quote(self.good * 1e9))
        self.assertIsNotNone(sig, "a position that can never be priced must free its slot")
        fraction, reason = sig
        self.assertEqual(fraction, 1.0)
        self.assertIn("bad quote", reason)
        self.assertLess(self.b.equity(), START * 2)


class EquityClamp(unittest.TestCase):
    """Even a ledger that is already corrupt cannot fake a $549M equity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper_ledger.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self):
        s = {"cash": START - 4.0, "start_equity": START, "day": str(date.today()),
             "day_start": START, "closed": [],
             "positions": {MINT: {"symbol": "TEST", "qty": 40_000.0, "entry": 1e-4,
                                  "last": 1e-4, "opened": time.time(), "cost": 4.0,
                                  "half_taken": False, "horizon": "swing", "stop_pct": 0.3,
                                  "peak": 1e-4, "stage": 0, "miss": 0, "stop_price": 7e-5}}}
        self.path.write_text(json.dumps(s))
        return s

    def test_a_mark_a_billion_times_off_is_valued_at_cost(self):
        """The repair fixes it on load; if a bad mark arrives later, _value holds."""
        self.write()
        b = PaperBroker(self.path)
        b.s["positions"][MINT]["last"] = 1e-4 * 1e9      # arrives mid-session
        self.assertLess(b.equity(), START * 2)
        self.assertAlmostEqual(b.equity(), START, places=2)
        self.assertTrue(b.clamped(), "the clamp must be visible, not silent")
        self.assertEqual(b.clamped()[0]["mint"], MINT)

    def test_a_nan_mark_does_not_poison_equity(self):
        s = self.write()
        s["positions"][MINT]["last"] = float("nan")
        self.path.write_text(json.dumps(s))
        b = PaperBroker(self.path)
        self.assertEqual(b.equity(), b.equity())          # not NaN
        self.assertLess(b.equity(), START * 2)

    def test_a_clean_book_is_untouched(self):
        self.write()
        b = PaperBroker(self.path)
        self.assertAlmostEqual(b.equity(), START, places=2)
        self.assertEqual(b.clamped(), [])


class RepairOnLoad(unittest.TestCase):
    """The exact broken ledger heals on the next boot instead of needing a Reset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper_ledger.json"

    def tearDown(self):
        self.tmp.cleanup()

    def broken(self):
        """$549,195,436.20 equity: cash blown out by a sell into a bad print."""
        huge = 549_195_369.45
        return {"cash": huge + 66.75, "start_equity": START, "day": str(date.today()),
                "day_start": 66.75, "closed": [{"mint": "X", "symbol": "BAD",
                                                "pnl_usd": huge, "reason": "TP1",
                                                "closed": time.time()},
                                               {"mint": "Y", "symbol": "OK",
                                                "pnl_usd": 1.25, "reason": "TP1",
                                                "closed": time.time()}],
                "positions": {MINT: {"symbol": "TEST", "qty": 40_000.0, "entry": 1e-4,
                                     "last": 1e-4 * 1e6, "opened": time.time(), "cost": 4.0,
                                     "half_taken": False, "horizon": "swing", "stop_pct": 0.3,
                                     "peak": 1e-4 * 1e6, "stage": 0, "miss": 0,
                                     "stop_price": 7e-5}}}

    def test_the_549m_ledger_comes_back_sane(self):
        self.path.write_text(json.dumps(self.broken()))
        b = PaperBroker(self.path)
        self.assertLess(b.equity(), START * 2, f"equity {b.equity()} is still impossible")
        self.assertGreater(b.equity(), 0)
        self.assertEqual(b.clamped(), [], "after repair nothing should need clamping")

    def test_repair_keeps_the_position_and_the_history(self):
        self.path.write_text(json.dumps(self.broken()))
        b = PaperBroker(self.path)
        self.assertIn(MINT, b.s["positions"], "repair must not delete a position")
        # the impossible row is dropped, the honest one is kept
        self.assertEqual([c["symbol"] for c in b.s["closed"]], ["OK"],
                         "repair must drop the fake trade and keep the real ones")
        self.assertAlmostEqual(b.s["positions"][MINT]["last"], 1e-4, places=12)

    def test_repair_is_idempotent(self):
        self.path.write_text(json.dumps(self.broken()))
        first = PaperBroker(self.path).equity()
        second = PaperBroker(self.path).equity()
        self.assertAlmostEqual(first, second, places=2)

    def test_a_clean_ledger_is_not_repaired(self):
        self.path.write_text(json.dumps({"cash": START, "start_equity": START,
                                         "day": str(date.today()),
                                         "day_start": START, "closed": [], "positions": {}}))
        b = PaperBroker(self.path)
        self.assertEqual(b._repair(), [])
        self.assertAlmostEqual(b.equity(), START, places=2)


class CashIdentity(unittest.TestCase):
    """cash = start + closed P&L + open proceeds - open cost, exactly."""

    def test_a_normal_round_trip_keeps_the_identity(self):
        with tempfile.TemporaryDirectory() as d:
            b = PaperBroker(Path(d) / "paper_ledger.json")
            b.buy(MINT, "TEST", 4.0, 1e-4)
            b.sell(MINT, 0.5, 2e-4, "TP1")
            b.sell(MINT, 1.0, 1e-4, "stop")
            start = b.s["start_equity"]
            realized = sum(c["pnl_usd"] for c in b.s["closed"])
            open_cost = sum(p["cost"] for p in b.s["positions"].values())
            open_proc = sum(p.get("proceeds", 0.0) for p in b.s["positions"].values())
            self.assertAlmostEqual(b.s["cash"], start + realized + open_proc - open_cost, places=4)
            self.assertEqual(b._repair(), [])


if __name__ == "__main__":
    unittest.main()
