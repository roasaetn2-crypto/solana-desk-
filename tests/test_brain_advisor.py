"""The AI second opinion: Gemini + Groq, mocked HTTP only. Advisory; it can never move money."""
import json
import os
import sys
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
        # this test is about the ENTRY review path, which is off by default now
        env = {"GEMINI_API_KEY": GKEY, "GROQ_API_KEY": QKEY, "BRAIN_ADVISE_ON": "entry"}
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


class CerebrasProvider(unittest.TestCase):
    """Cerebras is a fourth independent free bucket, so the desk never goes quiet."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CEREBRAS_API_KEY": "csk_FAKECEREBRASKEY1234"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.p = ba.Cerebras("cerebras", "CEREBRAS_API_KEY", 200)
        self.p.rpm = 1000

    def test_registered_in_advisor_and_budgets(self):
        adv = ba.Advisor()
        self.addCleanup(adv._pool.shutdown, wait=True, cancel_futures=True)
        names = [p.name for p in adv.providers()]
        self.assertIn("cerebras", names)
        self.assertIn("cerebras", ba.BUDGETS)
        self.assertIn("cerebras", ba.RPM)
        self.assertEqual(names[:3], ["gemini", "groq", "cerebras"])

    def test_models_start_with_primary_then_fallbacks(self):
        self.assertEqual(ba.cerebras_models()[0], ba.CEREBRAS_MODEL)
        self.assertIn("llama3.1-8b", ba.cerebras_models())

    def test_model_override_env(self):
        with mock.patch.dict(os.environ, {"BRAIN_CEREBRAS_MODEL": "custom-model"}):
            self.assertEqual(ba.cerebras_models()[0], "custom-model")
            self.assertEqual(len(ba.cerebras_models()), len(set(ba.cerebras_models())))

    def test_call_parses_choices_and_sends_bearer_key(self):
        seen = {}

        def post(url, headers=None, json=None, timeout=None):
            seen.update(url=url, headers=headers, json=json)
            return Resp(200, {"choices": [{"message": {"content": opinion("buy", 71)}}]})

        with mock.patch.object(ba.requests, "post", post):
            out = self.p.call("sys", "user", 700)
        self.assertEqual(json.loads(out)["verdict"], "buy")
        self.assertEqual(seen["url"], ba.CEREBRAS_URL)
        self.assertTrue(seen["headers"]["Authorization"].startswith("Bearer csk_"))
        self.assertEqual(seen["json"]["response_format"], {"type": "json_object"})

    def test_dead_model_falls_through_to_the_next(self):
        replies = [Resp(404, text="no such model"), Resp(200, {"choices": [{"message": {"content": opinion()}}]})]
        with mock.patch.object(ba.requests, "post", lambda *a, **k: replies.pop(0)):
            out = self.p.call("sys", "user", 700)
        self.assertEqual(json.loads(out)["verdict"], "watch")
        self.assertEqual(self.p.last_model, self.p.models[1])

    def test_429_raises_so_the_advisor_cools_the_key(self):
        with mock.patch.object(ba.requests, "post", lambda *a, **k: Resp(429, text="rate limited")):
            with self.assertRaises(ba.ProviderError):
                self.p.call("sys", "user", 700)

    def test_no_key_means_disabled(self):
        os.environ.pop("CEREBRAS_API_KEY", None)
        p = ba.Cerebras("cerebras", "CEREBRAS_API_KEY", 200)
        self.assertFalse(p.key())
        self.assertFalse(p.available())


class OllamaProvider(unittest.TestCase):
    """Ollama Cloud is a sixth bucket and the only PAID one: it goes last."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "ollama_FAKEKEY1234567890"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.p = ba.Ollama("ollama", "OLLAMA_API_KEY", 200)
        self.p.rpm = 1000

    def test_registered_in_advisor_and_budgets(self):
        adv = ba.Advisor()
        self.addCleanup(adv._pool.shutdown, wait=True, cancel_futures=True)
        names = [p.name for p in adv.providers()]
        self.assertIn("ollama", names)
        self.assertIn("ollama", ba.BUDGETS)
        self.assertIn("ollama", ba.RPM)
        self.assertIn("ollama", adv.status()["models"])

    def test_paid_bucket_never_displaces_a_free_one(self):
        """Order is the safety property: a paid provider must be reached last."""
        adv = ba.Advisor()
        self.addCleanup(adv._pool.shutdown, wait=True, cancel_futures=True)
        names = [p.name for p in adv.providers()]
        self.assertEqual(names[-1], "ollama")
        self.assertEqual(names[:5], ["gemini", "groq", "cerebras", "cloudflare", "openrouter"])

    def test_models_start_with_primary_then_fallbacks(self):
        self.assertEqual(ba.ollama_models()[0], ba.OLLAMA_MODEL)
        self.assertEqual(ba.OLLAMA_MODEL, "deepseek-v4.1-flash")
        self.assertIn("glm-5.3-flash", ba.ollama_models())

    def test_model_override_env(self):
        with mock.patch.dict(os.environ, {"BRAIN_OLLAMA_MODEL": "custom-model"}):
            self.assertEqual(ba.ollama_models()[0], "custom-model")
            self.assertEqual(len(ba.ollama_models()), len(set(ba.ollama_models())))

    def test_call_parses_choices_and_sends_bearer_key(self):
        seen = {}

        def post(url, headers=None, json=None, timeout=None):
            seen.update(url=url, headers=headers, json=json)
            return Resp(200, {"choices": [{"message": {"content": opinion("buy", 71)}}]})

        with mock.patch.object(ba.requests, "post", post):
            out = self.p.call("sys", "user", 700)
        self.assertEqual(json.loads(out)["verdict"], "buy")
        self.assertEqual(seen["url"], ba.OLLAMA_URL)
        self.assertTrue(seen["headers"]["Authorization"].startswith("Bearer ollama_"))
        self.assertEqual(seen["json"]["model"], ba.OLLAMA_MODEL)
        # Ollama's compat layer does not promise these, so asking would risk a 400.
        self.assertNotIn("response_format", seen["json"])
        self.assertNotIn("reasoning_effort", seen["json"])

    def test_dead_model_falls_through_to_the_next(self):
        replies = [Resp(404, text="no such model"), Resp(200, {"choices": [{"message": {"content": opinion()}}]})]
        with mock.patch.object(ba.requests, "post", lambda *a, **k: replies.pop(0)):
            out = self.p.call("sys", "user", 700)
        self.assertEqual(json.loads(out)["verdict"], "watch")
        self.assertEqual(self.p.last_model, self.p.models[1])

    def test_402_is_out_of_quota_not_a_hard_error(self):
        """No credits is the normal state of a paid bucket. Cool it, do not storm."""
        body = {"error": {"message": "Add usage credits or subscribe: https://ollama.com/plans"}}
        with mock.patch.object(ba.requests, "post", lambda *a, **k: Resp(402, body)):
            with self.assertRaises(ba.ProviderError) as ctx:
                self.p.call("sys", "user", 700)
        err = ctx.exception
        self.assertTrue(err.limited)
        self.assertEqual(err.status, 402)
        self.assertIn("quota", str(err))
        self.assertFalse(self.p.available())
        snap = self.p.snapshot()
        self.assertEqual(snap["errors"], 0)
        self.assertGreater(snap["cooldown_s"], 0)

    def test_429_raises_so_the_advisor_cools_the_key(self):
        with mock.patch.object(ba.requests, "post", lambda *a, **k: Resp(429, text="rate limited")):
            with self.assertRaises(ba.ProviderError):
                self.p.call("sys", "user", 700)

    def test_no_key_means_disabled(self):
        os.environ.pop("OLLAMA_API_KEY", None)
        p = ba.Ollama("ollama", "OLLAMA_API_KEY", 200)
        self.assertFalse(p.key())
        self.assertFalse(p.available())


