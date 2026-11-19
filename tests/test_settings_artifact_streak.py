"""Losses that close right after a settings change are artifacts, not market losses:
they must not count toward the breaker loss streak (PAPER only)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk import guard                    # noqa: E402
from desk import settings as st           # noqa: E402


def trade(sym, pnl, t):
    return {"mint": sym, "symbol": sym, "pnl_usd": pnl, "closed": t}


class GuardArtifacts(unittest.TestCase):
    def test_losses_right_after_a_change_do_not_count(self):
        change = 1_000_000.0
        closed = [trade("APY", -0.3, change + 4), trade("WISHES", -0.3, change + 8)]
        self.assertEqual(guard.loss_streak(closed, [change]), (0, None))
        b = guard.circuit_breaker(closed, 0, 50, now=change + 600, change_times=[change])
        self.assertEqual((b["state"], b["mult"]), ("ok", 1.0))

    def test_same_losses_without_change_times_still_count(self):
        change = 1_000_000.0
        closed = [trade("APY", -0.3, change + 4), trade("WISHES", -0.3, change + 8)]
        self.assertEqual(guard.loss_streak(closed)[0], 2)
        b = guard.circuit_breaker(closed, 0, 50, now=change + 600)
        self.assertEqual((b["state"], b["mult"]), ("reduce", 0.5))

    def test_real_losses_outside_the_window_still_count(self):
        change = 1_000_000.0
        closed = [trade("A", -0.3, change + 120), trade("B", -0.3, change + 300),
                  trade("C", -0.3, change + 301)]
        self.assertEqual(guard.loss_streak(closed, [change])[0], 3)
        b = guard.circuit_breaker(closed, 0, 50, now=change + 400, change_times=[change])
        self.assertEqual(b["state"], "cooldown")

    def test_trade_before_the_change_is_not_an_artifact(self):
        change = 1_000_000.0
        self.assertFalse(guard.is_settings_artifact(trade("A", -1, change - 5), [change]))
        self.assertTrue(guard.is_settings_artifact(trade("A", -1, change + 60), [change]))
        self.assertFalse(guard.is_settings_artifact(trade("A", -1, change + 61), [change]))

    def test_artifacts_are_skipped_not_streak_breakers(self):
        change = 1_000_000.0
        closed = [trade("A", -0.3, change - 50), trade("B", -0.3, change - 40),
                  trade("C", 0.5, change + 3)]          # artifact win in between
        self.assertEqual(guard.loss_streak(closed, [change])[0], 2)

    def test_flagged_row_is_excluded(self):
        c = trade("A", -0.3, 5.0)
        c["settings_artifact"] = True
        self.assertEqual(guard.loss_streak([c])[0], 0)

    def test_a_real_loss_after_artifacts_counts_once(self):
        change = 1_000_000.0
        closed = [trade("APY", -0.3, change + 4), trade("WISHES", -0.3, change + 8),
                  trade("REAL", -0.3, change + 900)]
        self.assertEqual(guard.loss_streak(closed, [change])[0], 1)
        b = guard.circuit_breaker(closed, 0, 50, now=change + 1000, change_times=[change])
        self.assertEqual(b["state"], "ok")


class SettingsChangeTimes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DESK_DATA_DIR": self.tmp.name})
        self.env.start()
        self.saved = st.current_values()
        st.reset_runtime()

    def tearDown(self):
        for k, v in self.saved.items():
            st.REGISTRY[k].set(v)
        st.reset_runtime()
        self.env.stop()
        self.tmp.cleanup()

    def test_confirm_records_epoch_time_and_survives_reload(self):
        now = time.time()
        pid = st.create_values({"breaker.streak_pause": 4})["proposal"]["id"]
        self.assertEqual(st.change_times(), [])            # pending is not a change
        st.confirm(pid, "tester")
        ts = st.change_times()
        self.assertEqual(len(ts), 1)
        self.assertLess(abs(ts[0] - now), 5)
        st.reset_runtime()                                  # reload from the persisted file
        ts2 = st.change_times()
        self.assertEqual(len(ts2), 1)
        self.assertLess(abs(ts2[0] - ts[0]), 1.5)

    def test_old_proposals_without_epoch_use_decided_at(self):
        iso = "2026-11-19T05:31:00"
        expect = time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%S"))
        st._write(st._PROPOSALS, "proposals", {"proposals": [
            {"id": "a", "status": "confirmed", "decided_at": iso},
            {"id": "b", "status": "rejected", "decided_at": "2026-11-19T06:00:00"},
            {"id": "c", "status": "confirmed", "decided_at": "garbage"}]})
        st.reset_runtime()
        self.assertEqual(st.change_times(), [expect])
        closed = [trade("APY", -0.3, expect + 4), trade("WISHES", -0.3, expect + 8)]
        self.assertEqual(guard.loss_streak(closed, st.change_times())[0], 0)


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_guard_integration import Base as AgentBase   # noqa: E402


class AgentBreaker(AgentBase):
    def _closed(self):
        change = time.time() - 600
        return change, [trade("APY", -0.1, change + 4), trade("WISHES", -0.1, change + 8)]

    def test_artifact_losses_do_not_reduce_the_live_agent(self):
        change, closed = self._closed()
        with mock.patch("desk.settings.change_times", return_value=[change]):
            a = self.run_agent(closed=closed)
        self.assertEqual(a.breaker["state"], "ok")
        self.assertEqual(a.breaker["mult"], 1.0)
        with mock.patch("desk.settings.change_times", return_value=[change]):
            rows = a.status()["broker"]["closed"]
        self.assertTrue(all(r.get("settings_artifact") for r in rows))

    def test_without_a_change_the_same_losses_still_reduce(self):
        _, closed = self._closed()
        with mock.patch("desk.settings.change_times", return_value=[]):
            a = self.run_agent(closed=closed)
        self.assertEqual(a.breaker["state"], "reduce")


if __name__ == "__main__":
    unittest.main()
