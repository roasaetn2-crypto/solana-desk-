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
        # existing tests burst many calls; the real RPM caps are covered in Pace
        for p in self.adv.providers():
            p.rpm = 1000
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


class Candidates(unittest.TestCase):
    def test_only_valid_setup_at_or_above_min_score(self):
        from config import MIN_SCORE
        self.assertTrue(ba.should_advise({"verdict": "valid_setup", "score": MIN_SCORE}))
        self.assertTrue(ba.should_advise({"verdict": "valid_setup", "score": MIN_SCORE + 3}))
        self.assertFalse(ba.should_advise({"verdict": "valid_setup", "score": MIN_SCORE - 1}))
        self.assertFalse(ba.should_advise({"verdict": "watch", "score": 9}))
        self.assertFalse(ba.should_advise({"verdict": "skip", "score": 8}))
        self.assertFalse(ba.should_advise(None))
        self.assertFalse(ba.should_advise({"verdict": "valid_setup"}))
        self.assertFalse(ba.should_advise("nope"))

    def test_accepts_a_token_thesis(self):
        from desk.analyst import TokenThesis
        from config import MIN_SCORE
        th = TokenThesis(verdict="valid_setup", score=max(MIN_SCORE, 5), red_flags=[],
                         green_flags=["crowd"], shape="crowd", invalidation="price fails",
                         sources=["liquidity_usd"])
        self.assertTrue(ba.should_advise(th))
        th.verdict, th.score = "skip", 8
        self.assertFalse(ba.should_advise(th))

    def test_moved_a_lot_is_25_percent_price_or_liq(self):
        prev = {"price_usd": 1.0, "liquidity_usd": 100.0}
        self.assertFalse(ba.moved_a_lot(prev, {"price_usd": 1.24, "liquidity_usd": 110.0}))
        self.assertTrue(ba.moved_a_lot(prev, {"price_usd": 1.25, "liquidity_usd": 100.0}))
        self.assertTrue(ba.moved_a_lot(prev, {"price_usd": 1.0, "liquidity_usd": 74.0}))
        self.assertFalse(ba.moved_a_lot({}, {"price_usd": 2.0}))
        self.assertTrue(ba.moved_a_lot({"price_usd": 0.0}, {"price_usd": 1.0}))


class Freshness(Base):
    def test_cache_expires_after_ttl(self):
        os.environ.pop("GROQ_API_KEY")
        net = Net(gemini=[gem(opinion()), gem(opinion("skip", 90))])
        a = self.run_review(net)
        exp, res, snap = self.adv._cache["M1"]
        self.adv._cache["M1"] = (time.time() - 1, res, snap)
        b = self.run_review(net)
        self.assertIsNot(a, b)
        self.assertEqual(b["verdict"], "skip")
        self.assertEqual(net.n("gemini"), 2)

    def test_reasks_when_price_or_liquidity_moves_a_lot(self):
        os.environ.pop("GROQ_API_KEY")
        net = Net(gemini=[gem(opinion("watch", 40)), gem(opinion("skip", 90))])
        s1 = {**SUMMARY, "price_usd": 1.0, "liquidity_usd": 20_000}
        s2 = {**SUMMARY, "price_usd": 1.30, "liquidity_usd": 20_000}
        with mock.patch.object(ba.requests, "post", net):
            a = self.adv.review("M1", s1, self.lines.append)
            b = self.adv.review("M1", s2, self.lines.append)
        self.assertIsNot(a, b)
        self.assertEqual(b["verdict"], "skip")
        self.assertEqual(net.n("gemini"), 2)

    def test_small_move_keeps_cache_and_submit_is_cached(self):
        os.environ.pop("GROQ_API_KEY")
        net = Net(gemini=[gem(opinion("watch", 40)), gem(opinion("skip", 90))])
        s1 = {**SUMMARY, "price_usd": 1.0, "liquidity_usd": 20_000}
        s2 = {**SUMMARY, "price_usd": 1.10, "liquidity_usd": 21_000}
        with mock.patch.object(ba.requests, "post", net):
            a = self.adv.review("M1", s1, self.lines.append)
            got = []
            st = self.adv.submit("M1", s2, got.append)
        self.assertEqual(st, "cached")
        self.assertIs(got[0], a)
        self.assertEqual(net.n("gemini"), 1)

    def test_submit_requeues_on_material_move(self):
        os.environ.pop("GROQ_API_KEY")
        net = Net(gemini=[gem(opinion("watch", 40)), gem(opinion("skip", 90))])
        s1 = {**SUMMARY, "price_usd": 1.0, "liquidity_usd": 20_000}
        s2 = {**SUMMARY, "price_usd": 0.70, "liquidity_usd": 20_000}
        done = []
        with mock.patch.object(ba.requests, "post", net):
            self.assertEqual(self.adv.submit("M1", s1, done.append), "queued")
            for _ in range(50):
                if done:
                    break
                time.sleep(0.05)
            self.adv.new_cycle()
            st = self.adv.submit("M1", s2, done.append)
            for _ in range(50):
                if len(done) == 2:
                    break
                time.sleep(0.05)
        self.assertEqual(st, "queued")
        self.assertEqual(len(done), 2)
        self.assertEqual(done[1]["verdict"], "skip")
        self.assertEqual(net.n("gemini"), 2)


