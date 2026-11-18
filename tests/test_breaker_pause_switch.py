"""On/off switch for the loss-streak breaker pause (PAPER setting breaker.cooldown_enabled)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk import desk_chat as dc          # noqa: E402
from desk import guard                    # noqa: E402
from desk import settings as st           # noqa: E402

KEY = "breaker.cooldown_enabled"


def losses(n, now, ago=60):
    return [{"mint": f"M{i}", "pnl_usd": -0.3, "closed": now - ago} for i in range(n)]


class Base(unittest.TestCase):
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


class GuardSwitch(Base):
    def test_default_is_on_and_pauses(self):
        self.assertTrue(guard.BREAKER["cooldown_enabled"])
        now = time.time()
        self.assertEqual(guard.circuit_breaker(losses(3, now), 0, 100, now=now)["state"], "cooldown")

    def test_off_skips_only_the_pause(self):
        now = time.time()
        guard.BREAKER["cooldown_enabled"] = False
        b = guard.circuit_breaker(losses(3, now), 0, 100, now=now)
        self.assertEqual(b["state"], "reduce")          # still half size from the streak
        self.assertEqual(b["mult"], 0.5)
        self.assertEqual(guard.circuit_breaker(losses(1, now), 0, 100, now=now)["state"], "ok")

    def test_off_keeps_day_loss_half_size(self):
        now = time.time()
        guard.BREAKER["cooldown_enabled"] = False
        b = guard.circuit_breaker(losses(1, now), -5.0, 100, now=now)
        self.assertEqual((b["state"], b["mult"]), ("reduce", 0.5))

    def test_explicit_argument_overrides_setting(self):
        now = time.time()
        guard.BREAKER["cooldown_enabled"] = False
        self.assertEqual(guard.circuit_breaker(losses(3, now), 0, 100, now=now,
                                               pause_enabled=True)["state"], "cooldown")

    def test_cooldown_length_unchanged_when_on(self):
        now = time.time()
        guard.BREAKER["cooldown_s"] = 15 * 60
        self.assertEqual(guard.circuit_breaker(losses(3, now, ago=14 * 60), 0, 100, now=now)["state"], "cooldown")
        self.assertEqual(guard.circuit_breaker(losses(3, now, ago=16 * 60), 0, 100, now=now)["state"], "reduce")

    def test_live_mode_always_keeps_the_pause(self):
        from desk.solana_agent import SolanaAgent
        d = Path(tempfile.mkdtemp())
        a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        now = time.time()
        a.paper.s["closed"] = losses(3, now, ago=10)
        guard.BREAKER["cooldown_enabled"] = False
        a.mode = "live"
        with mock.patch("desk.solana_agent.scanner.new_tokens", return_value=[]):
            a.run_once()
        self.assertEqual(a.breaker["state"], "cooldown")
        a.mode = "paper"
        with mock.patch("desk.solana_agent.scanner.new_tokens", return_value=[]):
            a.run_once()
        self.assertNotEqual(a.breaker["state"], "cooldown")


class Registry(Base):
    def test_setting_is_a_public_bool_defaulting_on(self):
        s = st.REGISTRY[KEY]
        self.assertEqual(s.kind, "bool")
        self.assertFalse(s.owner_only)
        self.assertIs(s.get(), True)

    def test_15_minutes_is_inside_bounds_and_60_untouched(self):
        s = st.REGISTRY["breaker.cooldown_minutes"]
        self.assertTrue(s.lo <= 15 <= s.hi)
        self.assertEqual(s.coerce(15)[0], 15)

    def test_proposal_does_not_apply_until_confirm(self):
        made = st.create_values({KEY: False}, source="desk_ai")
        self.assertTrue(made["ok"], made)
        self.assertIs(guard.BREAKER["cooldown_enabled"], True)
        pid = made["proposal"]["id"]
        self.assertEqual(st.reject(pid, "owner")["proposal"]["status"], "rejected")
        self.assertIs(guard.BREAKER["cooldown_enabled"], True)

    def test_confirm_turns_it_off_then_on(self):
        pid = st.create_values({KEY: False}, source="desk_ai")["proposal"]["id"]
        self.assertTrue(st.confirm(pid, "owner")["ok"])
        self.assertIs(guard.BREAKER["cooldown_enabled"], False)
        pid = st.create_values({KEY: True}, source="desk_ai")["proposal"]["id"]
        self.assertTrue(st.confirm(pid, "owner")["ok"])
        self.assertIs(guard.BREAKER["cooldown_enabled"], True)

    def test_confirmed_off_survives_restart(self):
        pid = st.create_values({KEY: False}, source="desk_ai")["proposal"]["id"]
        st.confirm(pid, "owner")
        guard.BREAKER["cooldown_enabled"] = True        # as if the process restarted
        st.reset_runtime()
        st.apply_saved()
        self.assertIs(guard.BREAKER["cooldown_enabled"], False)

    def test_already_on_is_a_noop(self):
        self.assertFalse(st.create_values({KEY: True}, source="desk_ai")["ok"])

    def test_ai_prompt_lists_the_switch(self):
        self.assertIn([KEY, True, "bool"], st.compact_for_ai())


class ChatCommand(unittest.TestCase):
    def test_parse_on_off(self):
        for q, want in {"turn breaker pause off": False, "Turn breaker pause ON": True,
                        "switch the breaker pause off": False, "disable breaker pause": False,
                        "enable the breaker pause": True, "breaker pause on": True,
                        "turn off the loss streak pause": False, "set breaker pause on": True,
                        "turn the loss-streak pause on please": True}.items():
            self.assertIs(dc.breaker_pause_command(q), want, q)

    def test_other_breaker_questions_are_not_commands(self):
        for q in ("What's the breaker status?", "set breaker pause length to 30",
                  "why is the pause so long", "how is my pnl"):
            self.assertIsNone(dc.breaker_pause_command(q), q)

    def test_recommend_adds_15_minutes_pause_on(self):
        out = dc.add_breaker_recommendation({"reply": "hold", "proposals": [
            {"setting": "breaker.cooldown_minutes", "proposed": 90, "reason": "x"},
            {"setting": "risk.max_open_positions", "proposed": 4, "reason": "y"}]})
        by = {p["setting"]: p["proposed"] for p in out["proposals"]}
        self.assertEqual(by["breaker.cooldown_minutes"], 15)
        self.assertIs(by[KEY], True)
        self.assertEqual(by["risk.max_open_positions"], 4)
        self.assertEqual(len(out["proposals"]), 3)
        self.assertIn("15 min", out["reply"])


class Endpoints(Base):
    def _client(self):
        os.environ.pop("DESK_TOKEN", None)
        sys.modules.pop("app", None)
        from fastapi.testclient import TestClient
        import app as appmod
        from desk import chat_memory
        chat_memory.reset()
        return TestClient(appmod.app), appmod

    def test_chat_command_makes_pending_proposal_only(self):
        c, _ = self._client()
        r = c.post("/api/ai/chat", json={"message": "turn breaker pause off"})
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()["proposals"][0]
        self.assertEqual(p["status"], "pending")
        self.assertEqual(p["changes"][0]["setting"], KEY)
        self.assertIs(p["changes"][0]["proposed"], False)
        self.assertIs(guard.BREAKER["cooldown_enabled"], True)
        r = c.post(f"/api/proposals/{p['id']}/confirm")
        self.assertTrue(r.json()["ok"], r.text)
        self.assertIs(guard.BREAKER["cooldown_enabled"], False)
        again = c.post("/api/ai/chat", json={"message": "turn breaker pause off"}).json()
        self.assertEqual(again["proposals"], [])
        self.assertIn("already off", again["reply"])

    def test_ui_toggle_proposal_path(self):
        c, _ = self._client()
        r = c.post("/api/proposals", json={"values": {KEY: "false"}, "title": KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["proposal"]["status"], "pending")
        self.assertIs(guard.BREAKER["cooldown_enabled"], True)
        groups = c.get("/api/settings").json()["groups"]
        row = [x for g in groups.values() for x in g if x["key"] == KEY][0]
        self.assertEqual((row["type"], row["current"]), ("bool", True))

    def test_recommend_suggests_15_with_pause_on(self):
        c, appmod = self._client()
        st.REGISTRY["breaker.cooldown_minutes"].set(60)
        with mock.patch.object(appmod, "backtest", return_value={"rows": [], "thin": True, "hours": 6}):
            r = c.post("/api/ai/recommend")
        self.assertEqual(r.status_code, 200, r.text)
        ch = {x["setting"]: x["proposed"] for x in r.json()["proposals"][0]["changes"]}
        self.assertEqual(ch["breaker.cooldown_minutes"], 15)
        self.assertEqual(guard.BREAKER["cooldown_s"], 60 * 60)   # nothing applied


if __name__ == "__main__":
    unittest.main()
