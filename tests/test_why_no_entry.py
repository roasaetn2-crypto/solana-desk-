"""Tests for scripts/why_no_entry.py: the read-only "why no entry" diagnostic.

The point of these tests is that the diagnostic must be honest: it groups the
journal by the gate that actually fired, and it only calls a block "hard" when
the desk really cannot enter (daily stop, breaker cooldown, full book, TP off).
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("why_no_entry", ROOT / "scripts" / "why_no_entry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WNE = _load()


def _row(kind: str, **kw) -> dict:
    return {"time": datetime.now(timezone.utc).isoformat(), "kind": kind, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"DESK_DATA_DIR": str(self.dir)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_journal(self, rows: list[dict]) -> None:
        (self.dir / "journal.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def write_ledger(self, **over) -> None:
        led = {"cash": 50.0, "start_equity": 50.0, "day_start": 50.0,
               "day": "2026-01-01", "positions": {}, "closed": []}
        led.update(over)
        (self.dir / "paper_ledger.json").write_text(json.dumps(led), encoding="utf-8")


class Empty(Base):
    def test_no_journal_is_not_a_hard_block(self):
        a = WNE.analyse(24, 100)
        self.assertEqual(a["journal_rows"], 0)
        self.assertFalse(a["hard_block"])
        self.assertIn("No journal", a["verdict"])


class Tally(Base):
    def test_rejects_are_grouped_by_gate(self):
        self.write_journal(
            [_row("reject", stage="scanner", reason="chase", symbol="AAA") for _ in range(4)]
            + [_row("reject", stage="risk", reason="score 5 < 6", symbol="BBB") for _ in range(2)]
            + [_row("reject", stage="confirm", reason="price_gap", symbol="CCC")]
            + [_row("entry", mint="m9", symbol="WIN")])
        a = WNE.analyse(24, 100)
        tally = dict(a["tally"])
        self.assertEqual(tally["scanner: chase"], 4)
        self.assertEqual(tally["risk: thesis score below min"], 2)
        self.assertEqual(tally["confirm: price moved past gap cap"], 1)
        self.assertEqual(a["entries"], 1)
        self.assertIn("chase", a["verdict"])
        self.assertFalse(a["hard_block"])

    def test_window_excludes_old_rows(self):
        old = {"time": datetime.fromtimestamp(time.time() - 48 * 3600, tz=timezone.utc).isoformat(),
               "kind": "reject", "stage": "scanner", "reason": "chase", "symbol": "OLD"}
        self.write_journal([old, _row("reject", stage="scanner", reason="age", symbol="NEW")])
        a = WNE.analyse(6, 100)
        tally = dict(a["tally"])
        self.assertNotIn("scanner: chase", tally)
        self.assertEqual(tally["scanner: age"], 1)


class HardBlocks(Base):
    def test_daily_halt_is_a_hard_block(self):
        self.write_ledger(cash=30.0, day_start=50.0)
        self.write_journal([_row("reject", stage="risk", reason="daily loss stop")])
        a = WNE.analyse(24, 100)
        self.assertTrue(a["ledger"]["blocking_entries"])
        self.assertTrue(a["hard_block"])
        self.assertIn("Daily loss stop", a["verdict"])
        self.assertIn("/api/risk/continue", a["fix"])

    def test_full_book_is_a_hard_block(self):
        pos = {f"m{i}": {"symbol": f"S{i}", "qty": 10, "entry": 1.0, "last": 1.0,
                         "stop_price": 0.85, "horizon": "scalp"} for i in range(6)}
        self.write_ledger(positions=pos)
        a = WNE.analyse(24, 100)
        self.assertTrue(a["hard_block"])
        self.assertIn("Book is full", a["verdict"])

    def test_override_clears_the_daily_halt(self):
        from datetime import date
        self.write_ledger(cash=30.0, day_start=50.0,
                          daily_loss_override_date=str(date.today()))
        a = WNE.analyse(24, 100)
        self.assertTrue(a["ledger"]["daily_halted"])
        self.assertTrue(a["ledger"]["override_today"])
        self.assertFalse(a["ledger"]["blocking_entries"])


class TakeProfit(Base):
    def test_scalp_ladder_self_test_fires(self):
        t = WNE.tp_self_test()
        self.assertTrue(t["ok"])
        self.assertIn("TP1", t["tp1"])
        self.assertIsNotNone(t["stop"])

    def test_tp_view_is_armed_by_default(self):
        tp = WNE.tp_view()
        self.assertTrue(tp["active"])
        self.assertTrue(any(r["label"] == "TP1" for r in tp["rungs"]))

    def test_single_plan_still_has_one_rung(self):
        import config
        with mock.patch.dict(config.EXITS, {"take_profit_mode": "single"}):
            tp = WNE.tp_view()
        self.assertTrue(tp["active"])
        self.assertEqual(len(tp["rungs"]), 1)


class Cli(Base):
    def test_main_returns_zero_when_nothing_blocks(self):
        with mock.patch("sys.argv", ["why_no_entry", "--json"]):
            self.assertEqual(WNE.main(), 0)

    def test_main_returns_two_on_daily_halt(self):
        self.write_ledger(cash=30.0, day_start=50.0)
        with mock.patch("sys.argv", ["why_no_entry", "--json"]):
            self.assertEqual(WNE.main(), 2)


if __name__ == "__main__":
    unittest.main()
