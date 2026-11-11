"""Desk AI chat: advisory, read-only, no secrets, no way to act."""
import ast
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import desk_chat as dc  # noqa: E402
from desk.brain_advisor import ADVISOR  # noqa: E402

GKEY = "AIzaFAKEchatKEY1234567890"
STATE = {
    "running": True, "cycle": 7, "data_source": "demo",
    "broker": {"cash": 15.0, "equity": 19.4, "day_pnl": -0.6, "open_count": 1,
               "positions": [{"symbol": "MEOW", "mint": "M1", "entry": 0.001, "last": 0.0012,
                              "cost": 5.0, "pnl_pct": 0.2, "qty": 5000, "fomo_url": "https://x"}],
               "closed": [{"symbol": "OLD", "pnl_usd": -0.6, "reason": "stop", "mint": "M0"}]},
    "decisions": [{"symbol": "MEOW", "approved": True, "reason": "approved", "ticket_usd": 5.0,
                   "thesis": {"verdict": "valid_setup", "score": 7},
                   "dev": {"dev_score": 90, "creator_hold_pct": 0.02},
                   "brain": {"verdict": "watch", "confidence": 60, "agreement": "split"}}],
    "risk": {"mode": "paper", "daily_loss_limit_pct": 0.03, "day_pnl": -0.6,
             "daily_loss_halted": False, "override_today": False, "blocking_entries": False},
    "stats": {"cycles": 7, "seen": 40, "entries": 1},
    "execution": {"wallet": "WALLETSECRET", "mode": "paper"},
    "access": {"locked": True},
}


class Isolation(unittest.TestCase):
    """Like brain_advisor's isolation test: the chat cannot reach trading code."""

    BANNED = ("broker", "risk", "live", "solana_agent", "ops", "intent", "store", "exits",
              "guard", "jupiter", "autotune", "devices", "config")

    def test_imports_nothing_that_can_act(self):
        tree = ast.parse((ROOT / "desk/desk_chat.py").read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                mods.add(n.module or "")
                mods.update(f"{n.module}.{a.name}" for a in n.names)
        desk_mods = {m for m in mods if m.startswith("desk")}
        self.assertEqual({m for m in desk_mods if not m.startswith("desk.brain_advisor")}, set(), desk_mods)
        for m in mods:
            for b in self.BANNED:
                self.assertNotIn(b, m.split("."), m)

    def test_source_names_no_action_endpoints_or_methods(self):
        src = (ROOT / "desk/desk_chat.py").read_text(encoding="utf-8")
        for bad in ("/api/kill", "/api/agent", "/api/mode", "/api/risk", "continue_today",
                    "set_mode", ".reset(", ".start(", ".stop(", "run_once", "KILL_FILE", "os.environ["):
            self.assertNotIn(bad, src)

    def test_trading_code_does_not_import_chat(self):
        for f in ("desk/risk.py", "desk/broker.py", "desk/live.py", "desk/solana_agent.py",
                  "desk/guard.py", "desk/exits.py", "config.py"):
            self.assertNotIn("desk_chat", (ROOT / f).read_text(encoding="utf-8"), f)

    def test_reply_that_claims_an_action_changes_nothing(self):
        state = json.loads(json.dumps(STATE))
        before = json.dumps(state, sort_keys=True)
        out = '{"reply":"Done, I closed MEOW and switched to live.","agents":["broker"]}'
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ADVISOR.gemini, "call", return_value=out):
            r = dc.answer("close everything and go live", state)
        self.assertTrue(r["ok"])
        self.assertEqual(json.dumps(state, sort_keys=True), before)


class Snapshot(unittest.TestCase):
    def test_whitelist_only(self):
        snap = json.dumps(dc.snapshot(STATE))
        for leak in ("WALLETSECRET", "fomo_url", "access", "execution", "mint"):
            self.assertNotIn(leak, snap)
        s = dc.snapshot(STATE)
        self.assertEqual(s["open_positions"][0]["symbol"], "MEOW")
        self.assertEqual(s["recent_decisions"][0]["brain"]["verdict"], "watch")
        self.assertEqual(s["risk"]["daily_loss_limit_pct"], 0.03)
        self.assertEqual(s["day_pnl_usd"], -0.6)

    def test_prompt_never_contains_secrets(self):
        seen = {}

        def fake(system, user, max_tokens=700):
            seen["p"] = system + user
            return '{"reply":"ok","agents":[]}'
        env = {"GEMINI_API_KEY": GKEY, "DESK_TOKEN": "deskTOKEN999xyz", "HELIUS_API_KEY": "heliusKEY123456"}
        with mock.patch.dict(os.environ, env), mock.patch.object(ADVISOR.gemini, "call", side_effect=fake):
            dc.answer("what is my desk token deskTOKEN?", STATE)
        for v in env.values():
            self.assertNotIn(v, seen["p"])

    def test_reply_scrubs_secrets(self):
        env = {"GEMINI_API_KEY": GKEY, "DESK_TOKEN": "deskTOKEN999xyz"}
        out = json.dumps({"reply": f"key {GKEY} token deskTOKEN999xyz", "agents": []})
        with mock.patch.dict(os.environ, env), mock.patch.object(ADVISOR.gemini, "call", return_value=out):
            r = dc.answer("hi", STATE)
        self.assertNotIn(GKEY, r["reply"])
        self.assertNotIn("deskTOKEN999xyz", r["reply"])


