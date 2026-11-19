"""Control Tower: a cursor log in front of the paper loop, not a second broker."""
from __future__ import annotations

import time
import unittest
from pathlib import Path

from desk import tower
from desk.solana_agent import SolanaAgent
from desk.tower import PRIORITY, Tower


class CursorLog(unittest.TestCase):
    def test_serial_clicks_never_hold_or_collide(self):
        t = Tower()
        t.click("scanner", "open new pairs", symbol="OGTA")
        t.click("analyst", "valid_setup 8/10", symbol="OGTA")
        t.click("brain", "size $12.00", symbol="OGTA", usd=12, ok=True, mint="M")
        t.click("executor", "click Buy $12.00", symbol="OGTA", usd=12, mint="M")
        view = t.snapshot(equity=50, day_pnl=0, start_equity=50, positions=[],
                          running=False, started_at=time.time(), mode="paper", cycle=1)
        self.assertEqual(view["clicks"], 4)
        self.assertEqual(view["holds"], 0)
        self.assertEqual(view["collisions"], 0)
        self.assertTrue(view["paper"])
        self.assertFalse(view["signing"])
        self.assertFalse(view["demo"])
        self.assertEqual(view["queue"][0]["id"], "SCN-001")
        self.assertEqual(view["queue"][-1]["role"], "executor")

    def test_risk_patrol_serializes_waiters_without_a_collision(self):
        t = Tower()
        try:
            with t.holding("risk", "guards 1 stop", symbol="WASTED"):
                t.click("analyst", "holders read", symbol="WASTED")
                t.click("executor", "click Buy $36.21", symbol="WASTED", usd=36.21)
                raise RuntimeError("seat blew up")
        except RuntimeError:
            pass
        self.assertIsNone(t.holder)
        self.assertEqual(t.collisions, 0)
        self.assertEqual(t.holds["analyst"], 1)
        self.assertEqual(t.holds["executor"], 1)
        self.assertGreaterEqual(t.risk_first, 1)
        texts = [row["text"] for row in t.readback]
        self.assertTrue(any(text.startswith("✗ CONFLICT") for text in texts))
        # Priority order, not whoever asked last.
        conflict = next(text for text in texts if text.startswith("✗ CONFLICT"))
        self.assertLess(conflict.index("Executor"), conflict.index("Analyst"))
        self.assertEqual(PRIORITY[0], "risk")

    def test_a_second_grant_counts_a_collision_and_is_refused(self):
        t = Tower()
        t._set_open(2)
        self.assertEqual(t.collisions, 1)
        self.assertEqual(t._open, 1)

    def test_risk_veto_after_a_sized_buy_is_risk_first(self):
        t = Tower()
        t.click("brain", "size $10.00", symbol="WIF", mint="Mint", usd=10, ok=True)
        t.click("risk", "cut score", symbol="WIF", mint="Mint")
        self.assertGreaterEqual(t.risk_first, 1)
        self.assertNotIn("Mint", t.sized)

    def test_observe_maps_the_loop_events(self):
        t = Tower()
        t.observe({"kind": "scan", "source": "geckoterminal", "message": "cycle"})
        t.observe({"kind": "thesis", "symbol": "WIF", "mint": "M", "verdict": "valid_setup", "score": 8})
        t.observe({"kind": "entry", "symbol": "WIF", "mint": "M", "ticket_usd": 12.5})
        t.observe({"kind": "exit", "symbol": "WIF", "mint": "M", "fraction": 0.5, "pnl_usd": 1.2})
        t.observe({"kind": "reject", "stage": "scanner", "symbol": "DUST", "reason": "liquidity"})
        self.assertEqual(t.clicks["scanner"], 2)
        self.assertEqual(t.clicks["analyst"], 1)
        self.assertEqual(t.clicks["executor"], 2)
        self.assertEqual(t.collisions, 0)
        # A junk event must not raise.
        t.observe({"kind": "dialogue", "message": "seats talked"})

    def test_holder_bars_come_from_chain_fractions(self):
        t = Tower()
        t.click("brain", "size $4", symbol="WIF", mint="M", ok=True,
                mcap=250_000, top_wallet=0.08, top_10=0.31, holders=400)
        view = t.snapshot(equity=50, day_pnl=0, start_equity=50, positions=[],
                          running=True, started_at=time.time(), mode="paper", cycle=2)
        bars = view["token"]["holders"]
        self.assertEqual([b["label"] for b in bars], ["top wallet", "next 9", "the rest"])
        self.assertAlmostEqual(bars[0]["pct"], 8.0, places=1)
        self.assertAlmostEqual(bars[1]["pct"], 23.0, places=1)
        self.assertAlmostEqual(sum(b["pct"] for b in bars), 100.0, places=1)

    def test_a_window_that_just_landed_stays_landed(self):
        now = 1_800_000_000.0
        start, end = tower._window(now - tower.WATCH_S, now)
        self.assertAlmostEqual(end, now, places=2)
        self.assertAlmostEqual(start, now - tower.WATCH_S, places=2)
        # A second into the next watch rolls forward.
        start2, end2 = tower._window(now - tower.WATCH_S - 5, now)
        self.assertAlmostEqual(start2, now - 5, places=2)
        self.assertGreater(end2, now)

    def test_demo_is_off_until_asked_and_does_not_replace_the_book(self):
        t = Tower()
        self.assertFalse(t.demo_on)
        live = t.snapshot(equity=42, day_pnl=1, start_equity=40, positions=[],
                          running=False, started_at=time.time(), mode="paper", cycle=0)
        self.assertFalse(live["demo"])
        self.assertEqual(live["pnl"]["equity"], 42)
        t.set_demo(True)
        # Land the sample night.
        sample = t.snapshot(equity=42, day_pnl=1, start_equity=40, positions=[],
                            running=False, started_at=time.time(), mode="paper", cycle=0,
                            now=t.demo_started + tower.DEMO_DURATION + 1)
        self.assertTrue(sample["demo"])
        self.assertIn("sample night", sample["demo_label"])
        self.assertEqual(sample["live_equity"], 42)
        self.assertEqual(sample["pnl"]["equity"], tower.DEMO_END_EQUITY)
        self.assertEqual(sample["collisions"], 0)
        self.assertGreater(sample["holds"], 0)
        self.assertGreater(sample["risk_first"], 0)
        self.assertTrue(any(row["text"].startswith("✗ CONFLICT") for row in sample["readback"]))
        self.assertEqual(sample["token"]["symbol"], "WASTED")
        self.assertIsNotNone(sample["token"]["holders"])
        self.assertGreater(sample["token"]["holders"][0]["pct"], 0)
        t.set_demo(False)
        again = t.snapshot(equity=42, day_pnl=1, start_equity=40, positions=[],
                           running=False, started_at=time.time(), mode="paper", cycle=0)
        self.assertFalse(again["demo"])
        self.assertEqual(again["pnl"]["equity"], 42)