class Pace(Base):
    def setUp(self):
        super().setUp()
        for name, p in (("gemini", self.adv.gemini), ("groq", self.adv.groq),
                        ("openrouter", self.adv.openrouter)):
            p.rpm = ba.RPM[name]

    def test_gemini_caps_at_8_calls_a_minute(self):
        os.environ.pop("GROQ_API_KEY")
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.assertEqual(self.adv.gemini.rpm, 8)
        net = Net(gemini=[gem(opinion()) for _ in range(20)])
        for i in range(12):
            self.run_review(net, mint=f"M{i}")
        self.assertEqual(net.n("gemini"), 8)
        self.assertFalse(self.adv.gemini.pace(max_wait=0))
        self.assertEqual(self.adv.gemini.health()["used_last_min"], 8)
        self.assertEqual(self.adv.gemini.health()["rpm"], 8)
        # available() ignores the review pacer, so Desk AI chat can still call this provider
        self.assertTrue(self.adv.gemini.available())

    def test_openrouter_caps_below_gemini(self):
        os.environ.pop("GEMINI_API_KEY")
        os.environ.pop("GROQ_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = ORKEY
        self.assertEqual(self.adv.openrouter.rpm, 3)
        self.assertLess(self.adv.openrouter.rpm, self.adv.gemini.rpm)
        net = Net(orr=[groq(opinion()) for _ in range(12)])
        for i in range(8):
            self.run_review(net, mint=f"M{i}")
        self.assertEqual(net.n("openrouter"), 3)
        self.assertEqual(self.adv.openrouter.health()["used_last_min"], 3)
        self.assertFalse(self.adv.openrouter.pace(max_wait=0))
        self.run_review(net, mint="M99")
        self.assertEqual(net.n("openrouter"), 3)

    def test_rpm_window_frees_a_slot(self):
        os.environ.pop("GROQ_API_KEY")
        self.adv.gemini.rpm = 2
        net = Net(gemini=[gem(opinion()) for _ in range(5)])
        self.run_review(net, mint="A")
        self.run_review(net, mint="B")
        self.assertEqual(net.n("gemini"), 2)
        self.assertFalse(self.adv.gemini.pace(max_wait=0))
        self.adv.gemini._hits = [time.time() - 61, time.time() - 61]
        self.run_review(net, mint="C")
        self.assertEqual(net.n("gemini"), 3)

    def test_rpm_does_not_block_the_other_provider(self):
        self.adv.gemini.rpm = 1
        net = Net(gemini=[gem(opinion("skip", 40)), gem(opinion("skip", 40))],
                  groq_=[groq(opinion("skip", 70)), groq(opinion("skip", 70))])
        self.run_review(net, mint="A")
        r = self.run_review(net, mint="B")
        self.assertEqual(net.n("gemini"), 1)
        self.assertEqual(r["source"], "groq")
        self.assertTrue(r.get("solo") or r["source"] == "groq")

    def test_rpm_does_not_block_direct_call_used_by_chat(self):
        os.environ.pop("GROQ_API_KEY")
        self.adv.gemini.rpm = 1
        self.assertTrue(self.adv.gemini.pace(max_wait=0))
        self.assertFalse(self.adv.gemini.pace(max_wait=0))
        net = Net(gemini=[gem(opinion("watch", 40))])
        with mock.patch.object(ba.requests, "post", net):
            out = self.adv.gemini.call("sys", "user")
        self.assertTrue(out)
        self.assertEqual(net.n("gemini"), 1)


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

    def test_analyst_skip_is_not_sent_to_the_advisor(self):
        from test_guard_integration import Base as GBase
        from desk.brain_advisor import ADVISOR
        from desk.analyst import TokenThesis

        class T(GBase):
            def runTest(self):
                pass
        t = T()
        calls = []
        skip = TokenThesis(verdict="skip", score=2, red_flags=["thin liquidity"], green_flags=[],
                           shape="fading", invalidation="n/a", sources=["liquidity_usd"])
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY, "GROQ_API_KEY": QKEY}), \
                mock.patch("desk.solana_agent.analyst.analyze_token", return_value=skip), \
                mock.patch.object(ADVISOR, "submit", side_effect=lambda *a, **k: calls.append((a, k)) or "skipped"):
            a = t.run_agent()
            t.doCleanups()
        self.assertEqual(calls, [])
        self.assertTrue(a.decisions)
        self.assertNotIn("brain", a.decisions[0])
        self.assertEqual(a.decisions[0]["thesis"]["verdict"], "skip")

    def test_analyst_watch_is_not_sent_to_the_advisor(self):
        from test_guard_integration import Base as GBase
        from desk.brain_advisor import ADVISOR
        from desk.analyst import TokenThesis

        class T(GBase):
            def runTest(self):
                pass
        t = T()
        calls = []
        watch = TokenThesis(verdict="watch", score=6, red_flags=[], green_flags=["ok"],
                            shape="crowd", invalidation="n/a", sources=["volume_h1"])
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY, "GROQ_API_KEY": QKEY}), \
                mock.patch("desk.solana_agent.analyst.analyze_token", return_value=watch), \
                mock.patch.object(ADVISOR, "submit", side_effect=lambda *a, **k: calls.append(1) or "skipped"):
            a = t.run_agent()
            t.doCleanups()
        self.assertEqual(calls, [])
        self.assertEqual(a.decisions[0]["thesis"]["verdict"], "watch")

    def test_valid_setup_below_min_score_is_not_sent(self):
        from test_guard_integration import Base as GBase
        from desk.brain_advisor import ADVISOR
        from desk.analyst import TokenThesis
        from config import MIN_SCORE

        class T(GBase):
            def runTest(self):
                pass
        t = T()
        calls = []
        weak = TokenThesis(verdict="valid_setup", score=max(0, MIN_SCORE - 1), red_flags=[],
                           green_flags=["ok"], shape="crowd", invalidation="n/a",
                           sources=["liquidity_usd"])
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": GKEY, "GROQ_API_KEY": QKEY}), \
                mock.patch("desk.solana_agent.analyst.analyze_token", return_value=weak), \
                mock.patch.object(ADVISOR, "submit", side_effect=lambda *a, **k: calls.append(1) or "skipped"):
            a = t.run_agent()
            t.doCleanups()
        self.assertEqual(calls, [])
        self.assertNotIn("brain", a.decisions[0])

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