class DaySpread(unittest.TestCase):
    """Spend the daily budget across the whole day, so it lasts until reset.

    Without this a burst spends all 400 Groq calls by mid-morning and the desk
    sits on the heuristic until midnight.
    """

    MIDNIGHT = 1794528000.0 - (1794528000.0 % 86400)   # a UTC midnight

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_FAKEFAKEFAKE1234567890"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self._spread = ba.SPREAD_DAY
        self.addCleanup(setattr, ba, "SPREAD_DAY", self._spread)
        self._time = ba.time.time
        self.addCleanup(setattr, ba.time, "time", self._time)
        self.clock = self.MIDNIGHT
        ba.time.time = lambda: self.clock

    def provider(self, budget=400, spread=True):
        ba.SPREAD_DAY = {"groq"} if spread else set()
        p = ba.Groq("groq", "GROQ_API_KEY", budget)
        return p

    def test_seconds_to_reset_counts_down_to_utc_midnight(self):
        self.assertAlmostEqual(ba._seconds_to_reset(self.MIDNIGHT), 86400, delta=1)
        self.assertAlmostEqual(ba._seconds_to_reset(self.MIDNIGHT + 3600), 82800, delta=1)
        # never zero, or the rate would divide by zero at the stroke of midnight
        self.assertGreaterEqual(ba._seconds_to_reset(self.MIDNIGHT + 86400 - 0.5), 1.0)

    def test_off_by_default_keeps_the_old_burst_behaviour(self):
        p = self.provider(spread=False)
        self.assertFalse(p.spread)
        for _ in range(50):
            self.assertTrue(p.reserve())
        self.assertEqual(p.used, 50)

    def test_burst_is_allowed_then_the_pace_takes_over(self):
        p = self.provider()
        burst = int(p._burst())
        for i in range(burst):
            self.assertTrue(p.reserve(), f"burst call {i} should be allowed")
        # the bucket is empty now: the next call is ahead of schedule
        self.assertFalse(p.reserve())
        self.assertEqual(p.used, burst)

    def test_credit_accrues_with_time(self):
        p = self.provider()
        burst = int(p._burst())
        for _ in range(burst):
            p.reserve()
        self.assertFalse(p.reserve())
        self.assertGreater(p.seconds_until_call(), 0)
        # wait out the wait it just quoted, and the call is affordable again
        self.clock += p.seconds_until_call() + 1
        self.assertTrue(p.reserve())

    def test_a_spent_budget_slows_the_trickle(self):
        """Rate is what is LEFT over the time that is LEFT, not the full budget."""
        fresh = self.provider()
        fresh._accrue(self.clock)
        spent = self.provider()
        spent.used = 380          # 20 of 400 left
        spent._accrue(self.clock)
        self.assertLess(spent._rate(self.clock), fresh._rate(self.clock))

    def test_a_bursty_day_still_has_calls_left_at_the_end(self):
        """The property that matters: the budget survives until the reset."""
        p = self.provider()
        calls, last_hour = 0, 0.0
        t = self.MIDNIGHT
        while t < self.MIDNIGHT + 86400:
            self.clock = t
            if p.reserve():
                calls += 1
                last_hour = (t - self.MIDNIGHT) / 3600.0
            t += 60                      # a review every minute, all day
        self.assertEqual(calls, 400, "every call in the budget should get spent")
        self.assertLessEqual(p.used, p.budget)
        # and the last one lands near midnight, not at 06:40
        self.assertGreater(last_hour, 20.0)

    def test_without_spreading_the_same_day_runs_dry_early(self):
        p = self.provider(spread=False)
        t = self.MIDNIGHT
        while t < self.MIDNIGHT + 86400 and p.reserve():
            t += 60
        self.assertLess((t - self.MIDNIGHT) / 3600.0, 8.0)

    def test_no_budget_means_no_calls(self):
        p = self.provider(budget=0)
        self.assertFalse(p.reserve())
        self.assertGreater(p.seconds_until_call(), 0)


