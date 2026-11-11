"""The AI second opinion: Gemini + Groq, mocked HTTP only. Advisory; it can never move money."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DESK_OFFLINE", "1")

from desk import brain_advisor as ba  # noqa: E402

GKEY, QKEY = "AIzaFAKEGEMINIKEY123456", "gsk_FAKEGROQKEY1234567890"
SUMMARY = {"symbol": "SYM", "age_minutes": 30, "liquidity_usd": 20000, "mcap_usd": 100000}


def opinion(v="watch", c=62, reasons=("thin liquidity", "real volume"), flags=()):
    return json.dumps({"verdict": v, "confidence": c, "reasons": list(reasons), "red_flags": list(flags)})


def chat(say, v):
    return json.dumps({"say": say, "verdict": v})


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text

    def json(self):
        return self._body


def gem(text):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def groq(text):
    return Resp(200, {"choices": [{"message": {"content": text}}]})


class Net:
    """Scripted per-provider replies; records every call (url, headers, body)."""

    def __init__(self, gemini=(), groq_=(), orr=()):
        self.q = {"gemini": list(gemini), "groq": list(groq_), "openrouter": list(orr)}
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        who = ("gemini" if "generativelanguage" in url else
               "openrouter" if "openrouter.ai" in url else "groq")
        self.calls.append((who, url, headers, json, timeout))
        item = self.q[who].pop(0) if self.q[who] else Resp(500, text="script exhausted")
        if isinstance(item, Exception):
            raise item
        return item

    def n(self, who):
        return sum(1 for c in self.calls if c[0] == who)


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY, "GROQ_API_KEY": QKEY})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.adv = ba.Advisor()
        self.lines = []
        self.addCleanup(self.adv._pool.shutdown, wait=True, cancel_futures=True)

    def run_review(self, net, mint="M1", adv=None):
        with mock.patch.object(ba.requests, "post", net):
            return (adv or self.adv).review(mint, SUMMARY, self.lines.append)


class Opinions(Base):
    def test_gemini_success_parse(self):
        os.environ.pop("GROQ_API_KEY")
        net = Net(gemini=[gem(opinion("watch", 62))])
        r = self.run_review(net)
        self.assertEqual((r["verdict"], r["confidence"], r["source"]), ("watch", 62, "gemini"))
        self.assertEqual(r["opinions"]["gemini"]["reasons"], ["thin liquidity", "real volume"])
        self.assertEqual(net.n("groq"), 0)
        self.assertEqual(self.lines, ["brain(gemini): $SYM -> watch 62% \u2014 thin liquidity; real volume"])
        who, url, headers, body, timeout = net.calls[0]
        self.assertIn("gemini-3.6-flash:generateContent", url)
        self.assertNotIn(GKEY, url)                      # key in a header, never in the URL
        self.assertEqual(headers["x-goog-api-key"], GKEY)
        self.assertLessEqual(timeout, 8)

    def test_both_asked_independently_and_agree(self):
        net = Net(gemini=[gem(opinion("buy", 70))], groq_=[groq(opinion("buy", 50))])
        r = self.run_review(net)
        self.assertEqual((net.n("gemini"), net.n("groq")), (1, 1))
        self.assertEqual((r["agreement"], r["verdict"], r["confidence"]), ("agree", "buy", 60))
        self.assertIn("brain: $SYM agree buy", self.lines)
        self.assertTrue(any(l.startswith("brain(gemini): $SYM -> buy 70%") for l in self.lines))
        self.assertTrue(any(l.startswith("brain(groq): $SYM -> buy 50%") for l in self.lines))
        self.assertEqual(net.calls[1][3]["model"] if net.calls[1][0] == "groq" else
                         net.calls[0][3]["model"], "openai/gpt-oss-120b")

    def test_groq_alone_when_gemini_429(self):
        net = Net(gemini=[Resp(429, headers={"Retry-After": "90"})], groq_=[groq(opinion("skip", 70))])
        r = self.run_review(net)
        self.assertEqual((r["verdict"], r["source"], r["agreement"]), ("skip", "groq", "single"))
        self.assertTrue(any("gemini" in l and "429" in l for l in self.lines))
        snap = self.adv.gemini.snapshot()
        self.assertGreaterEqual(snap["cooldown_s"], 60)
        # the cooling provider is not called again; the other still works
        net2 = Net(groq_=[groq(opinion("watch", 40))])
        r2 = self.run_review(net2, mint="M2")
        self.assertEqual((net2.n("gemini"), net2.n("groq")), (0, 1))
        self.assertEqual(r2["source"], "groq")

    def test_gemini_alone_when_groq_errors(self):
        net = Net(gemini=[gem(opinion("watch", 55))], groq_=[requests_timeout()])
        r = self.run_review(net)
        self.assertEqual((r["source"], r["verdict"]), ("gemini", "watch"))

    def test_no_keys_is_a_noop(self):
        os.environ.pop("GEMINI_API_KEY")
        os.environ.pop("GROQ_API_KEY")
        net = Net()
        self.assertFalse(self.adv.enabled())
        self.assertIsNone(self.run_review(net))
        self.assertEqual(net.calls, [])
        self.assertEqual(self.adv.submit("M", SUMMARY, lambda r: self.fail("no callback")), "skipped")

    def test_malformed_json_is_handled(self):
        for bad in ("not json at all", "{\"verdict\": \"moon\", \"confidence\": 90}",
                    "{\"verdict\": \"buy\", \"confidence\": \"lots\"}", "", "[1,2]"):
            adv = ba.Advisor()
            os.environ.pop("GROQ_API_KEY", None)
            r = self.run_review(Net(gemini=[gem(bad)]), adv=adv)
            self.assertIsNone(r, bad)
        os.environ["GROQ_API_KEY"] = QKEY
        # one malformed, one good: the good one stands alone
        r = self.run_review(Net(gemini=[gem("garbage")], groq_=[groq(opinion("skip", 80))]))
        self.assertEqual((r["source"], r["verdict"]), ("groq", "skip"))

    def test_fenced_json_and_clamping(self):
        os.environ.pop("GROQ_API_KEY")
        txt = "```json\n" + opinion("buy", 250, ["a", "b", "c", "d"]) + "\n```"
        r = self.run_review(Net(gemini=[gem(txt)]))
        self.assertEqual(r["confidence"], 100)
        self.assertEqual(len(r["opinions"]["gemini"]["reasons"]), 3)

    def test_budget_enforced(self):
        os.environ.pop("GROQ_API_KEY")
        self.adv.gemini.budget = 2
        net = Net(gemini=[gem(opinion()) for _ in range(5)])
        for i in range(4):
            self.run_review(net, mint=f"M{i}")
        self.assertEqual(net.n("gemini"), 2)
        self.assertEqual(self.adv.gemini.snapshot()["used_today"], 2)
        self.assertFalse(self.adv.gemini.available())

    def test_cache_per_mint(self):
        os.environ.pop("GROQ_API_KEY")
        net = Net(gemini=[gem(opinion()), gem(opinion("skip", 90))])
        a = self.run_review(net)
        b = self.run_review(net)
        self.assertIs(a, b)
        self.assertEqual(net.n("gemini"), 1)

    def test_keys_never_leak_in_errors_or_logs(self):
        net = Net(gemini=[Resp(403, text=f"bad key {GKEY} key={GKEY}")], groq_=[Resp(401, text=f"Bearer {QKEY}")])
        self.run_review(net)
        blob = "\n".join(self.lines)
        self.assertNotIn(GKEY, blob)
        self.assertNotIn(QKEY, blob)
        self.assertNotIn(GKEY, ba.redact(f"https://x?key={GKEY}"))
        self.assertNotIn(QKEY, ba.redact(f"Authorization: Bearer {QKEY}"))
        self.assertNotIn(GKEY, json.dumps(self.adv.status()))

    def test_per_cycle_cap_and_nonblocking_submit(self):
        os.environ.pop("GROQ_API_KEY")
        gate = threading.Event()
        done = []

        def slow(url, headers=None, json=None, timeout=None):
            gate.wait(5)
            return gem(opinion())

        with mock.patch.object(ba.requests, "post", slow):
            t0 = time.time()
            states = [self.adv.submit(f"M{i}", SUMMARY, done.append, self.lines.append) for i in range(4)]
            self.assertLess(time.time() - t0, 1.0)             # returned without waiting on the model
            self.assertEqual(states, ["queued", "queued", "skipped", "skipped"])
            gate.set()
            for _ in range(50):
                if len(done) == 2:
                    break
                time.sleep(0.05)
            self.assertEqual(len(done), 2)
            self.adv.new_cycle()
            # Keep the post mock in place and wait; a leftover worker must not
            # leak into the next test and steal its scripted HTTP replies.
            self.assertEqual(self.adv.submit("M9", SUMMARY, done.append), "queued" if self.adv.enabled() else "")
            for _ in range(50):
                if len(done) == 3:
                    break
                time.sleep(0.05)
            self.assertEqual(len(done), 3)


def requests_timeout():
    return ba.requests.Timeout("slow")


class Debate(Base):
    def split_net(self, **kw):
        g = [gem(opinion("watch", 62, ("liquidity thin but volume real",))),
             gem(chat("fair, but a rug setup is a rug setup, I'll say skip too", "skip"))]
        q = [groq(opinion("skip", 70, ("dev holds 18%",))),
             groq(chat("dev holds 18%, that's a rug setup, skip", "skip")),
             groq(chat("still skip", "skip"))]
        return Net(gemini=g, groq_=q)

    def test_split_triggers_debate_with_transcript_and_final(self):
        net = self.split_net()
        r = self.run_review(net)
        self.assertEqual(r["agreement"], "split")
        self.assertEqual((net.n("gemini"), net.n("groq")), (2, 3))
        self.assertLessEqual(net.n("gemini") - 1, ba.DEBATE_CALLS_PER_PROVIDER)
        self.assertLessEqual(net.n("groq") - 1, ba.DEBATE_CALLS_PER_PROVIDER)
        self.assertEqual([l["bot"] for l in r["debate"]], ["gemini", "groq", "gemini", "groq"])
        self.assertEqual(r["final"], {"verdict": "skip", "votes": {"gemini": "skip", "groq": "skip"}, "agree": 2})
        self.assertEqual(r["verdict"], "skip")
        self.assertIn("brain: $SYM split (gemini watch / groq skip)", self.lines)
        self.assertIn("\U0001F4AC groq: dev holds 18%, that's a rug setup, skip", self.lines)
        self.assertEqual(self.lines[-1], "brain final: $SYM skip (2/2)")
        chats = [l for l in self.lines if l.startswith("\U0001F4AC")]
        self.assertEqual(len(chats), 4)
        self.assertTrue(chats[0].startswith("\U0001F4AC gemini:"))
        # the debate counts against the same daily budgets
        self.assertEqual(self.adv.gemini.snapshot()["used_today"], 2)
        self.assertEqual(self.adv.groq.snapshot()["used_today"], 3)

    def test_no_debate_when_they_agree(self):
        net = Net(gemini=[gem(opinion("skip", 60))], groq_=[groq(opinion("skip", 80))])
        r = self.run_review(net)
        self.assertNotIn("debate", r)
        self.assertEqual((net.n("gemini"), net.n("groq")), (1, 1))

    def test_debate_skipped_when_budget_low(self):
        self.adv.groq.budget = 10
        self.adv.groq.used = 9          # under 10% (and under 2 calls) left
        net = Net(gemini=[gem(opinion("watch", 62))], groq_=[groq(opinion("skip", 70))])
        r = self.run_review(net)
        self.assertEqual(r["agreement"], "split")
        self.assertNotIn("debate", r)
        self.assertEqual((net.n("gemini"), net.n("groq")), (1, 1))

    def test_one_debate_per_cycle(self):
        net = Net(gemini=self.split_net().q["gemini"] + [gem(opinion("watch", 60))] * 2,
                  groq_=self.split_net().q["groq"] + [groq(opinion("skip", 70))] * 2)
        r1 = self.run_review(net, mint="A")
        r2 = self.run_review(net, mint="B")
        self.assertIn("debate", r1)
        self.assertNotIn("debate", r2)
        self.adv.new_cycle()
        self.assertTrue(self.adv._debate_ok())

    def test_debate_survives_a_failed_reply(self):
        net = Net(gemini=[gem(opinion("watch", 62))],
                  groq_=[groq(opinion("skip", 70)), Resp(429)])
        r = self.run_review(net)
        self.assertEqual(r["verdict"], "skip")            # cautious pick of the two opinions
        self.assertNotIn("final", r)
        self.assertEqual(r["debate"][0]["bot"], "gemini")

    def test_debate_split_stays_cautious(self):
        net = Net(gemini=[gem(opinion("buy", 80)), gem(chat("still a buy", "buy"))],
                  groq_=[groq(opinion("skip", 70)), groq(chat("nah", "skip")), groq(chat("skip", "skip"))])
        r = self.run_review(net)
        self.assertEqual(r["final"]["verdict"], "skip")
        self.assertEqual(r["final"]["agree"], 1) if r["final"]["votes"]["gemini"] != "skip" else None
        self.assertEqual(r["verdict"], "skip")


ORKEY = "sk-or-v1-FAKEOPENROUTERKEY123456"


class OpenRouterProvider(Base):
    def setUp(self):
        super().setUp()
        os.environ.pop("GROQ_API_KEY")                  # Romz's setup: Gemini + OpenRouter
        os.environ["OPENROUTER_API_KEY"] = ORKEY

    def test_gemini_plus_openrouter_pair_and_debate(self):
        net = Net(gemini=[gem(opinion("watch", 62, ("liquidity thin but volume real",))),
                          gem(chat("fine, skip", "skip"))],
                  orr=[groq(opinion("skip", 70, ("dev holds 18%",))),
                       groq(chat("dev holds 18%, that's a rug setup, skip", "skip")),
                       groq(chat("skip", "skip"))])
        r = self.run_review(net)
        self.assertEqual(r["providers"], ["gemini", "openrouter"])
        self.assertEqual((net.n("gemini"), net.n("openrouter"), net.n("groq")), (2, 3, 0))
        self.assertIn("brain: $SYM split (gemini watch / openrouter skip)", self.lines)
        self.assertIn("\U0001F4AC openrouter: dev holds 18%, that's a rug setup, skip", self.lines)
        self.assertEqual(self.lines[-1], "brain final: $SYM skip (2/2)")
        self.assertEqual(r["final"]["votes"], {"gemini": "skip", "openrouter": "skip"})
        who, url, headers, body, timeout = [c for c in net.calls if c[0] == "openrouter"][0]
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer " + ORKEY)
        self.assertTrue(body["model"].endswith(":free"))
        self.assertEqual(body["model"], ba.OPENROUTER_MODELS[0])
        self.assertLessEqual(timeout, 8)
        self.assertEqual(self.adv.openrouter.snapshot()["used_today"], 3)
        self.assertEqual(r["opinions"]["openrouter"]["model"], ba.OPENROUTER_MODELS[0])

    def test_first_two_in_order_gemini_groq_openrouter(self):
        os.environ["GROQ_API_KEY"] = QKEY
        net = Net(gemini=[gem(opinion("skip", 60))], groq_=[groq(opinion("skip", 60))],
                  orr=[groq(opinion("buy", 90))])
        r = self.run_review(net)
        self.assertEqual(r["providers"], ["gemini", "groq"])
        self.assertEqual(net.n("openrouter"), 0)
        # gemini cooling: groq + openrouter step in
        self.adv.gemini.cool(600)
        net2 = Net(groq_=[groq(opinion("skip", 60))], orr=[groq(opinion("skip", 50))])
        r2 = self.run_review(net2, mint="M2")
        self.assertEqual(r2["providers"], ["groq", "openrouter"])
        self.assertEqual(net2.n("gemini"), 0)

    def test_openrouter_falls_back_to_next_free_model(self):
        os.environ.pop("GEMINI_API_KEY")
        net = Net(orr=[Resp(404, text="No endpoints found"), groq(opinion("watch", 40))])
        r = self.run_review(net)
        models = [c[3]["model"] for c in net.calls]
        self.assertEqual(models, ba.OPENROUTER_MODELS[:2])
        self.assertEqual(r["opinions"]["openrouter"]["model"], ba.OPENROUTER_MODELS[1])
        self.assertTrue(all(m.endswith(":free") for m in ba.OPENROUTER_MODELS))
        self.assertGreaterEqual(len(ba.OPENROUTER_MODELS), 3)

    def test_openrouter_429_cools_and_gemini_stands_alone(self):
        net = Net(gemini=[gem(opinion("watch", 55))], orr=[Resp(429, headers={"Retry-After": "60"})])
        r = self.run_review(net)
        self.assertEqual((r["source"], net.n("openrouter")), ("gemini", 1))   # no model hopping on 429
        self.assertGreaterEqual(self.adv.openrouter.snapshot()["cooldown_s"], 30)
        self.assertFalse(self.adv.openrouter.available())

    def test_openrouter_daily_budget(self):
        self.assertEqual(self.adv.openrouter.budget, 45)
        os.environ.pop("GEMINI_API_KEY")
        net = Net(orr=[groq(opinion()) for _ in range(60)])
        for i in range(50):
            self.run_review(net, mint=f"M{i}")
        self.assertEqual(net.n("openrouter"), 45)
        self.assertFalse(self.adv.openrouter.available())

    def test_no_debate_when_openrouter_budget_under_10(self):
        self.adv.openrouter.used = 45 - 10      # exactly 10 left; one opinion call leaves 9
        net = Net(gemini=[gem(opinion("watch", 62))], orr=[groq(opinion("skip", 70))])
        r = self.run_review(net)
        self.assertEqual(r["agreement"], "split")
        self.assertNotIn("debate", r)
        self.assertEqual((net.n("gemini"), net.n("openrouter")), (1, 1))

    def test_model_override_env(self):
        with mock.patch.dict(os.environ, {"BRAIN_OPENROUTER_MODEL": "google/gemma-4-31b-it:free"}):
            ms = ba.openrouter_models()
        self.assertEqual(ms[0], "google/gemma-4-31b-it:free")
        self.assertEqual(len(ms), len(set(ms)))
        self.assertEqual(ba.openrouter_models("")[0], "nvidia/nemotron-3-ultra-550b-a55b:free")

    def test_openrouter_key_never_leaks(self):
        net = Net(gemini=[Resp(500, text="x")], orr=[Resp(401, text=f"bad Bearer {ORKEY}")])
        self.run_review(net)
        self.assertNotIn(ORKEY, "\n".join(self.lines))
        self.assertNotIn(ORKEY, ba.redact(f"token {ORKEY}"))


class Advisory(unittest.TestCase):
    """The verdict must never alter the risk/broker decision, and the risk path can't see it."""

    def test_risk_and_broker_do_not_import_the_advisor(self):
        root = Path(__file__).resolve().parents[1]
        for f in ("desk/risk.py", "desk/broker.py", "desk/live.py", "desk/guard.py", "desk/exits.py",
                  "desk/devcheck.py", "desk/chain.py", "desk/meme.py", "desk/signals.py", "config.py"):
            self.assertNotIn("brain_advisor", (root / f).read_text(encoding="utf-8"), f)

    def _run(self, verdict):
        from test_guard_integration import TOK, Base as GBase
        from desk.brain_advisor import ADVISOR
        out = {}

        class T(GBase):
            def runTest(self):
                pass
        t = T()
        t.setUp() if hasattr(t, "setUp") else None
        fake = {"symbol": "GOOD", "verdict": verdict, "confidence": 99, "advisory_only": True,
                "opinions": {}, "agreement": "agree"}
        env = {"GEMINI_API_KEY": GKEY, "GROQ_API_KEY": QKEY}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ADVISOR, "review", return_value=fake):
            ADVISOR.new_cycle()
            ADVISOR._cache.clear()
            a = t.run_agent()
            for _ in range(50):
                if a.decisions and a.decisions[0].get("brain", {}).get("verdict") == verdict:
                    break
                time.sleep(0.05)
            t.doCleanups()
        self.assertTrue(a.decisions, [e['message'] for e in list(a.events)[:8]])
        d = a.decisions[0]
        out.update(approved=d["approved"], ticket=d["ticket_usd"], reason=d["reason"],
                   entries=a.stats["entries"], brain=d.get("brain", {}).get("verdict"),
                   cost=a.paper.s["positions"].get(TOK["mint"], {}).get("cost"),
                   mode=a.mode, sizes=json.dumps(d["sizes"], sort_keys=True, default=str))
        return out

    def test_verdict_never_changes_the_decision(self):
        buy, skip = self._run("buy"), self._run("skip")
        self.assertEqual(buy["brain"], "buy")
        self.assertEqual(skip["brain"], "skip")
        for k in ("approved", "ticket", "reason", "entries", "cost", "mode", "sizes"):
            self.assertEqual(buy[k], skip[k], k)
        self.assertEqual(buy["mode"], "paper")

    def test_skip_verdict_cannot_unkill_or_force_a_buy(self):
        """A 'buy' on a coin the dev check killed changes nothing: the kill happens first."""
        from test_guard_integration import Base as GBase
        from desk.brain_advisor import ADVISOR
        from desk import devcheck

        class T(GBase):
            def runTest(self):
                pass
        t = T()
        calls = []
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY, "GROQ_API_KEY": QKEY}), \
                mock.patch.object(ADVISOR, "review", side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch.object(devcheck, "dev_kill", return_value="dev_rug_history"):
            a = t.run_agent()
            t.doCleanups()
        self.assertEqual(a.stats["entries"], 0)
        self.assertEqual(a.stats["dev_rejects"], 1)
        self.assertEqual(calls, [])                        # killed coins are never even sent to the brain

    def test_keys_alone_leave_analyst_on_heuristic_and_dialogue_never_raises(self):
        from desk import brain, dialogue
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY, "GROQ_API_KEY": QKEY}), \
                mock.patch.object(brain, "_ollama_up", return_value=False):
            self.assertEqual(brain.active(), "heuristic")
        with mock.patch.object(dialogue, "_model_debate", side_effect=RuntimeError("429")), \
                mock.patch.object(dialogue, "_template_debate", return_value=[{"text": "t"}]):
            out = dialogue.debate({}, {}, {}, {}, {}, {}, False, 0, "r", {})
        self.assertEqual(out["backend"], "template")

    def test_no_keys_no_brain_key_on_decision(self):
        from test_guard_integration import Base as GBase

        class T(GBase):
            def runTest(self):
                pass
        t = T()
        a = t.run_agent()
        t.doCleanups()
        self.assertNotIn("brain", a.decisions[0])
        self.assertFalse(a.status()["advisor"]["enabled"])


if __name__ == "__main__":
    unittest.main()
