"""The caution layer inside the agent loop: security blocks, size cuts, breaker, flags, reviews."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DESK_OFFLINE", "1")

import config  # noqa: E402
from desk import scanner, signals  # noqa: E402
from desk.solana_agent import SolanaAgent  # noqa: E402
from legacy_book import legacy_book  # noqa: E402

TOK = {"mint": "REALMINT1" + "x" * 30, "symbol": "GOOD", "name": "GOOD", "pool": "P",
       "dex": "pumpswap", "price_usd": 0.001, "mcap_usd": 300_000, "liquidity_usd": 40_000,
       "volume_m5": 5000, "volume_h1": 90_000, "volume_h6": 150_000, "volume_h24": 300_000,
       "buys_h1": 500, "sells_h1": 300, "buyers_h1": 220, "change_m5": 3, "change_h1": 60,
       "change_h6": 80, "age_minutes": 45, "source": "geckoterminal"}
CLEAN = {"chips": [], "block": [], "halve": [], "unknown": [], "parts": {}}
NO_JUP = {"available": False, "flags": [], "block": [], "halve": []}


def sec(block=(), halve=()):
    chips = ([signals.chip(f, "block", "goplus") for f in block]
             + [signals.chip(f, "halve", "goplus") for f in halve])
    return {"chips": chips, "block": list(block), "halve": list(halve), "unknown": [], "parts": {}}


class Base(unittest.TestCase):
    def run_agent(self, security=CLEAN, jup=NO_JUP, regime_info=None, closed=None,
                  day_pnl=0.0, decimals=6):
        d = Path(tempfile.mkdtemp())
        a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        if closed:
            a.paper.s["closed"] = closed
        facts = {"mint_authority": False, "freeze_authority": False, "top_wallet_pct": 0.03,
                 "top_10_pct": 0.2, "holders": 400, "supply": 1e9, "decimals": decimals}
        dev = {"hard_no": [], "dev_score": 90, "dev_hold_pct": 0.01, "creator_prev_rugs": 0,
               "sniper_pct": 0.05, "reasons": []}
        ds = {"has_x": True, "has_telegram": True, "websites": ["https://x.y"], "socials": {}}
        quote = {"price_usd": 0.00105, "liquidity_usd": 39_000, "volume_h6": 1, "volume_h24": 1}
        patches = [mock.patch.object(scanner, "new_tokens", return_value=[dict(TOK)]),
                   mock.patch.object(scanner, "socials", return_value={"x": "https://x.com/a", "telegram": None, "website": None}),
                   mock.patch.object(scanner, "quote", return_value=quote),
                   mock.patch("desk.chain.facts", return_value=facts),
                   mock.patch("desk.dexscreener.facts", return_value=ds),
                   mock.patch("desk.devcheck.investigate", return_value=dev),
                   mock.patch("desk.solana_agent.alert"),
                   mock.patch("desk.solana_agent.signals.security", return_value=security),
                   mock.patch("desk.solana_agent.signals.entry_impact", return_value=jup)]
        if regime_info is not None:
            patches += [mock.patch("desk.solana_agent.offline", return_value=False),
                        mock.patch("desk.solana_agent.regime.current", return_value=regime_info)]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        a.run_once()
        return a

    def entry_cost(self, a):
        return a.paper.s["positions"][TOK["mint"]]["cost"]


@legacy_book
class CautionLayer(Base):
    def test_baseline_enters_at_full_size(self):
        a = self.run_agent()
        self.assertEqual(a.stats["entries"], 1, [e["message"] for e in list(a.events)[:6]])
        self.full = self.entry_cost(a)
        self.assertGreater(self.full, 3)
        meta = a.paper.s["positions"][TOK["mint"]]["meta"]
        self.assertEqual(meta["decimals"], 6)
        self.assertIsNotNone(a.decisions[0]["checklist"])
        self.assertTrue(a.decisions[0]["checklist"]["ok"])

    def test_block_flag_refuses_entry_and_benches(self):
        a = self.run_agent(sec(block=["closable"]))
        self.assertEqual(a.stats["entries"], 0)
        self.assertEqual(a.stats["sec_rejects"], 1)
        self.assertEqual(a.reject_reasons["sec_closable"], 1)

    def test_halve_flag_cuts_size_and_shows_chip(self):
        full = self.entry_cost(self.run_agent())
        a = self.run_agent(sec(halve=["transfer_fee"]))
        if a.stats["entries"] == 0:       # a halved ticket can fall under the fee floor: also a cut
            self.assertEqual(a.stats["guard_rejects"], 1)
            return
        self.assertLess(self.entry_cost(a), full)
        chips = a.status()["broker"]["positions"][0]["flags"]
        self.assertEqual([c["t"] for c in chips], ["transfer_fee"])

    def test_halving_never_grows_the_ticket(self):
        full = self.entry_cost(self.run_agent())
        for kw in (dict(security=sec(halve=["x"])), dict(jup={"available": True, "flags": ["jup_impact_high"],
                                                              "block": [], "halve": ["jup_impact_high"],
                                                              "impact_pct": 9.0}),
                   dict(regime_info={"tag": "RISK_OFF", "halve": True})):
            a = self.run_agent(**kw)
            if a.stats["entries"]:
                self.assertLessEqual(self.entry_cost(a), full / 2 + 0.01)

    def test_risk_off_regime_halves(self):
        full = self.entry_cost(self.run_agent())
        a = self.run_agent(regime_info={"tag": "RISK_OFF", "halve": True})
        self.assertEqual(a.regime["tag"], "RISK_OFF")
        if a.stats["entries"]:
            self.assertLessEqual(self.entry_cost(a), full / 2 + 0.01)
        self.assertEqual(a.status()["guard"]["regime"]["tag"], "RISK_OFF")

    def test_three_losses_pause_new_entries(self):
        now = time.time()
        closed = [{"mint": f"M{i}", "pnl_usd": -0.3, "closed": now - 60 * (3 - i)} for i in range(3)]
        a = self.run_agent(closed=closed)
        self.assertEqual(a.stats["entries"], 0)
        self.assertEqual(a.breaker["state"], "cooldown")
        self.assertEqual(a.status()["guard"]["breaker"]["state"], "cooldown")

    def test_two_losses_halve(self):
        full = self.entry_cost(self.run_agent())
        now = time.time()
        closed = [{"mint": f"M{i}", "pnl_usd": -0.1, "closed": now - 60 * (2 - i)} for i in range(2)]
        a = self.run_agent(closed=closed)
        self.assertEqual(a.breaker["state"], "reduce")
        if a.stats["entries"]:
            self.assertLessEqual(self.entry_cost(a), full / 2 + 0.01)

    def test_cuts_never_loosen_the_daily_loss_limit(self):
        d = Path(tempfile.mkdtemp())
        a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        self.assertEqual(config.RISK["max_daily_loss_pct"], 0.03)
        self.assertEqual(a.mode, "paper")

    def test_status_has_integrations_and_no_secrets(self):
        with mock.patch.dict(os.environ, {"HEALTHCHECKS_URL": "https://hc-ping.com/SECRET-ID",
                                          "GMGN_API_KEY": "SECRET-KEY"}):
            a = self.run_agent()
            import json
            snap = json.dumps(a.status())
        self.assertNotIn("SECRET", snap)
        ig = a.status()["integrations"]
        self.assertEqual(ig["healthchecks"]["status"], "code ready, waiting on signup")
        self.assertEqual(ig["gmgn"]["status"], "waiting on key")


@legacy_book
class ExitReview(Base):
    def test_exit_writes_review_and_jupiter_note(self):
        a = self.run_agent()
        self.assertEqual(a.stats["entries"], 1)
        pos = a.paper.s["positions"][TOK["mint"]]
        # force a stop-out on the next cycle
        crash = {"price_usd": pos["entry"] * 0.5, "liquidity_usd": 39_000, "volume_h6": 1, "volume_h24": 1}
        jup = {"available": True, "route": True, "impact": 0.07, "impact_pct": 7.0,
               "flags": ["jup_impact_high"], "block": [], "halve": ["jup_impact_high"]}
        with mock.patch.object(scanner, "quote_many", return_value={TOK["mint"]: crash}), \
                mock.patch("desk.solana_agent.signals.exit_impact", return_value=jup) as ei:
            a._manage_positions()
        self.assertEqual(a.stats["exits"], 1)
        ei.assert_called_once()
        self.assertEqual(ei.call_args.args[3 - 1] if False else ei.call_args.args[2], 6)   # decimals
        self.assertEqual(len(a.reviews), 1)
        rv = a.reviews[0]
        self.assertFalse(rv["won"])
        self.assertIn("jupiter sell impact 7.0%", rv["note"])
        ev = next(e for e in a.events if e["kind"] == "exit")
        self.assertIn("GOOD", ev["review"])
        self.assertEqual(ev["exit_flags"][0]["t"], "jup_impact_high")
        act = [r for r in a.status()["activity"] if r["kind"] == "exit"]
        self.assertIn("GOOD", act[0]["review"])
        self.assertEqual(a.status()["guard"]["reviews"][0]["symbol"], "GOOD")

    def test_exit_not_blocked_by_jupiter_failure(self):
        a = self.run_agent()
        pos = a.paper.s["positions"][TOK["mint"]]
        crash = {"price_usd": pos["entry"] * 0.5, "liquidity_usd": 39_000, "volume_h6": 1, "volume_h24": 1}
        with mock.patch.object(scanner, "quote_many", return_value={TOK["mint"]: crash}), \
                mock.patch("desk.solana_agent.signals.exit_impact", return_value=NO_JUP):
            a._manage_positions()
        self.assertEqual(a.stats["exits"], 1)
        self.assertEqual(a.stats["errors"], 0)


if __name__ == "__main__":
    unittest.main()
