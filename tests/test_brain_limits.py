"""Usage/rate limits: one bot limited -> the next provider steps in or the other goes solo;
all limited -> no AI opinion (the rule-based analyst carries on). Advisory only."""
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_brain_advisor import Base, Net, Resp, gem, groq, opinion, ORKEY  # noqa: E402
from desk import brain_advisor as ba  # noqa: E402


def orr(text):
    return Resp(200, {"choices": [{"message": {"content": text}}]})


class OneLimited(Base):
    def test_gemini_429_groq_solo_and_note(self):
        net = Net(gemini=[Resp(429, headers={"Retry-After": "90"})], groq_=[groq(opinion("skip", 70))])
        r = self.run_review(net)
        self.assertEqual(r["source"], "groq")
        self.assertTrue(r["solo"])
        self.assertEqual(r["limited"], ["gemini"])
        self.assertEqual(r["limit_note"], "Gemini out of quota, Groq solo")
        self.assertGreater(self.adv.gemini.cooldown_until, time.time() + 60)   # cooled, not hammered

    def test_limited_bot_replaced_by_openrouter_keeps_two_voices(self):
        os.environ["OPENROUTER_API_KEY"] = ORKEY
        net = Net(gemini=[Resp(429)], groq_=[groq(opinion("skip", 70))], orr=[orr(opinion("skip", 60))])
        r = self.run_review(net)
        self.assertEqual(r["providers"], ["groq", "openrouter"])
        self.assertEqual(r["agreement"], "agree")
        self.assertEqual(r["limit_note"], "Gemini out of quota, Groq, Openrouter talking")
        self.assertEqual(net.n("gemini"), 1)

    def test_quota_text_counts_as_limit(self):
        net = Net(gemini=[Resp(403, text="RESOURCE_EXHAUSTED: quota exceeded")], groq_=[groq(opinion("watch", 50))])
        r = self.run_review(net)
        self.assertEqual(r["limited"], ["gemini"])
        self.assertFalse(self.adv.gemini.available())

    def test_cooling_provider_not_called_next_review(self):
        net = Net(gemini=[Resp(429)], groq_=[groq(opinion("skip", 70)), groq(opinion("skip", 70))])
        self.run_review(net, mint="A")
        self.run_review(net, mint="B")
        self.assertEqual(net.n("gemini"), 1)


class AllLimited(Base):
    def test_all_limited_returns_none_and_resting(self):
        net = Net(gemini=[Resp(429)], groq_=[Resp(429)])
        self.assertIsNone(self.run_review(net))
        self.assertTrue(self.adv.resting())
        self.assertTrue(self.adv.status()["resting"])
        self.assertTrue(any("resting" in l for l in self.lines))
        # nothing is called while everyone is cooling
        net2 = Net(gemini=[gem(opinion("buy", 90))], groq_=[groq(opinion("buy", 90))])
        self.assertIsNone(self.run_review(net2, mint="Z"))
        self.assertEqual(len(net2.calls), 0)

    def test_budget_exhausted_is_resting(self):
        for p in self.adv.providers():
            p.used = p.budget
        self.assertTrue(self.adv.resting())
        self.assertIsNone(self.run_review(Net()))

    def test_no_keys_is_not_resting(self):
        os.environ.pop("GEMINI_API_KEY"); os.environ.pop("GROQ_API_KEY")
        self.assertFalse(self.adv.resting())

    def test_rule_based_analyst_still_decides(self):
        """With every bot limited the agent's own scoring still runs; no brain on the record."""
        from test_guard_integration import Base as GBase
        from desk.brain_advisor import ADVISOR

        class T(GBase):
            def runTest(self):
                pass
        t = T()
        t.setUp()
        try:
            with mock.patch.object(ADVISOR, "review", return_value=None):
                ADVISOR._cache.clear()
                a = t.run_agent()
            self.assertTrue(a.decisions)
            for d in a.decisions:
                self.assertNotIn("verdict", d.get("brain") or {})   # no AI opinion on any record
            self.assertIn("thesis", a.decisions[0])
        finally:
            t.tearDown()
            t.doCleanups()
