"""Continue today: the operator override for the 3% daily loss stop.

PAPER only, today only, logged. The limit itself never changes, every other risk
gate still fires, and LIVE can never use it.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import PAPER, RISK                                            # noqa: E402
from desk import analyst                                                  # noqa: E402
from desk.risk import (DAILY_LOSS_REASON, Account, check_live_trade,     # noqa: E402
                       check_token_trade, daily_override_active)
from desk.solana_agent import SolanaAgent                                 # noqa: E402
from legacy_book import legacy_book  # noqa: E402

NOON = datetime(2026, 10, 26, 12, 0)
EQ = PAPER["starting_equity_usd"]
TODAY = str(date.today())
YESTERDAY = str(date.today() - timedelta(days=1))


def thesis(**kw):
    base = dict(verdict="valid_setup", score=8, red_flags=[], green_flags=["crowd"],
                shape="crowd", invalidation="buyers stall", sources=["buyers_h1"])
    base.update(kw)
    return analyst.TokenThesis(**base)


def down_account(open_positions=0):
    return Account(equity=EQ, day_pnl=-(RISK["max_daily_loss_pct"] * EQ + 0.01),
                   open_positions=open_positions, realized_equity=EQ)


@legacy_book
class OverrideRule(unittest.TestCase):
    def test_limit_is_still_three_percent(self):
        self.assertEqual(RISK["max_daily_loss_pct"], 0.03)

    def test_without_override_daily_stop_blocks(self):
        ok, _, reason = check_token_trade(thesis(), down_account(), 30_000, now=NOON)
        self.assertFalse(ok)
        self.assertEqual(reason, DAILY_LOSS_REASON)

    def test_override_bypasses_only_the_daily_stop(self):
        ok, ticket, reason = check_token_trade(thesis(), down_account(), 30_000, now=NOON,
                                               daily_override=True)
        self.assertTrue(ok, reason)
        self.assertGreater(ticket, 0)

    def test_other_checks_still_fire_under_override(self):
        acct = down_account()
        self.assertIn("verdict", check_token_trade(thesis(verdict="watch"), acct, 30_000,
                                                   now=NOON, daily_override=True)[2])
        self.assertIn("score", check_token_trade(thesis(score=4), acct, 30_000,
                                                 now=NOON, daily_override=True)[2])
        full = down_account(open_positions=RISK["max_open_positions"])
        self.assertEqual(check_token_trade(thesis(), full, 30_000, now=NOON,
                                           daily_override=True)[2], "max open positions")
        self.assertIn("fee floor", check_token_trade(thesis(), acct, 50, now=NOON,
                                                     daily_override=True)[2])
        with mock.patch.dict("config.RISK", {"quiet_hours": (1, 7)}):
            night = datetime(2026, 10, 26, 2, 30)
            self.assertIn("quiet", check_token_trade(thesis(), acct, 30_000, now=night,
                                                     daily_override=True)[2])

    def test_active_only_in_paper_and_only_today(self):
        self.assertTrue(daily_override_active("paper", TODAY, TODAY))
        self.assertFalse(daily_override_active("live", TODAY, TODAY))
        self.assertFalse(daily_override_active("LIVE", TODAY, TODAY))
        self.assertFalse(daily_override_active("paper", YESTERDAY, TODAY))
        self.assertFalse(daily_override_active("paper", None, TODAY))

    def test_live_check_has_no_override(self):
        import inspect
        self.assertNotIn("daily_override", inspect.signature(check_live_trade).parameters)
        caps = {"max_trade": 10, "max_notional": 50, "daily_loss_usd": 5, "deployed": 0}
        ok, _, reason = check_live_trade(thesis(), -6, 0, 30_000, caps, now=NOON)
        self.assertFalse(ok)
        self.assertIn("live daily loss", reason)


class AgentContinue(unittest.TestCase):
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

    def lose(self, usd=1.0):
        """Push today's paper P&L past -3% of a $20 book."""
        self.a.paper.s["day_start"] = self.a.paper.equity() + usd
        self.a.paper._save()

    def test_refuses_when_not_halted(self):
        r = self.a.continue_today()
        self.assertFalse(r["ok"])
        self.assertIn("not active", r["error"])
        self.assertNotIn("daily_loss_override_date", self.a.paper.s)

    def test_refuses_in_live(self):
        self.lose()
        self.a.mode = "live"
        r = self.a.continue_today()
        self.assertFalse(r["ok"])
        self.assertIn("PAPER only", r["error"])
        self.assertNotIn("daily_loss_override_date", self.a.paper.s)
        self.assertFalse(self.a.daily_override_today())

    def test_continue_sets_today_logs_and_persists(self):
        self.lose()
        st = self.a.risk_status()
        self.assertTrue(st["daily_loss_halted"]); self.assertTrue(st["can_continue"])
        self.assertTrue(st["blocking_entries"]); self.assertFalse(st["override_today"])
        r = self.a.continue_today()
        self.assertTrue(r["ok"], r)
        self.assertTrue(self.a.daily_override_today())
        st = r["risk"]
        self.assertTrue(st["override_today"]); self.assertFalse(st["blocking_entries"])
        self.assertFalse(st["can_continue"])
        # logged with a timestamp and the day P&L
        rows = [json.loads(l) for l in (self.d / "journal.jsonl").read_text().splitlines()]
        ov = [x for x in rows if x["kind"] == "risk_override"]
        self.assertEqual(len(ov), 1)
        self.assertEqual(ov[0]["day"], TODAY)
        self.assertLess(ov[0]["day_pnl"], 0)
        self.assertIn("time", ov[0])
        # survives a restart the same day
        again = SolanaAgent(ledger=self.d / "p.json", bench_path=self.d / "b2.db",
                            live_ledger=self.d / "l.json")
        self.assertTrue(again.daily_override_today())
        # second tap is idempotent, not a second log row
        self.assertTrue(self.a.continue_today().get("already"))
        # the limit did not move
        self.assertEqual(RISK["max_daily_loss_pct"], 0.03)

    def test_override_ignored_in_live_even_if_stored(self):
        self.lose()
        self.assertTrue(self.a.continue_today()["ok"])
        self.a.mode = "live"
        self.assertFalse(self.a.daily_override_today())
        self.assertFalse(self.a.risk_status()["override_today"])

    def test_override_expires_at_day_roll(self):
        self.lose()
        self.assertTrue(self.a.continue_today()["ok"])
        self.a.paper.s["daily_loss_override_date"] = YESTERDAY   # as seen tomorrow
        self.assertFalse(self.a.daily_override_today())
        self.a.paper.s["day"] = YESTERDAY                      # the book rolls too
        st = self.a.risk_status()
        self.assertEqual(st["day"], TODAY)
        self.assertFalse(st["override_today"])
        self.assertFalse(st["daily_loss_halted"])              # fresh day, stop re-armed

    def test_state_exposes_risk_fields(self):
        snap = self.a.status()
        self.assertIn("risk", snap)
        for k in ("daily_loss_halted", "override_today", "can_continue", "daily_loss_limit_pct"):
            self.assertIn(k, snap["risk"])


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        env = {"DESK_DATA_DIR": str(d), "DESK_OFFLINE": "1", "PAPER_AUTOSTART": "0"}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        for k in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
            os.environ.pop(k, None)
        try:
            import app as desk_app
        except ImportError as e:  # pragma: no cover
            self.skipTest(f"app deps missing: {e}")
        self.app = desk_app
        self.agent = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db",
                                 live_ledger=d / "l.json")
        self.patch = mock.patch.object(desk_app, "agent", self.agent)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def call(self):
        r = self.app.risk_continue()
        return r.status_code, json.loads(r.body)

    def test_route_is_registered_and_gated(self):
        paths = {getattr(r, "path", None) for r in self.app.app.routes}
        self.assertIn("/api/risk/continue", paths)
        self.assertNotIn("/api/risk/continue", self.app.PUBLIC_PATHS)

    def test_refuses_when_not_halted(self):
        code, body = self.call()
        self.assertEqual(code, 409); self.assertFalse(body["ok"])

    def test_refuses_when_not_paper(self):
        self.agent.paper.s["day_start"] = self.agent.paper.equity() + 1
        self.agent.mode = "live"
        code, body = self.call()
        self.assertEqual(code, 409); self.assertIn("PAPER only", body["error"])

    def test_accepts_when_halted_in_paper(self):
        self.agent.paper.s["day_start"] = self.agent.paper.equity() + 1
        code, body = self.call()
        self.assertEqual(code, 200); self.assertTrue(body["risk"]["override_today"])


if __name__ == "__main__":
    unittest.main()
