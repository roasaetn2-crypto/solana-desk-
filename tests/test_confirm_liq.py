"""entry.min_confirm_liq_ratio compares like for like (PAPER only).

The confirm quote's liquidity and the discovery token's liquidity are compared only when both are on the
same basis (DexScreener liquidity.usd vs Jupiter's `liquidity` are different numbers, about 2x apart on pump
pools). On a mismatch: one cheap same-basis read (cache / one budgeted Jupiter refresh), else the ratio check
is skipped for that candidate, with both values and sources logged and confirm_liq_mismatch counted.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from desk import scanner  # noqa: E402
from legacy_book import legacy_book  # noqa: E402
from test_merge_best import EndToEnd  # noqa: E402
from test_second_source import jup_row, put_row  # noqa: E402

DS, JUP = "dexscreener", "jupiter"
MINT = "REALMINT1"


def quote(liq, src, price=0.001):
    return {"price_usd": price, "liquidity_usd": liq, "volume_h6": 1, "volume_h24": 1,
            "liq_source": src, "at": time.time()}


@legacy_book
class Gate(unittest.TestCase):
    run_agent = EndToEnd.run_agent

    def setUp(self):
        scanner.reset_runtime()
        self.addCleanup(scanner.reset_runtime)
        self.assertEqual(config.ENTRY["min_confirm_liq_ratio"], 0.5)          # the gate and its value are as they were

    def infos(self, a, reason):
        return [e for e in a.events if e.get("reason") == reason]

    def test_same_basis_still_blocks_a_real_drop_and_passes_a_good_read(self):
        bad = self.run_agent(quote(5_000, DS), tok_source=DS)
        self.assertEqual((bad.stats["entries"], bad.reject_reasons["liq_unconfirmed"]), (0, 1))
        self.assertNotIn("confirm_liq_mismatch", bad.stats)
        good = self.run_agent(quote(39_000, DS), tok_source=DS)
        self.assertEqual(good.stats["entries"], 1)

    def test_jupiter_vs_jupiter_is_compared_too(self):
        a = self.run_agent(quote(5_000, JUP), tok_source="pumpportal")           # pumpportal rows are Jupiter's
        self.assertEqual(a.reject_reasons["liq_unconfirmed"], 1)

    def test_a_cross_source_half_is_not_a_failed_confirm(self):
        # scan 40k on DexScreener; the confirm read is Jupiter's ~half-size number: ratio 0.42 < 0.5 before
        a = self.run_agent(quote(17_000, JUP), tok_source=DS)
        self.assertEqual(a.stats["entries"], 1, [e["message"] for e in list(a.events)[:8]])
        self.assertEqual(a.stats["confirm_liq_mismatch"], 1)
        ev = self.infos(a, "confirm_liq_mismatch")
        self.assertEqual(len(ev), 1)
        self.assertIn("40,000 [dexscreener]", ev[0]["message"])
        self.assertIn("17,000 [jupiter]", ev[0]["message"])

    def test_the_other_direction_is_skipped_too(self):
        a = self.run_agent(quote(60_000, DS), tok_source=JUP)
        self.assertEqual(a.stats["entries"], 1)
        self.assertEqual(a.stats["confirm_liq_mismatch"], 1)

    def test_geckoterminal_and_untagged_reads_are_skipped_not_blocked(self):
        gt = self.run_agent(quote(17_000, DS), tok_source="geckoterminal")
        self.assertEqual((gt.stats["entries"], gt.stats["confirm_liq_mismatch"]), (1, 1))
        raw = self.run_agent({"price_usd": 0.001, "liquidity_usd": 17_000, "volume_h6": 1, "volume_h24": 1},
                             tok_source=DS)
        self.assertEqual((raw.stats["entries"], raw.stats["confirm_liq_mismatch"]), (1, 1))

    def test_a_same_basis_read_is_used_when_there_is_one(self):
        with mock.patch.object(scanner, "same_basis_quote", return_value=quote(39_000, DS)) as alt:
            ok = self.run_agent(quote(17_000, JUP), tok_source=DS)
        alt.assert_called_once()
        self.assertEqual(alt.call_args.args[:2], (MINT, DS))
        self.assertEqual(ok.stats["entries"], 1)
        self.assertEqual(ok.stats["confirm_liq_alt"], 1)
        self.assertNotIn("confirm_liq_mismatch", ok.stats)
        with mock.patch.object(scanner, "same_basis_quote", return_value=quote(5_000, DS)):
            thin = self.run_agent(quote(17_000, JUP), tok_source=DS)
        self.assertEqual((thin.stats["entries"], thin.reject_reasons["liq_unconfirmed"]), (0, 1))

    def test_a_failing_lookup_never_breaks_the_entry(self):
        with mock.patch.object(scanner, "same_basis_quote", side_effect=RuntimeError("boom")):
            a = self.run_agent(quote(17_000, JUP), tok_source=DS)
        self.assertEqual((a.stats["entries"], a.stats["confirm_liq_mismatch"]), (1, 1))

    def test_ticket_is_still_capped_by_the_confirm_liquidity(self):
        a = self.run_agent(quote(17_000, JUP), tok_source=DS)
        self.assertLessEqual(a.paper.s["positions"][MINT]["cost"], config.RISK["max_pool_share"] * 17_000 + 0.01)

    def test_paper_only(self):
        src = Path(sys.modules["desk.solana_agent"].__file__).read_text()
        i = src.index("scanner.same_basis_quote(")
        self.assertIn('if self.mode != "live":\n                    cq = scanner.quote(', src[max(0, i - 2500):i])


class SameBasisQuote(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()
        self.addCleanup(scanner.reset_runtime)
        self.now = time.time()

    def pair(self, liq=39_000):
        return {"chainId": "solana", "baseToken": {"address": MINT}, "priceUsd": "0.001",
                "liquidity": {"usd": liq}, "volume": {}}

    def test_dexscreener_comes_from_the_pair_cache_within_the_window(self):
        scanner._remember_pairs([self.pair()], self.now - 100)
        s = mock.Mock()
        q = scanner.same_basis_quote(MINT, DS, s, self.now)
        self.assertEqual((q["liq_source"], q["liquidity_usd"]), (DS, 39_000))
        s.get.assert_not_called()                                              # never a DexScreener call
        scanner._remember_pairs([self.pair()], self.now - config.LAST_GOOD["liq_max_age_s"] - 1)
        self.assertIsNone(scanner.same_basis_quote(MINT, DS, s, self.now))
        s.get.assert_not_called()

    def test_jupiter_uses_a_recent_row_without_a_call(self):
        put_row(jup_row(mint=MINT, price=0.001, liquidity=17_000), at=self.now - 120)
        s = mock.Mock()
        q = scanner.same_basis_quote(MINT, JUP, s, self.now)
        self.assertEqual((q["liq_source"], q["liquidity_usd"]), (JUP, 17_000))
        s.get.assert_not_called()

    def test_jupiter_refresh_respects_the_cooldown(self):
        scanner._HOST_COOL["jupiter"] = {"until": self.now + 60, "strikes": 1}
        s = mock.Mock()
        with mock.patch("desk.netutil.offline", return_value=False):
            self.assertIsNone(scanner.same_basis_quote(MINT, JUP, s, self.now))
        s.get.assert_not_called()

    def test_jupiter_refresh_goes_through_the_budgeted_fetch(self):
        with mock.patch.object(scanner, "_jup_quotes", return_value={MINT: {"liq_source": JUP}}) as jq, \
                mock.patch.object(scanner, "_jup_enabled", return_value=True):
            q = scanner.same_basis_quote(MINT, JUP, mock.Mock(), self.now)
            none = scanner.same_basis_quote(MINT, JUP, mock.Mock(), self.now, fetch=False)
        self.assertEqual(q, {"liq_source": JUP})
        self.assertIsNone(none)
        jq.assert_called_once()

    def test_other_bases_have_no_cheap_read(self):
        for b in ("geckoterminal", "demo", "unknown"):
            self.assertIsNone(scanner.same_basis_quote(MINT, b, mock.Mock(), self.now))


if __name__ == "__main__":
    unittest.main()