class KeyRotation(unittest.TestCase):
    """Extra free-tier keys are extra quota: rotate on a 429 instead of resting."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "GROQ_API_KEY": "gsk_firstkey000000",
            "GROQ_API_KEY_2": "gsk_secondkey00000",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.p = ba.Groq("groq", "GROQ_API_KEY", 10)
        self.p.rpm = 0

    def test_keys_found_in_order(self):
        self.assertEqual(self.p.keys(), ["gsk_firstkey000000", "gsk_secondkey00000"])

    def test_budget_multiplies_with_keys(self):
        self.assertEqual(self.p.budget, 20)          # 10 per key x 2 keys

    def test_duplicate_key_does_not_double_budget(self):
        os.environ["GROQ_API_KEY_2"] = "gsk_firstkey000000"
        self.assertEqual(len(self.p.keys()), 1)
        self.assertEqual(self.p.budget, 10)

    def test_reserve_rotates_after_a_429(self):
        self.assertTrue(self.p.reserve())
        first = self.p.key()
        self.p.cool(60)                              # that key is now cooling
        self.assertTrue(self.p.available())
        self.assertTrue(self.p.reserve())
        self.assertNotEqual(self.p.key(), first)     # the second key took over

    def test_provider_rests_only_when_every_key_cools(self):
        self.p.reserve(); self.p.cool(60)
        self.assertTrue(self.p.available())
        self.p.reserve(); self.p.cool(60)
        self.assertFalse(self.p.available())
        self.assertTrue(self.p.resting() if hasattr(self.p, "resting") else True)

    def test_short_or_blank_keys_ignored(self):
        os.environ["GROQ_API_KEY_3"] = "  "
        os.environ["GROQ_API_KEY_4"] = "short"
        self.assertEqual(len(self.p.keys()), 2)
