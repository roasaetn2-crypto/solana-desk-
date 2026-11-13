"""Backtest -> AI recommendation: isolated sweeps, pure recommend(), offline fallback.

These lock in the fix for the broken "Recommend settings" feature:
  * every actionable sweep moves exactly ONE live setting (so it is attributable),
  * recommend() only suggests a change that actually beat current risk-adjusted,
  * performance() forwards EVERY row plus the recommendation,
  * the desk can still recommend with no AI key (deterministic fallback),
  * backtest-only gate names are normalised to real registry keys.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import backtest  # noqa: E402
from desk import desk_chat as dc  # noqa: E402
from desk import settings as st  # noqa: E402

BT_KEYS = ("min_score", "max_chase", "min_alive_6h",
           "max_price_gap_pct", "max_h1_change_pct", "min_thesis_score")


def _row(label, *, current=False, actionable=True, bundle=False, trades=0,
         wins=0, win_rate=0.0, pnl=0.0, dd=0.0, **settings):
    s = {k: settings.get(k) for k in BT_KEYS}
    return {
        "label": label, "current": current, "actionable": actionable, "bundle": bundle,
        "trades": trades, "wins": wins, "win_rate": win_rate, "pnl_usd": pnl,
        "max_drawdown_usd": dd, "unknown": 0, "passed_gates": trades, "settings": s,
    }


class GridIsolation(unittest.TestCase):
    def test_current_row_is_first_and_actionable(self):
        grid = backtest.default_grid()
        self.assertTrue(grid[0]["current"])
        self.assertTrue(grid[0]["actionable"])

    def test_each_actionable_row_moves_one_live_knob(self):
        grid = backtest.default_grid()
        cur = grid[0]
        live = set(backtest.LIVE_SETTING_KEYS)
        for row in grid:
            if row.get("current") or not row.get("actionable"):
                continue
            changed = {k for k in live if row.get(k) != cur.get(k)}
            if row.get("bundle"):
                self.assertGreaterEqual(len(changed), 1, row["label"])
                continue
            self.assertEqual(len(changed), 1,
                             f"{row['label']} should move exactly one live knob, moved {changed}")

    def test_actionable_knobs_are_settable_and_in_bounds(self):
        grid = backtest.default_grid()
        for row in grid:
            if not row.get("actionable") or row.get("current"):
                continue
            for bt_key, reg_key in backtest.LIVE_SETTING_KEYS.items():
                if row.get(bt_key) is None:
                    continue
                lo, hi = backtest.LIVE_SETTING_BOUNDS[reg_key]
                self.assertGreaterEqual(row[bt_key], lo, f"{row['label']} {reg_key} below min")
                self.assertLessEqual(row[bt_key], hi, f"{row['label']} {reg_key} above max")

    def test_old_desk_gates_are_non_actionable_diagnostics(self):
        grid = backtest.default_grid()
        diag = [r for r in grid if not r.get("actionable")]
        self.assertTrue(diag, "expected replay-only diagnostics to be kept as context")
        for row in diag:
            self.assertFalse(row.get("actionable"))


class Recommend(unittest.TestCase):
    def test_adjust_when_best_beats_current(self):
        cur = _row("current", current=True, trades=10, wins=2, win_rate=0.2,
                   pnl=-5.0, dd=6.0, min_thesis_score=6.0, max_h1_change_pct=250.0,
                   max_price_gap_pct=25.0)
        best = _row("analyst score 8", trades=10, wins=7, win_rate=0.7,
                    pnl=4.0, dd=1.0, min_thesis_score=8.0, max_h1_change_pct=250.0,
                    max_price_gap_pct=25.0)
        rec = backtest.recommend({"rows": [cur, best], "thin": False})
        self.assertEqual(rec["action"], "adjust")
        self.assertEqual(len(rec["deltas"]), 1)
        d = rec["deltas"][0]
        self.assertEqual(d["setting"], "analyst.valid_score")
        self.assertEqual(d["current"], 6)
        self.assertEqual(d["proposed"], 8)
        self.assertGreater(rec["improvement"], 0)
        self.assertIn(rec["confidence"], ("medium", "high"))

    def test_hold_on_tie(self):
        cur = _row("current", current=True, trades=6, pnl=-7.68, dd=7.67,
                   min_thesis_score=6.0)
        tie = _row("analyst score 4", trades=6, pnl=-7.68, dd=7.67,
                   min_thesis_score=4.0)
        rec = backtest.recommend({"rows": [cur, tie], "thin": False})
        self.assertEqual(rec["action"], "hold")
        self.assertEqual(rec["deltas"], [])

    def test_hold_when_no_trades(self):
        cur = _row("current", current=True, trades=0, min_thesis_score=6.0)
        alt = _row("analyst score 8", trades=0, min_thesis_score=8.0)
        rec = backtest.recommend({"rows": [cur, alt], "thin": True})
        self.assertEqual(rec["action"], "hold")
        self.assertEqual(rec["deltas"], [])

    def test_hold_when_only_non_actionable(self):
        cur = _row("current", current=True, trades=5, pnl=1.0)
        diag = _row("score 55", actionable=False, trades=5, pnl=9.0, min_score=55)
        rec = backtest.recommend({"rows": [cur, diag], "thin": False})
        self.assertEqual(rec["action"], "hold")
        self.assertEqual(rec["deltas"], [])

    def test_confidence_low_on_thin_sample(self):
        cur = _row("current", current=True, trades=10, pnl=-5.0, dd=6.0,
                   min_thesis_score=6.0)
        best = _row("analyst score 8", trades=10, wins=8, win_rate=0.8,
                    pnl=5.0, dd=1.0, min_thesis_score=8.0)
        rec = backtest.recommend({"rows": [cur, best], "thin": True})
        self.assertEqual(rec["action"], "adjust")
        self.assertEqual(rec["confidence"], "low")

    def test_simulate_emits_actionable_and_bundle_flags(self):
        out = backtest.simulate({"candidates": [], "prices": {}, "t_start": 0,
                                 "t_end": 1}, {"id": "x", "label": "x",
                                               "current": True, "actionable": True,
                                               "bundle": False})
        self.assertTrue(out["actionable"])
        self.assertFalse(out["bundle"])


class ReportWiring(unittest.TestCase):
    def test_report_includes_recommendation(self):
        import json
        from datetime import datetime, timezone

        def iso(ts):
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        t0 = 1_700_000_000
        mint = "MintRec0000000000000000000000000000000001"
        tok = dict(mint=mint, symbol="REC", price_usd=0.001, mcap_usd=300_000,
                   liquidity_usd=40_000, volume_m5=5_000, volume_h1=20_000,
                   volume_h6=80_000, volume_h24=200_000, buys_h1=200, sells_h1=120,
                   buyers_h1=150, change_m5=-6, change_h1=10, age_minutes=45,
                   source="test")
        rows = [
            {"time": iso(t0), "kind": "decision", "mint": mint, "symbol": "REC",
             "token": tok, "thesis": {"verdict": "valid_setup", "score": 9},
             "scorecard": {"total": 66}, "approved": True, "reason": "approved",
             "ticket_usd": 5},
            {"time": iso(t0 + 60), "kind": "quote", "mint": mint, "symbol": "REC",
             "price_usd": 0.0007, "volume_h6": 10_000, "volume_h24": 40_000},
        ]
        d = Path(tempfile.mkdtemp())
        p = d / "journal.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows))
        rep = backtest.report(str(p))
        self.assertIn("recommendation", rep)
        self.assertIn("action", rep["recommendation"])
        self.assertTrue(all("actionable" in r for r in rep["rows"]))


class PerformanceCompleteness(unittest.TestCase):
    def test_all_rows_and_recommendation_reach_the_ai(self):
        rows = [_row(f"row {i}", trades=i, pnl=float(i)) for i in range(20)]
        rows[0]["current"] = True
        rec = {"action": "hold", "confidence": "low", "deltas": [], "evidence": "x",
               "notes": []}
        perf = dc.performance({"broker": {"closed": []}},
                              {"rows": rows, "thin": False, "hours": 6,
                               "recommendation": rec})
        bt = perf["backtest"]
        self.assertEqual(len(bt["rows"]), 20)
        self.assertTrue(all("settings" in r for r in bt["rows"]))
        self.assertEqual(bt["recommendation"]["action"], "hold")

    def test_missing_recommendation_is_tolerated(self):
        # desk_chat must not import backtest (isolation), so a report without a
        # recommendation simply has no recommendation block; it never crashes.
        cur = _row("current", current=True, trades=10, pnl=-5.0, dd=6.0)
        perf = dc.performance({"broker": {"closed": []}},
                              {"rows": [cur], "thin": False, "hours": 6})
        self.assertIn("rows", perf["backtest"])
        self.assertNotIn("recommendation", perf["backtest"])


class OfflineFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DESK_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_fallback_produces_confirmable_proposal(self):
        perf = {"backtest": {"recommendation": {
            "action": "adjust", "confidence": "medium", "evidence": "score 8 won",
            "notes": [], "deltas": [{"setting": "analyst.valid_score", "current": 6,
                                     "proposed": 8, "reason": "backtest"}]}}}
        out = dc._recommend_fallback(perf)
        self.assertIsNotNone(out)
        self.assertEqual(len(out["proposals"]), 1)
        made = st.create(out["proposals"], "test", "recommend")
        self.assertTrue(made["ok"], made)
        self.assertEqual(made["proposal"]["changes"][0]["setting"], "analyst.valid_score")

    def test_fallback_is_none_on_hold(self):
        perf = {"backtest": {"recommendation": {"action": "hold", "deltas": []}}}
        self.assertIsNone(dc._recommend_fallback(perf))


class Endpoint(unittest.TestCase):
    """The real /api/ai/recommend path: report -> fallback -> pending proposal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("DESK_DATA_DIR")
        os.environ["DESK_DATA_DIR"] = self.tmp.name
        os.environ.pop("DESK_TOKEN", None)
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                  "CEREBRAS_API_KEY", "CLOUDFLARE_API_TOKEN"):
            os.environ.pop(k, None)

    def tearDown(self):
        from desk import chat_memory
        chat_memory.reset()
        if self._prev is None:
            os.environ.pop("DESK_DATA_DIR", None)
        else:
            os.environ["DESK_DATA_DIR"] = self._prev
        sys.modules.pop("app", None)
        self.tmp.cleanup()

    def test_recommend_endpoint_creates_pending_proposal(self):
        from desk import chat_memory
        chat_memory.reset()
        sys.modules.pop("app", None)
        from fastapi.testclient import TestClient
        import app as appmod

        cur = _row("current", current=True, trades=10, wins=2, win_rate=0.2,
                   pnl=-5.0, dd=6.0, min_thesis_score=6.0, max_h1_change_pct=250.0,
                   max_price_gap_pct=25.0)
        best = _row("analyst score 8", trades=10, wins=7, win_rate=0.7,
                    pnl=4.0, dd=1.0, min_thesis_score=8.0, max_h1_change_pct=250.0,
                    max_price_gap_pct=25.0)
        report = {"rows": [cur, best], "thin": False, "hours": 6,
                  "recommendation": backtest.recommend({"rows": [cur, best]})}
        with mock.patch.object(appmod, "backtest", return_value=report):
            r = TestClient(appmod.app).post("/api/ai/recommend")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(len(body["proposals"]), 1)
        self.assertEqual(body["proposals"][0]["status"], "pending")
        self.assertEqual(body["proposals"][0]["changes"][0]["setting"],
                         "analyst.valid_score")


class KeyAliases(unittest.TestCase):
    def test_backtest_keys_map_to_registry_or_drop(self):
        self.assertEqual(dc._BT_KEY_ALIASES["min_thesis_score"], "analyst.valid_score")
        self.assertEqual(dc._BT_KEY_ALIASES["max_h1_change_pct"], "scan.max_h1_change_pct")
        self.assertEqual(dc._BT_KEY_ALIASES["max_price_gap_pct"], "entry.max_price_gap_pct")
        for replay_only in ("min_score", "max_chase", "min_alive_6h"):
            self.assertIsNone(dc._BT_KEY_ALIASES[replay_only])


if __name__ == "__main__":
    unittest.main()
