"""The trade tape must say how many tokens a buy actually bought.

$12 of an 8.8e-7 token is ~13.5M units, and that count is the position. The
entry event always carried `qty`, but `_activity()` dropped it, so the tape
could only ever show the ticket in dollars. These tests pin the field through
the whole path the dashboard reads: entry event -> _activity row -> snapshot.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desk.solana_agent import SolanaAgent


class ActivityQty(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"DESK_DATA_DIR": str(self.d)})
        self.env.start()
        self.a = SolanaAgent(ledger=self.d / "p.json", bench_path=self.d / "b.db",
                             live_ledger=self.d / "l.json")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _entry(self, mint="Mint1111", symbol="WIF", qty=13_560_431.0,
               fill=8.85e-7, ticket=12.0):
        self.a.emit("entry", f"PAPER BUY ${symbol} \u2014 ticket ${ticket}",
                    mint=mint, symbol=symbol, qty=qty, fill=fill,
                    ticket_usd=ticket, score=8, live=False)

    def test_entry_row_carries_qty(self):
        self._entry()
        rows = self.a._activity({})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "entry")
        self.assertAlmostEqual(rows[0]["qty"], 13_560_431.0)
        self.assertAlmostEqual(rows[0]["usd"], 12.0)

    def test_qty_survives_the_snapshot(self):
        self._entry()
        snap = self.a.status()
        entry = next(r for r in snap["activity"] if r["kind"] == "entry")
        self.assertAlmostEqual(entry["qty"], 13_560_431.0)

    def test_exit_rows_still_have_no_entry_qty(self):
        # An exit event has no `qty` of its own; the field must stay None rather
        # than inheriting a stale count from somewhere else.
        self.a.emit("exit", "closed WIF \u2014 stop", mint="Mint1111", symbol="WIF",
                    fraction=1.0, entry=8.85e-7, fill=9.1e-7, pnl_usd=0.34)
        rows = self.a._activity({})
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["qty"])

    def test_open_position_row_has_qty(self):
        # The position card reads broker.positions[].qty, which already existed;
        # this guards it so the new card chip cannot silently lose its source.
        self.a.broker.buy("Mint1111", "WIF", 12.0, 8.85e-7)
        pos = self.a.broker.snapshot()["positions"][0]
        self.assertGreater(pos["qty"], 0)


if __name__ == "__main__":
    unittest.main()
