"""The paper profile that was starving the live desk, and the two unknown-pass gates."""
from __future__ import annotations

import unittest

from desk import rulebook, settings as st


class PaperFlow(unittest.TestCase):
    def test_flow_moves_the_gates_once_and_can_be_restored(self):
        keys = list(st.PAPER_FLOW)
        before = {k: st.resolve(k, owner=True).get() for k in keys}
        saved = st._read(st._OVERRIDES, {})
        try:
            moved = st.apply_paper_flow()
            self.assertEqual(moved["scalp.min_mcap_usd"]["new"], 2500)
            self.assertEqual(st.resolve("scalp.min_mcap_usd", owner=True).get(), 2500)
            self.assertFalse(st.resolve("rulebook.entry_trigger_gate", owner=True).get())
            self.assertTrue(st.resolve("scalp.pool_change_entry", owner=True).get())
            self.assertEqual(st.resolve("scalp.entry_style", owner=True).get(), "moving_candle")
            self.assertEqual(st.apply_paper_flow(), {})
        finally:
            for k, v in before.items():
                st.resolve(k, owner=True).set(v)
            st._write(st._OVERRIDES, "operator_settings", saved if isinstance(saved, dict) else {})


class UnknownPasses(unittest.TestCase):
    def test_volume_unknown_passes_only_when_asked(self):
        rulebook.RULEBOOK["volume_rising_gate"] = True
        rulebook.RULEBOOK["vol_unknown_pass"] = False
        t = {"mint": "M", "volume_m5": 100, "source": "geckoterminal"}
        self.assertEqual(rulebook.volume_gate(t, [], 1_000_000), "vol_unknown")
        rulebook.RULEBOOK["vol_unknown_pass"] = True
        self.assertIsNone(rulebook.volume_gate(t, [], 1_000_000))

    def test_lp_unknown_passes_only_when_asked_and_a_known_unlock_still_fails(self):
        rulebook.RULEBOOK["lp_gate"] = True
        rulebook.RULEBOOK["lp_unknown_pass"] = False
        rulebook.RULEBOOK["min_lp_locked_pct"] = 0.5
        self.assertEqual(rulebook.lp_gate({"lp_locked_pct": None}), "lp_unknown")
        rulebook.RULEBOOK["lp_unknown_pass"] = True
        self.assertIsNone(rulebook.lp_gate({}))
        self.assertEqual(rulebook.lp_gate({"lp_locked_pct": 0.10}), "lp_unlocked")
