"""Operator-confirmed PAPER settings: registry bounds, exclusions, proposals, confirm / reject, auth."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                                             # noqa: E402
from config import EXITS, RISK, SCAN                      # noqa: E402
from desk import desk_chat as dc                          # noqa: E402
from desk import settings as st                           # noqa: E402
from desk.brain_advisor import ADVISOR                    # noqa: E402

GKEY = "AIzaFAKEsettingsKEY1234567890abcdef"
FORBIDDEN = ["desk.mode", "mode", "execution.live_max_trade_usd", "live.enabled", "EXECUTION.default_mode",
             "chain.max_top_10", "chain.max_top_wallet", "chain.allow_mint_authority",
             "chain.allow_freeze_authority", "meme.max_dev_hold_pct", "meme.max_sniper_pct",
             "meme.max_creator_prev_rugs", "entry.require_confirm_quote", "SOLANA_PRIVATE_KEY",
             "GEMINI_API_KEY", "desk_token", "paper.starting_equity_usd", "kill_switch"]


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


class Registry(Base):
    def test_every_setting_has_bounds_and_current(self):
        for s in st.REGISTRY.values():
            d = s.describe()
            self.assertIn("current", d)
            if s.kind in ("pct", "float", "int"):
                self.assertLess(s.lo, s.hi, s.key)
                self.assertTrue(s.lo <= s.get() <= s.hi, f"{s.key}={s.get()} outside its bounds")

    def test_exclusions_never_in_registry_and_refused(self):
        for k in FORBIDDEN:
            self.assertNotIn(k, st.REGISTRY)
            with self.assertRaises(st.Rejected):
                st.resolve(k)

    def test_stop_loss_cannot_be_removed(self):
        ok, _ = st.validate_changes([{"setting": "exits.scalp.stop_pct", "proposed": 0}])
        self.assertEqual(ok[0]["proposed"], 0.03)
        ok, _ = st.validate_changes([{"setting": "exits.swing.stop_pct", "proposed": -1}])
        self.assertGreater(ok[0]["proposed"], 0)

    def test_daily_loss_capped(self):
        ok, _ = st.validate_changes([{"setting": "risk.max_daily_loss_pct", "proposed": "90%"}])
        self.assertEqual(ok[0]["proposed"], 0.10)

    def test_percent_forms(self):
        ok, _ = st.validate_changes([{"setting": "exits.scalp.stop_pct", "proposed": "15%"}])
        self.assertEqual(ok[0]["proposed"], 0.15)
        ok, _ = st.validate_changes([{"setting": "exits.scalp.stop_pct", "proposed": 15}])
        self.assertEqual(ok[0]["proposed"], 0.15)

    def test_garbage_and_cross_checks_dropped(self):
        ok, dropped = st.validate_changes([{"setting": "risk.max_open_positions", "proposed": "lots"}])
        self.assertEqual(ok, [])
        self.assertTrue(dropped)
        ok, dropped = st.validate_changes([{"setting": "exits.scalp.tp2_at", "proposed": 1.1},
                                           {"setting": "exits.scalp.tp1_at", "proposed": 3.0}])
        self.assertEqual(ok, [])
        self.assertTrue(any("take profit 2" in d["why"] for d in dropped))

    def test_style_filter(self):
        EXITS["style"] = "scalp"
        self.assertTrue(st.style_allows("scalp"))
        self.assertFalse(st.style_allows("swing"))
        EXITS["style"] = "both"
        self.assertTrue(st.style_allows("swing"))


class Flow(Base):
    def test_create_never_applies(self):
        before = RISK["max_risk_per_trade"]
        out = st.create([{"setting": "risk.max_risk_per_trade", "proposed": 0.02}], "test", "desk_ai")
        self.assertTrue(out["ok"])
        self.assertEqual(RISK["max_risk_per_trade"], before)
        self.assertEqual(len(st.listing()["pending"]), 1)

    def test_excluded_dropped_from_ai_proposal(self):
        out = st.create([{"setting": "desk.mode", "proposed": "live"},
                         {"setting": "chain.max_top_10", "proposed": 0.9},
                         {"setting": "risk.max_open_positions", "proposed": 3}], "x", "desk_ai")
        self.assertTrue(out["ok"])
        keys = [c["setting"] for c in out["proposal"]["changes"]]
        self.assertEqual(keys, ["risk.max_open_positions"])
        self.assertEqual(len(out["proposal"]["dropped"]), 2)
        only_bad = st.create([{"setting": "desk.mode", "proposed": "live"}], "x", "desk_ai")
        self.assertFalse(only_bad["ok"])

    def test_confirm_applies_persists_and_journals(self):
        pid = st.create([{"setting": "exits.scalp.stop_pct", "proposed": 0.12}], "tighter", "desk_ai")["proposal"]["id"]
        r = st.confirm(pid, "tester")
        self.assertTrue(r["ok"])
        self.assertEqual(EXITS["profiles"]["scalp"]["stop_pct"], 0.12)
        saved = json.loads((Path(self.tmp.name) / "operator_settings.json").read_text())
        self.assertEqual(saved["values"]["exits.scalp.stop_pct"], 0.12)
        rows = [json.loads(x) for x in (Path(self.tmp.name) / "journal.jsonl").read_text().splitlines()]
        row = [x for x in rows if x["kind"] == "settings_change"][-1]
        self.assertEqual(row["action"], "confirm")
        self.assertEqual(row["by"], "tester")
        self.assertEqual(row["changes"][0]["from"], 0.2)
        self.assertEqual(row["changes"][0]["to"], 0.12)
        # survives a restart: reset in-memory value, re-apply from disk
        EXITS["profiles"]["scalp"]["stop_pct"] = 0.2
        st.apply_saved()
        self.assertEqual(EXITS["profiles"]["scalp"]["stop_pct"], 0.12)
        # second confirm is refused
        self.assertFalse(st.confirm(pid, "tester")["ok"])

    def test_reject_does_not_apply(self):
        before = SCAN["min_liquidity_usd"]
        pid = st.create([{"setting": "scan.min_liquidity_usd", "proposed": 9_000}], "x", "desk_ai")["proposal"]["id"]
        r = st.reject(pid, "tester")
        self.assertTrue(r["ok"])
        self.assertEqual(SCAN["min_liquidity_usd"], before)
        self.assertFalse(st.confirm(pid, "tester")["ok"])
        self.assertFalse((Path(self.tmp.name) / "operator_settings.json").exists())

    def test_expired_cannot_confirm(self):
        pid = st.create([{"setting": "risk.max_open_positions", "proposed": 2}], "x", "desk_ai")["proposal"]["id"]
        with mock.patch.object(st, "_now", return_value=st._now() + st.PROPOSAL_TTL_S + 1):
            r = st.confirm(pid, "t")
        self.assertFalse(r["ok"])
        self.assertNotEqual(RISK["max_open_positions"], 2)

    def test_tampered_saved_file_is_clamped_and_filtered(self):
        p = Path(self.tmp.name) / "operator_settings.json"
        p.write_text(json.dumps({"values": {"exits.scalp.stop_pct": 0, "desk.mode": "live",
                                            "risk.max_daily_loss_pct": 0.9}}))
        applied = st.apply_saved()
        self.assertNotIn("desk.mode", applied)
        self.assertEqual(EXITS["profiles"]["scalp"]["stop_pct"], 0.03)
        self.assertEqual(RISK["max_daily_loss_pct"], 0.10)

    def test_scalper_preset_is_one_card_with_every_change(self):
        out = st.create_preset("scalper")
        self.assertTrue(out["ok"])
        keys = {c["setting"] for c in out["proposal"]["changes"]}
        self.assertIn("trading.style", keys)
        self.assertIn("exits.scalp.stop_pct", keys)
        self.assertEqual(EXITS["style"], "both")
        self.assertTrue(st.confirm(out["proposal"]["id"], "t")["ok"])
        self.assertEqual(EXITS["style"], "scalp")
        back = st.create_preset("balanced")
        self.assertTrue(st.confirm(back["proposal"]["id"], "t")["ok"])
        self.assertEqual(EXITS["style"], "both")
        self.assertEqual(EXITS["profiles"]["scalp"]["stop_pct"], 0.20)

    def test_scalper_preset_rearms_the_scalp_take_profit_ladder(self):
        # If the operator had switched to a single/global plan, confirming scalper
        # mode must put the per-horizon scalp ladder back in force.
        EXITS["take_profit_mode"] = "single"
        out = st.create_preset("scalper")
        self.assertTrue(out["ok"])
        keys = {c["setting"] for c in out["proposal"]["changes"]}
        self.assertIn("exits.tp_plan", keys)
        self.assertTrue(st.confirm(out["proposal"]["id"], "t")["ok"])
        self.assertEqual(EXITS["take_profit_mode"], "profile")
        self.assertEqual(EXITS["profiles"]["scalp"]["tp1_at"], 1.3)
        self.assertEqual(EXITS["profiles"]["scalp"]["tp2_at"], 1.8)
        self.assertGreater(EXITS["profiles"]["scalp"]["tp1_frac"], 0)
        self.assertGreater(EXITS["profiles"]["scalp"]["tp2_frac"], 0)

    def test_score_setter_moves_both_gates(self):
        pid = st.create([{"setting": "analyst.valid_score", "proposed": 7}], "x", "t")["proposal"]["id"]
        st.confirm(pid, "t")
        self.assertEqual(config.MIN_SCORE, 7)
        self.assertEqual(config.ANALYST_VALID_SCORE, 7)


class ChatProposals(Base):
    def test_model_proposals_parsed_and_raw(self):
        out = json.dumps({"reply": "Tighten the stop.", "agents": ["risk"],
                          "proposals": [{"setting": "exits.scalp.stop_pct", "proposed": 0.15, "reason": "big losers"},
                                        {"setting": "desk.mode", "proposed": "live", "reason": "go live"}]})
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ADVISOR.gemini, "call", return_value=out):
            r = dc.answer("lower my stop loss", {}, settings=st.compact_for_ai())
        self.assertEqual(len(r["proposals"]), 2)          # raw: the caller validates
        made = st.create(r["proposals"], "x", "desk_ai")
        self.assertEqual([c["setting"] for c in made["proposal"]["changes"]], ["exits.scalp.stop_pct"])

    def test_prompt_has_registry_but_no_secrets(self):
        seen = {}

        def fake(system, user, max_tokens=700):
            seen["p"] = system + user
            return '{"reply":"ok","agents":[],"proposals":[]}'
        env = {"GEMINI_API_KEY": GKEY, "DESK_TOKEN": "deskTOKENsettings99", "SOLANA_PRIVATE_KEY": "privKEYshouldNEVERappear"}
        with mock.patch.dict(os.environ, env), mock.patch.object(ADVISOR.gemini, "call", side_effect=fake):
            dc.answer(dc.RECOMMEND_Q, {}, settings=st.compact_for_ai(), perf=dc.performance({}, None))
        self.assertIn("exits.scalp.stop_pct", seen["p"])
        for v in env.values():
            self.assertNotIn(v, seen["p"])


class Endpoints(Base):
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
        super().tearDown()

    def test_auth_required(self):
        c = self._client("s3cretSETTINGS")
        pid = st.create([{"setting": "risk.max_open_positions", "proposed": 2}], "x", "t")["proposal"]["id"]
        for method, url in (("get", "/api/settings"), ("get", "/api/proposals"), ("post", "/api/proposals"),
                            ("post", f"/api/proposals/{pid}/confirm"), ("post", f"/api/proposals/{pid}/reject"),
                            ("post", "/api/ai/recommend")):
            r = getattr(c, method)(url, json={}) if method == "post" else c.get(url)
            self.assertEqual(r.status_code, 401, url)
        self.assertNotEqual(RISK["max_open_positions"], 2)

    def test_full_flow_signed_in(self):
        c = self._client("s3cretSETTINGS")
        c.post("/login", data={"token": "s3cretSETTINGS"}, follow_redirects=False)
        s = c.get("/api/settings").json()
        blob = json.dumps(s)
        self.assertNotIn("s3cretSETTINGS", blob)
        self.assertIn("Risk", s["groups"])
        self.assertIn("desk.mode", s["excluded"])
        r = c.post("/api/proposals", json={"preset": "scalper"})
        self.assertEqual(r.status_code, 200)
        pid = r.json()["proposal"]["id"]
        self.assertEqual(EXITS["style"], "both")
        r = c.post(f"/api/proposals/{pid}/confirm")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(EXITS["style"], "scalp")
        self.assertEqual(c.post(f"/api/proposals/{pid}/confirm").status_code, 409)
        pid2 = st.create([{"setting": "risk.max_open_positions", "proposed": 2}], "x", "t")["proposal"]["id"]
        self.assertEqual(c.post(f"/api/proposals/{pid2}/reject").status_code, 200)
        self.assertNotEqual(RISK["max_open_positions"], 2)
        self.assertEqual(c.post("/api/proposals/nope/confirm").status_code, 404)
        lst = c.get("/api/proposals").json()
        self.assertEqual(lst["pending"], [])
        self.assertNotIn("s3cretSETTINGS", json.dumps(lst))

    def test_chat_creates_pending_only(self):
        c = self._client(None)
        out = json.dumps({"reply": "ok", "agents": [],
                          "proposals": [{"setting": "risk.max_risk_per_trade", "proposed": 0.02, "reason": "r"},
                                        {"setting": "desk.mode", "proposed": "live"}]})
        before = RISK["max_risk_per_trade"]
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ADVISOR.gemini, "call", return_value=out):
            r = c.post("/api/ai/chat", json={"message": "lower risk"}).json()
        self.assertEqual(len(r["proposals"]), 1)
        self.assertEqual(r["proposals"][0]["changes"][0]["setting"], "risk.max_risk_per_trade")
        self.assertTrue(any(d["setting"] == "desk.mode" for d in r["dropped"]))
        self.assertEqual(RISK["max_risk_per_trade"], before)


if __name__ == "__main__":
    unittest.main()


class StyleExpansion(Base):
    def test_ai_style_switch_brings_related_values(self):
        raw = [{"setting": "trading.style", "proposed": "scalp"}, {"setting": "exits.scalp.stop_pct", "proposed": 0.12}]
        full, name = st.expand_style(raw)
        self.assertEqual(name, "scalper")
        out = st.create(full, "x", "desk_ai", preset=name)
        ch = {c["setting"]: c["proposed"] for c in out["proposal"]["changes"]}
        self.assertEqual(ch["exits.scalp.stop_pct"], 0.12)          # AI's explicit value wins
        self.assertEqual(ch["trading.style"], "scalp")
        self.assertIn("exits.scalp.trail_pct", ch)

    def test_truncated_json_reply_is_readable(self):
        out = dc._parse('{"reply":"Tighten the stop to 12%.","agents":["risk"],"proposals":[{"setting":"exits.sca')
        self.assertEqual(out["reply"], "Tighten the stop to 12%.")