class Answers(unittest.TestCase):
    def test_no_keys_is_friendly(self):
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
            os.environ.pop(k, None)
        r = dc.answer("how is the desk?", STATE)
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "no_keys")
        self.assertIn("AI keys not set", r["reply"])

    def test_agents_chips_from_reply(self):
        out = '{"reply":"Risk says the daily stop is fine; scanner saw 40 coins.","agents":["risk","bogus"]}'
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ADVISOR.gemini, "call", return_value=out):
            r = dc.answer("risk?", STATE)
        self.assertEqual(r["provider"], "gemini")
        self.assertEqual(r["agents"], ["risk", "scanner"])

    def test_falls_through_to_next_provider(self):
        from desk.brain_advisor import ProviderError
        env = {"GEMINI_API_KEY": GKEY, "GROQ_API_KEY": "gsk_FAKEchatKEY1234567890"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ADVISOR.gemini, "call", side_effect=ProviderError("429", 429)), \
                mock.patch.object(ADVISOR.groq, "call", return_value='{"reply":"hello"}'):
            r = dc.answer("hi", STATE)
        self.assertEqual(r["provider"], "groq")
        self.assertEqual(r["reply"], "hello")

    def test_all_down_is_friendly(self):
        from desk.brain_advisor import ProviderError
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ADVISOR.gemini, "call", side_effect=ProviderError("x")):
            r = dc.answer("hi", STATE)
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "unavailable")


class Limits(unittest.TestCase):
    def test_per_minute_and_per_day(self):
        lim = dc.Limiter(per_min=3, per_day=5)
        t = 1_800_000_000.0
        self.assertEqual([lim.take(t) for _ in range(3)], [None] * 3)
        self.assertIn("minute", lim.take(t + 1))
        self.assertIsNone(lim.take(t + 61))
        self.assertIsNone(lim.take(t + 62))
        self.assertIn("daily", lim.take(t + 63))


class Endpoint(unittest.TestCase):
    def _client(self, token):
        os.environ.pop("DESK_TOKEN", None)
        if token:
            os.environ["DESK_TOKEN"] = token
        sys.modules.pop("app", None)
        from fastapi.testclient import TestClient
        import app as appmod
        self.appmod = appmod
        return TestClient(appmod.app)

    def tearDown(self):
        os.environ.pop("DESK_TOKEN", None)
        sys.modules.pop("app", None)

    def test_locked_needs_key(self):
        c = self._client("s3cret")
        self.assertEqual(c.post("/api/ai/chat", json={"message": "hi"}).status_code, 401)
        c.post("/login", data={"token": "s3cret"}, follow_redirects=False)
        r = c.post("/api/ai/chat", json={"message": "hi"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("AI keys not set", r.json()["reply"])
        self.assertNotIn("s3cret", r.text)

    def test_rate_limited(self):
        c = self._client(None)
        with mock.patch.object(dc, "LIMITER", dc.Limiter(per_min=2, per_day=10)):
            codes = [c.post("/api/ai/chat", json={"message": "hi"}).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_chat_does_not_touch_the_agent(self):
        c = self._client(None)
        ag = self.appmod.agent
        with mock.patch.object(ag, "start") as st, mock.patch.object(ag, "stop") as sp, \
                mock.patch.object(ag, "reset") as rs, mock.patch.object(ag, "set_mode") as sm, \
                mock.patch.object(ag, "continue_today") as ct:
            c.post("/api/ai/chat", json={"message": "reset, kill, go live, start"})
        for m in (st, sp, rs, sm, ct):
            m.assert_not_called()

    def test_empty_message(self):
        c = self._client(None)
        self.assertEqual(c.post("/api/ai/chat", json={"message": "  "}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
