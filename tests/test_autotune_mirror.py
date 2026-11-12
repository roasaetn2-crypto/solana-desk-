"""The self-tune mirror rule: a healthy win rate tightens back the tuner's own loosening.

Guarantees under test:
  * a gate the tuner loosened is tightened back toward the shipped default on a good win rate
  * a gate the OPERATOR set (tuner never wrote it) is never touched
  * the tuner never tightens past the shipped default
  * a losing book still tightens (the original rule is unchanged)
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import config
from desk.autotune import AutoTune, DEFAULTS, get_gate, looser_than_default, set_gate


class _Broker:
    def __init__(self, pnls):
        self.s = {"closed": [{"mint": f"M{i}", "pnl_usd": p} for i, p in enumerate(pnls)]}


class _Agent:
    mode = "paper"
    started_at = 0
    last_entry_ts = 0
    paused_tune = False
    reject_reasons: dict = {}

    def __init__(self, pnls):
        self.broker = _Broker(pnls)

    def emit(self, *a, **k):
        pass


class Mirror(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._orig = {n: get_gate(n) for n in DEFAULTS}

    def tearDown(self):
        for n, v in self._orig.items():
            set_gate(n, v)

    def _tune(self, pnls, tuned=None, tag="t", idle_h=0.0):
        agent = _Agent(pnls)
        t = AutoTune(agent, state_path=self.dir / f"{tag}.json",
                     log_path=self.dir / f"{tag}.jsonl")
        t.tuned = dict(tuned or {})
        t.last_run = 0
        # pin the idle clock so the idle-loosen rule cannot mask the win-rate rules
        agent.last_entry_ts = 10 ** 9 - idle_h * 3600
        return t

    def test_healthy_win_rate_tightens_back_its_own_loosening(self):
        set_gate("score", 5)                      # tuner had loosened 6 -> 5
        rec = self._tune([1.0] * 6, {"score": 5}).tick(now=10 ** 9)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["gate"], "score")
        self.assertEqual(rec["dir"], "tighten")
        self.assertEqual(get_gate("score"), 6)    # back at the shipped default

    def test_operator_set_gate_is_left_alone(self):
        set_gate("score", 4)                      # operator chose 4; tuner never wrote it
        self.assertTrue(looser_than_default("score"))
        rec = self._tune([1.0] * 6, {}).tick(now=10 ** 9)
        self.assertIsNone(rec)
        self.assertEqual(get_gate("score"), 4)

    def test_never_tighter_than_default(self):
        # a gate already at the default must not be pushed tighter by the mirror rule
        set_gate("score", DEFAULTS["score"])
        self.assertFalse(looser_than_default("score"))
        self.assertIsNone(self._tune([1.0] * 6, {}).tick(now=10 ** 9))
        self.assertEqual(get_gate("score"), DEFAULTS["score"])

    def test_losing_book_still_tightens(self):
        rec = self._tune([-1.0] * 6).tick(now=10 ** 9)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["dir"], "tighten")
        self.assertLess(rec["win_rate"], config.AUTOTUNE["tighten_below_win_rate"])

    def test_win_rate_between_thresholds_does_nothing(self):
        # 3/6 = 50%: above the tighten line, below the tighten-back line, and NOT idle
        set_gate("score", 5)
        rec = self._tune([1.0, 1.0, 1.0, -1.0, -1.0, -1.0], {"score": 5}, idle_h=0).tick(now=10 ** 9)
        self.assertIsNone(rec)
        self.assertEqual(get_gate("score"), 5)


if __name__ == "__main__":
    unittest.main()