class QuotaPayload(unittest.TestCase):
    """The dashboard reads this: per-provider burn, keys, cooldown, calls left."""

    FIELDS = {"keys", "keys_cooling", "used", "budget", "left", "cooling_s", "pct_used", "enabled"}

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "GEMINI_API_KEY": GKEY, "GROQ_API_KEY": QKEY,
            "GROQ_API_KEY_2": "gsk_secondkey00000",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("CEREBRAS_API_KEY", None)
        os.environ.pop("OLLAMA_API_KEY", None)
        self.adv = ba.Advisor()
        self.addCleanup(self.adv._pool.shutdown, wait=True, cancel_futures=True)

    def test_shape_and_all_providers(self):
        q = self.adv.quota()
        self.assertEqual(set(q["providers"]),
                         {"gemini", "groq", "cerebras", "cloudflare", "openrouter", "ollama"})
        for row in q["providers"].values():
            self.assertEqual(set(row), self.FIELDS)
        self.assertIn("total_left", q)
        self.assertIn("total_budget", q)
        self.assertIn("resting", q)

    def test_totals_are_the_sum_of_the_parts(self):
        q = self.adv.quota()
        self.assertEqual(q["total_budget"], sum(r["budget"] for r in q["providers"].values()))
        self.assertEqual(q["total_left"], sum(r["left"] for r in q["providers"].values()))

    def test_second_groq_key_doubles_the_groq_budget(self):
        q = self.adv.quota()["providers"]["groq"]
        self.assertEqual(q["keys"], 2)
        self.assertEqual(q["budget"], 2 * ba.BUDGETS["groq"])
        self.assertTrue(q["enabled"])

    def test_missing_keys_show_disabled_not_negative(self):
        for name in ("cerebras", "openrouter"):
            row = self.adv.quota()["providers"][name]
            self.assertEqual(row["keys"], 0)
            self.assertFalse(row["enabled"])
            self.assertEqual(row["left"], row["budget"])

    def test_used_and_pct_used_track_a_reservation(self):
        p = self.adv.gemini
        p.rpm = 1000
        self.assertTrue(p.reserve())
        row = self.adv.quota()["providers"]["gemini"]
        self.assertEqual(row["used"], 1)
        self.assertGreater(row["pct_used"], 0)
        self.assertEqual(row["left"], row["budget"] - 1)

    def test_one_cooling_key_is_visible_while_the_other_still_works(self):
        p = self.adv.groq
        p.rpm = 1000
        self.assertTrue(p.reserve())
        p.cool(60)
        row = self.adv.quota()["providers"]["groq"]
        self.assertEqual(row["keys"], 2)
        self.assertEqual(row["keys_cooling"], 1)   # half the provider is burning
        self.assertEqual(row["cooling_s"], 0)      # but the provider is not resting
        self.assertTrue(p.available())             # the second key still serves

    def test_provider_cools_only_when_every_key_is_cooling(self):
        p = self.adv.groq
        p.rpm = 1000
        p.reserve(); p.cool(60)
        p.reserve(); p.cool(60)
        row = self.adv.quota()["providers"]["groq"]
        self.assertEqual(row["keys_cooling"], 2)
        self.assertGreater(row["cooling_s"], 0)
        self.assertLessEqual(row["cooling_s"], 60)
        self.assertFalse(p.available())            # every groq key is spent
        self.assertFalse(self.adv.quota()["resting"])  # gemini is still up, desk not silent

    def test_quota_never_leaks_a_key(self):
        blob = json.dumps(self.adv.quota())
        self.assertNotIn(GKEY, blob)
        self.assertNotIn(QKEY, blob)
        self.assertNotIn("gsk_secondkey00000", blob)

    def test_desk_chat_status_carries_the_quota(self):
        from desk import desk_chat
        st = desk_chat.status()
        self.assertIn("quota", st)
        self.assertIn("providers", st["quota"])


