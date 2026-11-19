"""Liquidity-drop exit compares like for like (PAPER only).

Bug: entry_liq came from DexScreener's two-sided liquidity.usd, while the exit quote (DexScreener 429 ->
Jupiter fallback) carried Jupiter's `liquidity`, about half of it on pump pools. A -5% price move then looked
like "liquidity -57% since entry" and sold the position within a minute or two. Now the entry and the exit
read must come from the same source, a breach needs two distinct reads, and a >=90% drop on the same source
still exits at once.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_rulebook as tr  # noqa: E402
from config import LIQ_EXIT, RULEBOOK  # noqa: E402
from desk import rulebook, scanner  # noqa: E402
from legacy_book import legacy_book  # noqa: E402
from test_second_source import jup_row, put_row  # noqa: E402

DS, JUP = "dexscreener", "jupiter"


def pos(**kw):
    return {"entry_liq": 5_472.0, "entry_liq_src": DS, "entry_liqs": {DS: 5_472.0}, **kw}


def q(liq, src, at):
    return {"price_usd": 0.001, "liquidity_usd": liq, "liq_source": src, "at": at}


class Tags(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()
        self.addCleanup(scanner.reset_runtime)

    def test_quote_builders_name_their_source(self):
        pair = {"chainId": "solana", "priceUsd": "0.001", "liquidity": {"usd": 9000}, "volume": {}}
        self.assertEqual(scanner._quote_from_pairs([pair], at=5.0)["liq_source"], DS)
        self.assertEqual(scanner._quote_from_pairs([pair], at=5.0)["at"], 5.0)
        jq = scanner._quote_from_jup(jup_row(mint="M" * 40, price=0.001, liquidity=4500), at=7.0)
        self.assertEqual((jq["liq_source"], jq["at"], jq["liquidity_usd"]), (JUP, 7.0, 4500))
        self.assertEqual(scanner.demo_quote("DEMO1", 1.0)["liq_source"], "demo")

    def test_basis_per_discovery_source(self):
        self.assertEqual([scanner.liq_basis(s) for s in ("dexscreener", "jupiter", "pumpportal", "geckoterminal",
                                                          "demo", None, "x")],
                         [DS, JUP, JUP, "geckoterminal", "demo", "unknown", "unknown"])

    def test_jupiter_fallback_quote_is_tagged_with_the_row_time(self):
        m = "JQ" + "x" * 38
        put_row(jup_row(mint=m, price=0.001, liquidity=2500), at=time.time() - 3)
        out = scanner._jup_quotes(mock.Mock(), [m], time.time())
        self.assertEqual(out[m]["liq_source"], JUP)
        self.assertLess(out[m]["at"], time.time() - 2)

    def test_cached_pairs_keep_the_pair_cache_time(self):
        m = "DQ" + "x" * 38
        pair = {"chainId": "solana", "baseToken": {"address": m}, "priceUsd": "0.001",
                "liquidity": {"usd": 9000}, "volume": {}}
        scanner._remember_pairs([pair], time.time() - 8)
        got = scanner.quote_many([m], mock.Mock())[m]
        self.assertEqual(got["liq_source"], DS)
        self.assertLess(got["at"], time.time() - 5)


class LikeForLike(unittest.TestCase):
    def setUp(self):
        tr.on()

    def test_wktt_mismatch_is_not_a_drop(self):
        # entry 5,472 on DexScreener; the exit read is Jupiter's ~half-size number (-57%), price only -5%
        p = pos()
        for at in (1, 2, 3):
            r = rulebook.liq_check(p, q(2_350, JUP, at))
            self.assertEqual(r["status"], "mismatch")
            self.assertIsNone(r["reason"])
        self.assertIn("sources differ", r["note"])
        self.assertEqual((r["entry_src"], r["now_src"]), (DS, JUP))
        self.assertEqual(rulebook.liq_exit(p, q(2_350, JUP, 4))[0], None)
        self.assertNotIn("liq_breach", p)

    def test_same_source_breach_needs_two_distinct_reads(self):
        p = pos()
        r1 = rulebook.liq_check(p, q(2_300, DS, 10))
        self.assertEqual((r1["status"], r1["reads"]), ("breach", 1))
        self.assertIsNone(r1["reason"])
        again = rulebook.liq_check(p, q(2_300, DS, 10))                 # the cached read again: still 1
        self.assertEqual((again["status"], again["reads"]), ("breach", 1))
        r2 = rulebook.liq_check(p, q(2_300, DS, 11))
        self.assertEqual(r2["status"], "exit")
        self.assertEqual(r2["reason"], "rulebook: liquidity -58% since entry")
        self.assertEqual((r2["entry"], r2["now"], r2["entry_src"], r2["now_src"]), (5_472.0, 2_300, DS, DS))

    def test_a_recovered_read_resets_the_streak(self):
        p = pos()
        rulebook.liq_check(p, q(2_300, DS, 1))
        self.assertEqual(rulebook.liq_check(p, q(5_400, DS, 2))["status"], "ok")
        self.assertEqual(rulebook.liq_check(p, q(2_300, DS, 3))["reads"], 1)

    def test_a_mismatched_tick_neither_counts_nor_resets(self):
        p = pos()
        rulebook.liq_check(p, q(2_300, DS, 1))
        self.assertEqual(rulebook.liq_check(p, q(900, JUP, 2))["status"], "mismatch")
        self.assertEqual(rulebook.liq_check(p, q(2_300, DS, 3))["status"], "exit")

    def test_a_rug_on_the_same_source_exits_on_one_read(self):
        r = rulebook.liq_check(pos(), q(300, DS, 1))                     # -95%
        self.assertEqual(r["status"], "exit")
        self.assertEqual(r["reads"], 1)
        self.assertEqual(LIQ_EXIT["instant_drop"], 0.90)
        edge = rulebook.liq_check(pos(), q(5_472 * 0.1, DS, 1))          # exactly -90%
        self.assertEqual(edge["status"], "exit")
        self.assertEqual(rulebook.liq_check(pos(), q(5_472 * 0.11, DS, 1))["status"], "breach")

    def test_a_rug_seen_only_on_the_other_source_does_not_exit(self):
        self.assertEqual(rulebook.liq_check(pos(), q(10, JUP, 1))["status"], "mismatch")

    def test_each_source_has_its_own_entry_reading(self):
        p = pos(entry_liqs={DS: 5_472.0, JUP: 2_700.0})
        self.assertEqual(rulebook.liq_check(p, q(2_650, JUP, 1))["status"], "ok")      # -2% on Jupiter
        r = rulebook.liq_check(p, q(1_000, JUP, 2))
        self.assertEqual((r["status"], r["entry"], r["entry_src"]), ("breach", 2_700.0, JUP))

    def test_positions_without_a_recorded_source_are_skipped_not_sold(self):
        legacy = {"entry_liq": 5_472.0}
        self.assertEqual(rulebook.liq_check(legacy, q(2_300, DS, 1))["status"], "mismatch")
        self.assertEqual(rulebook.liq_check(pos(), {"liquidity_usd": 2_300})["status"], "mismatch")  # untagged quote

    def test_entry_src_alone_is_enough(self):
        p = {"entry_liq": 5_472.0, "entry_liq_src": DS}
        self.assertEqual(rulebook.liq_check(p, q(2_300, DS, 1))["status"], "breach")

    def test_missing_data_never_exits(self):
        self.assertEqual(rulebook.liq_check(pos(), None)["status"], "missing")
        self.assertEqual(rulebook.liq_check({}, q(1, DS, 1))["status"], "missing")
        self.assertIsNone(rulebook.liq_exit({}, q(1, DS, 1))[0])

    def test_the_switch_and_the_threshold_are_unchanged(self):
        self.assertEqual(rulebook.liq_check(pos(), q(10, DS, 1))["status"], "exit")
        with mock.patch.dict(RULEBOOK, {"exit_on_liq_drop": False}):
            self.assertEqual(rulebook.liq_check(pos(), q(10, DS, 1))["status"], "off")
        with mock.patch.dict(RULEBOOK, {"liq_drop_exit": 0.6}):                 # Trader's Confirmed value
            self.assertEqual(rulebook.liq_check(pos(), q(5_472 * 0.5, DS, 1))["status"], "ok")      # -50% < 60%
            self.assertEqual(rulebook.liq_check(pos(), q(5_472 * 0.35, DS, 1))["status"], "breach")  # -65%
        self.assertEqual(LIQ_EXIT["confirm_reads"], 2)


@legacy_book
class AgentWiring(unittest.TestCase):
    run_agent = tr.AgentWiring.run_agent
    skips = tr.AgentWiring.skips
    _held = tr.AgentWiring._held
    _manage = tr.AgentWiring._manage

    def setUp(self):
        tr.on()
        scanner.reset_runtime()
        self.addCleanup(scanner.reset_runtime)

    SNAP = {"available": True, "creator_hold_pct": 0.04, "top_10_pct": 0.30}

    def liq_events(self, a):
        return [e for e in a.events if e.get("stage") == "liq_exit"]

    def test_entry_records_the_source_of_each_reading(self):
        a = self.run_agent()          # the harness confirm quote carries no tag: only the discovery reading gets a source
        pos_ = a.paper.s["positions"][tr.TOK["mint"]]
        self.assertEqual(pos_["entry_liq"], 39_000)
        self.assertIsNone(pos_["entry_liq_src"])                 # an untagged reading is never given a source
        self.assertEqual(pos_["entry_liqs"], {"geckoterminal": 40_000})

    def test_a_tagged_confirm_quote_is_recorded_beside_the_discovery_reading(self):
        src = Path(sys.modules["desk.solana_agent"].__file__).read_text()
        i = src.index('"entry_liqs": _liqs')
        self.assertIn('_liqs[cq["liq_source"]] = _cq_l', src[max(0, i - 900):i])
        self.assertIn('_liqs.setdefault(_t_b, t["liquidity_usd"])', src[max(0, i - 900):i])

    def test_a_mismatched_exit_read_is_logged_with_both_values_and_sources_and_holds(self):
        a = self._held()
        self._manage(a, 17_000, self.SNAP, src=JUP)             # 17k Jupiter vs 40k DexScreener entry: -57%
        self.assertIn(tr.TOK["mint"], a.paper.s["positions"])
        ev = self.liq_events(a)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["reason"], "liq_mismatch")
        self.assertIn("40,000 [dexscreener]", ev[0]["message"])
        self.assertIn("17,000 [jupiter]", ev[0]["message"])
        self._manage(a, 17_000, self.SNAP, src=JUP)             # same mismatch again: logged once
        self.assertEqual(len(self.liq_events(a)), 1)
        self.assertEqual(a.stats["liq_mismatch"], 1)

    def test_a_real_same_source_drop_exits_after_two_reads_and_logs_both(self):
        a = self._held()
        self._manage(a, 12_000, self.SNAP, at=1.0)               # -70% (default 0.2 or Confirmed 0.6)
        self.assertIn(tr.TOK["mint"], a.paper.s["positions"])
        self._manage(a, 12_000, self.SNAP, at=2.0)
        self.assertNotIn(tr.TOK["mint"], a.paper.s["positions"])
        self.assertIn("liquidity -70%", a.paper.s["closed"][-1]["reason"])
        msgs = [e["message"] for e in self.liq_events(a)]
        self.assertTrue(any("breach" in m and "reads 1" in m for m in msgs), msgs)
        self.assertTrue(any(m.startswith("liq: $RULE exit") and "40,000 [dexscreener]" in m
                            and "12,000 [dexscreener]" in m and "reads 2" in m for m in msgs), msgs)

    def test_a_rug_exits_on_one_read(self):
        a = self._held()
        self._manage(a, 1_000, self.SNAP, at=1.0)               # -97%
        self.assertNotIn(tr.TOK["mint"], a.paper.s["positions"])

    def test_the_exit_stays_paper_only(self):
        src = Path(sys.modules["desk.solana_agent"].__file__).read_text()
        i = src.index("self._rulebook_exit(")
        self.assertIn('self.mode != "live"', src[max(0, i - 400):i])


if __name__ == "__main__":
    unittest.main()