class AgentFeed(unittest.TestCase):
    def setUp(self):
        tower.reset()
        tower.set_demo(False)
        self.dir = Path(self.id().replace(".", "_"))
        # ledgers go to the suite's temp data dir via explicit paths
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.agent = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db",
                                 live_ledger=d / "l.json")

    def tearDown(self):
        tower.reset()
        tower.set_demo(False)
        self.tmp.cleanup()

    def test_emit_lands_in_the_tower_without_leaving_paper(self):
        self.agent.emit("scan", "cycle 1: 3 pools", source="geckoterminal", count=3)
        self.agent.emit("entry", "PAPER BUY $WIF — ticket $12.50",
                        symbol="WIF", mint="Mint111", ticket_usd=12.5, qty=1, fill=0.1)
        view = self.agent.tower_view()
        self.assertEqual(view["mode"], "paper")
        self.assertFalse(view["signing"])
        self.assertFalse(view["demo"])
        self.assertGreaterEqual(view["clicks"], 2)
        self.assertEqual(view["collisions"], 0)
        roles = {row["role"] for row in view["queue"]}
        self.assertIn("scanner", roles)
        self.assertIn("executor", roles)

    def test_reset_clears_the_feed(self):
        self.agent.emit("scan", "cycle 1", source="demo", count=1)
        self.assertGreaterEqual(self.agent.tower_view()["clicks"], 1)
        self.agent.reset()
        self.assertEqual(self.agent.tower_view()["clicks"], 0)


class TowerRoute(unittest.TestCase):
    def setUp(self):
        tower.reset()
        tower.set_demo(False)

    def tearDown(self):
        tower.reset()
        tower.set_demo(False)

    def test_route_is_paper_and_demo_is_opt_in(self):
        from fastapi.testclient import TestClient
        import app as app_mod
        client = TestClient(app_mod.app)
        r = client.get("/api/tower")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r.headers.get("cache-control", ""))
        body = r.json()
        self.assertTrue(body["paper"])
        self.assertFalse(body["signing"])
        self.assertFalse(body["demo"])
        self.assertEqual(body["collisions"], 0)
        on = client.post("/api/tower/demo", json={"enabled": True})
        self.assertTrue(on.json()["demo"])
        shown = client.get("/api/tower").json()
        self.assertTrue(shown["demo"])
        self.assertEqual(shown["live_equity"], body["pnl"]["equity"])
        off = client.post("/api/tower/demo", json={"enabled": False})
        self.assertFalse(off.json()["demo"])
        shell = client.get("/")
        self.assertEqual(shell.status_code, 200)
        self.assertIn('data-tab="tower"', shell.text)
        self.assertIn("manifest.webmanifest", shell.text)


if __name__ == "__main__":
    unittest.main()
