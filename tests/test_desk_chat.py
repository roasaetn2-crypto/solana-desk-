"""Desk AI chat: advisory, read-only, no secrets, no way to act."""
import ast
import json
import os
import sys
import tempfile
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
              "guard", "jupiter", "devices", "config")

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
        r = dc.answer("hello there", STATE)
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
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # remember the suite-wide data dir (tests/isolate_env.py) so tearDown can put it
        # back; popping it outright sent every later test's journal into the repo root
        self._prev_data_dir = os.environ.get("DESK_DATA_DIR")
        os.environ["DESK_DATA_DIR"] = self.tmp.name
        from desk import chat_memory
        chat_memory.reset()

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
        from desk import chat_memory
        chat_memory.reset()
        os.environ.pop("DESK_TOKEN", None)
        if self._prev_data_dir is None:
            os.environ.pop("DESK_DATA_DIR", None)
        else:
            os.environ["DESK_DATA_DIR"] = self._prev_data_dir
        sys.modules.pop("app", None)
        self.tmp.cleanup()

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

    def test_history_and_clear(self):
        c = self._client(None)
        r = c.get("/api/ai/history")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["messages"], [])
        r = c.post("/api/ai/chat", json={"message": "today's P&L"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("P&L", r.json()["reply"])
        hist = c.get("/api/ai/history").json()["messages"]
        self.assertGreaterEqual(len(hist), 2)
        self.assertEqual(hist[0]["role"], "user")
        self.assertEqual(hist[0]["text"], "today's P&L")
        self.assertEqual(hist[1]["role"], "ai")
        self.assertTrue(hist[1]["text"])
        clr = c.post("/api/ai/chat/clear")
        self.assertEqual(clr.status_code, 200)
        self.assertEqual(c.get("/api/ai/history").json()["messages"], [])

    def test_history_locked_needs_key(self):
        c = self._client("s3cret")
        self.assertEqual(c.get("/api/ai/history").status_code, 401)
        self.assertEqual(c.post("/api/ai/chat/clear").status_code, 401)
        c.post("/login", data={"token": "s3cret"}, follow_redirects=False)
        self.assertEqual(c.get("/api/ai/history").status_code, 200)
        self.assertEqual(c.post("/api/ai/chat/clear").status_code, 200)

    def test_clear_does_not_touch_the_agent(self):
        c = self._client(None)
        ag = self.appmod.agent
        with mock.patch.object(ag, "start") as st, mock.patch.object(ag, "stop") as sp, \
                mock.patch.object(ag, "reset") as rs, mock.patch.object(ag, "set_mode") as sm, \
                mock.patch.object(ag, "continue_today") as ct:
            c.post("/api/ai/chat/clear")
        for m in (st, sp, rs, sm, ct):
            m.assert_not_called()

    def test_seed_only_when_empty(self):
        c = self._client(None)
        payload = {"messages": [
            {"role": "user", "text": "hi from phone", "t": 1},
            {"role": "ai", "text": "hello back", "t": 2},
        ]}
        r = c.post("/api/ai/history", json=payload)
        self.assertEqual(len(r.json()["messages"]), 2)
        r2 = c.post("/api/ai/history", json={"messages": [{"role": "user", "text": "overwrite", "t": 3}]})
        texts = [m["text"] for m in r2.json()["messages"]]
        self.assertIn("hi from phone", texts)
        self.assertNotIn("overwrite", texts)



if __name__ == "__main__":
    unittest.main()

# Extra state: a skipped coin, reject events, a tripped breaker. Mints stay for search only.
STATE_RICH = json.loads(json.dumps(STATE))
STATE_RICH["last_cycle_at"] = "2026-11-12T04:00:00+00:00"
STATE_RICH["starting_equity"] = 20.0
STATE_RICH["broker"]["open_count"] = 1
STATE_RICH["guard"] = {"breaker": {"state": "reduce", "mult": 0.5,
                                    "reason": "2 losses in a row, half size"},
                       "regime": {"tag": "risk_on"}}
STATE_RICH["decisions"] = list(STATE["decisions"]) + [
    {"symbol": "PEPE", "mint": "MintPEPE1111111111111111111111111111111",
     "approved": False, "reason": "ticket $1.40 is under the $2 fee floor",
     "ticket_usd": 1.40, "time": "2026-11-12T03:58:00+00:00",
     "thesis": {"verdict": "valid_setup", "score": 7},
     "brain": {"verdict": "watch", "confidence": 55, "agreement": "split"}},
]
STATE_RICH["events"] = [
    {"kind": "reject", "symbol": "BONK", "mint": "MintBONK1111111111111111111111111111111",
     "stage": "scanner", "reason": "low_liq",
     "message": "BONK: cut at scanner (low_liq)", "time": "2026-11-12T03:59:00+00:00"},
    {"kind": "reject", "symbol": "PEPE", "mint": "MintPEPE1111111111111111111111111111111",
     "stage": "risk", "reason": "ticket $1.40 is under the $2 fee floor",
     "message": "PEPE: risk engine said no (ticket $1.40 is under the $2 fee floor)",
     "time": "2026-11-12T03:58:00+00:00"},
]
STATE_RICH["execution"] = {"wallet": "WALLETSECRET", "mode": "paper"}


class Commands(unittest.TestCase):
    def test_intent_examples(self):
        cases = {
            "why was PEPE skipped": "skipped",
            "Why was $BONK skipped?": "skipped",
            "why no trade on PEPE": "skipped",
            "what happened to BONK": "skipped",
            "show my open trades": "positions",
            "Summarize my open paper positions.": "positions",
            "today's P&L": "pnl",
            "How is my paper P&L today?": "pnl",
            "breaker status": "breaker",
            "What's the breaker status?": "breaker",
            "last trades": "trades",
            "Show my last trades": "trades",
            "what's the desk doing": "status",
            "What is the scanner seeing right now?": "status",
            "Explain the last few decisions and the brain verdicts.": "decisions",
            "Risk check: is the daily loss stop close, and what is blocking entries?": "risk",
        }
        for q, want in cases.items():
            got = dc.match_intent(q)
            self.assertIsNotNone(got, q)
            self.assertEqual(got["intent"], want, q)
        self.assertEqual(dc.match_intent("why was PEPE skipped")["query"], "PEPE")
        self.assertEqual(dc.match_intent("Why was $BONK skipped?")["query"], "BONK")

    def test_settings_requests_are_not_commands(self):
        for q in ("set min paper ticket to 3", "change the stop loss to 15%",
                  "recommend settings", "scalper mode"):
            self.assertIsNone(dc.match_intent(q), q)

    def test_plain_fallback_without_keys(self):
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
            os.environ.pop(k, None)
        r = dc.answer("why was PEPE skipped", STATE_RICH)
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "desk")
        self.assertIn("PEPE", r["reply"])
        self.assertIn("1.40", r["reply"])
        self.assertNotIn("AI keys not set", r["reply"])
        self.assertNotIn("MintPEPE", r["reply"])
        self.assertNotIn("WALLETSECRET", r["reply"])

    def test_unknown_chat_without_keys_still_asks_for_keys(self):
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
            os.environ.pop(k, None)
        r = dc.answer("hello there", STATE_RICH)
        self.assertEqual(r["source"], "no_keys")
        self.assertIn("AI keys not set", r["reply"])

    def test_lookup_by_mint_does_not_leak_mint(self):
        mint = "MintBONK1111111111111111111111111111111"
        intent = dc.match_intent(f"why was {mint} skipped")
        self.assertEqual(intent["intent"], "skipped")
        facts = dc.command_facts(intent, STATE_RICH)
        blob = json.dumps(facts) + dc.format_command(intent, facts)
        self.assertIn("BONK", blob)
        self.assertNotIn(mint, blob)
        self.assertNotIn("WALLETSECRET", blob)
        self.assertNotIn("fomo_url", blob)

    def test_open_trades_and_pnl_and_breaker(self):
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
            os.environ.pop(k, None)
        pos = dc.answer("show my open trades", STATE_RICH)
        self.assertIn("MEOW", pos["reply"])
        self.assertIn("open paper", pos["reply"].lower())
        pnl = dc.answer("today's P&L", STATE_RICH)
        self.assertIn("0.60", pnl["reply"])
        br = dc.answer("breaker status", STATE_RICH)
        self.assertIn("half", br["reply"].lower())
        trades = dc.answer("last trades", STATE_RICH)
        self.assertIn("OLD", trades["reply"])
        st = dc.answer("what's the desk doing", STATE_RICH)
        self.assertIn("running", st["reply"].lower())

    def test_busy_ai_falls_back_to_desk_data(self):
        from desk.brain_advisor import ProviderError
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ADVISOR.gemini, "call", side_effect=ProviderError("429", 429)):
            r = dc.answer("why was BONK skipped", STATE_RICH)
        self.assertEqual(r["source"], "desk")
        self.assertIn("BONK", r["reply"])
        self.assertIn("scanner", r["reply"].lower())

    def test_prompt_includes_desk_facts(self):
        seen = {}

        def fake(system, user, max_tokens=700):
            seen["p"] = system + user
            return '{"reply":"PEPE was skipped because the ticket was under the floor.","agents":["risk"]}'
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ADVISOR.gemini, "call", side_effect=fake):
            r = dc.answer("why was PEPE skipped", STATE_RICH)
        self.assertEqual(r["source"], "ai")
        self.assertIn("desk_facts", seen["p"])
        self.assertIn("PEPE", seen["p"])
        self.assertNotIn("MintPEPE", seen["p"])
        self.assertNotIn(GKEY, seen["p"])
        self.assertNotIn("WALLETSECRET", seen["p"])

    def test_snapshot_breaker_and_rejects_still_whitelist(self):
        snap = json.dumps(dc.snapshot(STATE_RICH))
        for leak in ("WALLETSECRET", "fomo_url", "access", "execution", "mint", "MintPEPE", "MintBONK"):
            self.assertNotIn(leak, snap)
        s = dc.snapshot(STATE_RICH)
        self.assertEqual(s["breaker"]["state"], "reduce")
        self.assertEqual(s["breaker"]["mult"], 0.5)
        self.assertEqual(s["recent_rejects"][0]["symbol"], "BONK")