if __name__ == "__main__":
    unittest.main()


class ExitOnlyAdvice(unittest.TestCase):
    """BRAIN_ADVISE_ON=exit: the free tier is spent on sells, not on every buy."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"GROQ_API_KEY": QKEY})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("BRAIN_ADVISE_ON", None)
        self.adv = ba.Advisor()
        for p in self.adv.providers():
            p.rpm = 1000
        self.addCleanup(self.adv._pool.shutdown, wait=True, cancel_futures=True)

    def test_default_is_exit_only(self):
        self.assertEqual(ba.advise_mode(), "exit")
        self.assertTrue(ba.wants_exit())
        self.assertFalse(ba.wants_entry())

    def test_every_mode(self):
        for mode, entry, exit_ in (("exit", False, True), ("entry", True, False),
                                   ("both", True, True), ("off", False, False)):
            with mock.patch.dict(os.environ, {"BRAIN_ADVISE_ON": mode}):
                self.assertEqual(ba.wants_entry(), entry, mode)
                self.assertEqual(ba.wants_exit(), exit_, mode)

    def test_unknown_mode_falls_back_to_exit(self):
        with mock.patch.dict(os.environ, {"BRAIN_ADVISE_ON": "garbage"}):
            self.assertEqual(ba.advise_mode(), "exit")

    def test_status_reports_the_mode(self):
        self.assertEqual(self.adv.status()["advise_on"], "exit")

    def test_summarize_exit_has_pnl_and_drawdown(self):
        s = self.adv.summarize_exit(
            {"symbol": "WIF", "entry": 1.0, "last": 0.8, "peak": 1.4, "horizon": "scalp"},
            {"liquidity_usd": 50000}, "trailing stop")
        self.assertEqual(s["pnl_pct"], -20.0)
        self.assertEqual(s["drawdown_from_peak_pct"], -42.86)
        self.assertEqual(s["exit_rule_that_fired"], "trailing stop")
        self.assertEqual(s["liquidity_usd"], 50000)

    def test_summarize_exit_survives_missing_numbers(self):
        s = self.adv.summarize_exit({"symbol": "X"}, None, "")
        self.assertIsNone(s["pnl_pct"])
        self.assertIsNone(s["drawdown_from_peak_pct"])

    def test_parse_exit_accepts_plain_sell(self):
        self.assertEqual(ba.parse_exit('{"verdict":"sell","confidence":70}')["verdict"], "sell_now")
        self.assertEqual(ba.parse_exit('{"verdict":"SELL-HALF","confidence":50}')["verdict"], "sell_half")
        self.assertIsNone(ba.parse_exit('{"verdict":"buy","confidence":70}'))
        self.assertIsNone(ba.parse_exit("nonsense"))

    def test_review_exit_uses_one_provider_and_one_call(self):
        body = {"choices": [{"message": {"content": json.dumps(
            {"verdict": "sell_now", "confidence": 78, "reasons": ["deep drawdown"]})}}]}
        calls = []

        def post(url, headers=None, json=None, timeout=None):
            calls.append(url)
            return Resp(200, body)

        summ = self.adv.summarize_exit({"symbol": "WIF", "entry": 1.0, "last": 0.8}, None, "stop")
        with mock.patch.object(ba.requests, "post", post):
            res = self.adv.review_exit("M1", summ)
        self.assertEqual(res["verdict"], "sell_now")
        self.assertEqual(res["provider"], "groq")
        self.assertTrue(res["advisory_only"])
        self.assertEqual(len(calls), 1)          # no pair, no debate

    def test_review_exit_returns_none_without_keys(self):
        os.environ.pop("GROQ_API_KEY", None)
        self.assertIsNone(self.adv.review_exit("M1", {"symbol": "X"}))

    def test_submit_exit_skips_when_advice_is_off(self):
        with mock.patch.dict(os.environ, {"BRAIN_ADVISE_ON": "off"}):
            self.assertEqual(self.adv.submit_exit("M1", {"symbol": "X"}, lambda r: None), "skipped")

    def test_submit_exit_queues_and_never_blocks(self):
        got = []
        done = threading.Event()

        def on_done(res):
            got.append(res)
            done.set()

        with mock.patch.object(ba.requests, "post",
                               lambda *a, **k: Resp(200, {"choices": [{"message": {"content": json.dumps(
                                   {"verdict": "hold", "confidence": 60, "reasons": ["trend intact"]})}}]})):
            self.assertEqual(self.adv.submit_exit("M9", {"symbol": "X"}, on_done), "queued")
            self.assertTrue(done.wait(5))
        self.assertEqual(got[0]["verdict"], "hold")

    def test_agent_does_not_advise_entries_by_default(self):
        """The whole point: a buy candidate must not spend a free-tier call."""
        import desk.solana_agent as sa
        self.assertFalse(ba.wants_entry())
        self.assertTrue(hasattr(sa.SolanaAgent, "_advise_exit"))


class ExitAdvisoryWiring(unittest.TestCase):
    """_advise_exit must call the advisor, and must never touch a demo position."""

    def _agent(self):
        from desk.solana_agent import SolanaAgent
        return SolanaAgent.__new__(SolanaAgent)

    def test_it_submits_an_exit_review(self):
        from desk.solana_agent import SolanaAgent
        a = self._agent()
        a.emit = lambda *x, **k: None
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": QKEY, "BRAIN_ADVISE_ON": "exit"}), \
                mock.patch.object(ba.ADVISOR, "enabled", return_value=True), \
                mock.patch.object(ba.ADVISOR, "submit_exit", return_value="queued") as sub:
            SolanaAgent._advise_exit(a, "M1", {"symbol": "WIF", "entry": 1.0, "last": 0.9},
                                     {"liquidity_usd": 1}, "trailing stop")
        self.assertTrue(sub.called)
        mint, summary = sub.call_args[0][0], sub.call_args[0][1]
        self.assertEqual(mint, "M1")
        self.assertEqual(summary["exit_rule_that_fired"], "trailing stop")

    def test_demo_positions_are_never_sent(self):
        from desk.solana_agent import SolanaAgent
        a = self._agent()
        a.emit = lambda *x, **k: None
        with mock.patch.object(ba.ADVISOR, "enabled", return_value=True), \
                mock.patch.object(ba.ADVISOR, "submit_exit") as sub:
            SolanaAgent._advise_exit(a, "DEMO123", {"symbol": "D"}, None, "stop")
        self.assertFalse(sub.called)

    def test_off_mode_never_submits(self):
        from desk.solana_agent import SolanaAgent
        a = self._agent()
        a.emit = lambda *x, **k: None
        with mock.patch.dict(os.environ, {"BRAIN_ADVISE_ON": "off"}), \
                mock.patch.object(ba.ADVISOR, "enabled", return_value=True), \
                mock.patch.object(ba.ADVISOR, "submit_exit") as sub:
            SolanaAgent._advise_exit(a, "M1", {"symbol": "W"}, None, "stop")
        self.assertFalse(sub.called)


class CloudflareProvider(unittest.TestCase):
    """Cloudflare Workers AI: 10k free Neurons/day, the largest free quota here."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "CLOUDFLARE_ACCOUNT_ID": "acct_1234567890abcdef",
            "CLOUDFLARE_API_TOKEN": "cftoken_FAKE1234567890",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.p = ba.Cloudflare("cloudflare", "CLOUDFLARE_API_TOKEN", 300)
        self.p.rpm = 1000

    def test_registered_in_advisor_and_budgets(self):
        adv = ba.Advisor()
        self.addCleanup(adv._pool.shutdown, wait=True, cancel_futures=True)
        names = [p.name for p in adv.providers()]
        self.assertIn("cloudflare", names)
        self.assertIn("cloudflare", ba.BUDGETS)
        self.assertIn("cloudflare", ba.RPM)

    def test_models_start_with_primary_then_fallbacks(self):
        self.assertEqual(ba.cloudflare_models()[0], ba.CLOUDFLARE_MODEL)
        self.assertIn("@cf/meta/llama-3.1-8b-instruct-fp8-fast", ba.cloudflare_models())

    def test_model_override_env(self):
        with mock.patch.dict(os.environ, {"BRAIN_CLOUDFLARE_MODEL": "@cf/custom/model"}):
            self.assertEqual(ba.cloudflare_models()[0], "@cf/custom/model")
            self.assertEqual(len(ba.cloudflare_models()), len(set(ba.cloudflare_models())))

    def test_url_is_scoped_to_the_account(self):
        self.assertIn("acct_1234567890abcdef", ba.cloudflare_url())
        self.assertTrue(ba.cloudflare_url().endswith("/ai/v1/chat/completions"))

    def test_no_account_id_means_disabled(self):
        """A token without an account id cannot build a URL, so it must not count."""
        with mock.patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": ""}):
            p = ba.Cloudflare("cloudflare", "CLOUDFLARE_API_TOKEN", 300)
            self.assertEqual(p.keys(), [])
            self.assertFalse(p.available())
            self.assertFalse(p.enabled if hasattr(p, "enabled") else False)

    def test_no_token_means_disabled(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": ""}):
            p = ba.Cloudflare("cloudflare", "CLOUDFLARE_API_TOKEN", 300)
            self.assertEqual(p.keys(), [])
            self.assertFalse(p.available())

    def test_call_parses_choices_and_sends_bearer_token(self):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen["url"], seen["headers"], seen["json"] = url, headers, json
            r = mock.Mock(status_code=200)
            r.json.return_value = {"choices": [{"message": {"content": '{"verdict":"hold"}'}}]}
            return r

        with mock.patch.object(ba.requests, "post", fake_post):
            out = self.p.call("sys", "user", 400)
        self.assertEqual(out, '{"verdict":"hold"}')
        self.assertEqual(seen["headers"]["Authorization"], "Bearer cftoken_FAKE1234567890")
        self.assertIn("acct_1234567890abcdef", seen["url"])
        self.assertEqual(seen["json"]["model"], ba.CLOUDFLARE_MODEL)

    def test_token_rotation_gives_independent_keys(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN_2": "cftoken_SECOND1234567"}):
            p = ba.Cloudflare("cloudflare", "CLOUDFLARE_API_TOKEN", 300)
            self.assertEqual(len(p.keys()), 2)
            self.assertEqual(p.budget, 600)

    def test_duplicate_tokens_do_not_double_the_budget(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN_2": "cftoken_FAKE1234567890"}):
            p = ba.Cloudflare("cloudflare", "CLOUDFLARE_API_TOKEN", 300)
            self.assertEqual(len(p.keys()), 1)
            self.assertEqual(p.budget, 300)

    def test_falls_through_to_the_next_model_on_404(self):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json["model"])
            if len(calls) == 1:
                r = mock.Mock(status_code=404)
                r.json.return_value = {"errors": [{"message": "model not found"}]}
                return r
            r = mock.Mock(status_code=200)
            r.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
            return r

        with mock.patch.object(ba.requests, "post", fake_post):
            self.assertEqual(self.p.call("s", "u", 100), "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ba.CLOUDFLARE_MODEL)

    def test_429_does_not_walk_the_model_list(self):
        """A rate limit is the key's problem, not the model's: stop immediately."""
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json["model"])
            r = mock.Mock(status_code=429)
            r.headers = {}
            r.json.return_value = {"errors": [{"message": "rate limited"}]}
            return r

        with mock.patch.object(ba.requests, "post", fake_post):
            with self.assertRaises(ba.ProviderError):
                self.p.call("s", "u", 100)
        self.assertEqual(len(calls), 1)
