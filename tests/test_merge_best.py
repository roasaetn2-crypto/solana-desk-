"""MERGE tests: scalp/swing exits, ladder, compounding, entry confirm, autotune floors."""
import sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from desk import exits, autotune, scanner
from desk.broker import PaperBroker
from desk.risk import Account, check_token_trade
from desk.solana_agent import SolanaAgent
from desk import analyst
from legacy_book import legacy_book  # noqa: E402


def q(price, h6=1000, h24=4000):
    return {"price_usd": price, "liquidity_usd": 1e5, "volume_h6": h6, "volume_h24": h24}


def thesis(**kw):
    base = dict(verdict="valid_setup", score=8, red_flags=[], green_flags=["x"], shape="crowd",
                invalidation="x", sources=["buyers_h1"])
    base.update(kw)
    return analyst.TokenThesis(**base)


class Exits(unittest.TestCase):
    def mk(self, horizon):
        b = PaperBroker(Path(tempfile.mkdtemp()) / "l.json")
        b.buy("M", "T", 100, 1.0, horizon=horizon)
        return b, b.s["positions"]["M"]

    def test_horizon_by_age(self):
        self.assertEqual(exits.horizon_for(30), "scalp")
        self.assertEqual(exits.horizon_for(7 * 60), "swing")

    def test_scalp_stop_tighter_than_swing(self):
        b, p = self.mk("scalp"); e = p["entry"]
        self.assertEqual(b.exit_signal("M", q(e * 0.79))[0], 1.0)
        b, p = self.mk("swing"); e = p["entry"]
        self.assertIsNone(b.exit_signal("M", q(e * 0.79)))
        self.assertEqual(b.exit_signal("M", q(e * 0.69))[0], 1.0)

    def test_ladder_then_breakeven_stop_and_trail(self):
        b, p = self.mk("scalp"); e = p["entry"]
        f, why = b.exit_signal("M", q(e * 1.55)); self.assertEqual((f, why[:3]), (0.33, "TP1"))
        b.sell("M", f, e * 1.55, why)
        f, why = b.exit_signal("M", q(e * 2.05)); self.assertEqual((f, why[:3]), (0.5, "TP2"))
        b.sell("M", f, e * 2.05, why)
        # remainder trails 20% from the 2.05x peak
        self.assertIsNone(b.exit_signal("M", q(e * 1.8)))
        f, why = b.exit_signal("M", q(e * 1.6)); self.assertEqual(f, 1.0); self.assertIn("trail", why)

    def test_breakeven_stop_after_tp1(self):
        b, p = self.mk("scalp"); e = p["entry"]
        f, why = b.exit_signal("M", q(e * 1.5)); b.sell("M", f, e * 1.5, why)
        self.assertGreaterEqual(p["stop_price"], e)
        f, why = b.exit_signal("M", q(e * 0.99)); self.assertEqual(f, 1.0); self.assertIn("breakeven", why)

    def test_stop_never_moves_down(self):
        b, p = self.mk("scalp"); s0 = p["stop_price"]
        b.exit_signal("M", q(p["entry"] * 1.1)); b.exit_signal("M", q(p["entry"] * 0.9))
        self.assertEqual(p["stop_price"], s0)

    def test_one_missing_quote_does_not_close(self):
        b, p = self.mk("scalp")
        self.assertIsNone(b.exit_signal("M", None)); self.assertIsNone(b.exit_signal("M", None))
        f, why = b.exit_signal("M", None); self.assertEqual(f, 1.0)
        b, p = self.mk("scalp"); b.exit_signal("M", None); b.exit_signal("M", q(1.02)); b.exit_signal("M", None)
        self.assertEqual(p["miss"], 1)

    def test_scalp_timeout_only_when_not_up(self):
        b, p = self.mk("scalp"); e = p["entry"]; late = time.time() + 3.1 * 3600
        self.assertIn("timeout", b.exit_signal("M", q(e * 1.0), now=late)[1])
        b, p = self.mk("scalp"); e = p["entry"]
        self.assertIsNone(b.exit_signal("M", q(e * 1.2), now=late))

    def test_legacy_position_still_works(self):
        b, p = self.mk("swing")
        for k in ("stage", "horizon", "peak", "stop_price", "miss", "stop_pct"): p.pop(k)
        self.assertEqual(b.exit_signal("M", q(p["entry"] * 0.5))[0], 1.0)


class Compounding(unittest.TestCase):
    def test_size_scales_with_realized_equity_not_unrealized(self):
        base = Account(equity=1000, day_pnl=0, open_positions=0, realized_equity=1000)
        up = Account(equity=1500, day_pnl=0, open_positions=0, realized_equity=1500)
        unreal = Account(equity=1500, day_pnl=0, open_positions=0, realized_equity=1000)
        t = lambda a: check_token_trade(thesis(), a, 1e6, stop_pct=0.2)[1]
        self.assertGreater(t(up), t(base)); self.assertEqual(t(unreal), t(base))

    def test_caps_and_pool_share_still_bind(self):
        big = Account(equity=1e6, day_pnl=0, open_positions=0, realized_equity=1e6)
        ok, ticket, _ = check_token_trade(thesis(), big, 10_000, stop_pct=0.2)
        self.assertEqual(ticket, config.RISK["max_pool_share"] * 10_000)

    def test_losses_shrink_size_and_daily_stop_holds(self):
        down = Account(equity=900, day_pnl=-40, open_positions=0, realized_equity=900)
        self.assertIn("daily loss", check_token_trade(thesis(), down, 1e6)[2])

    def test_realized_equity_from_ledger(self):
        b = PaperBroker(Path(tempfile.mkdtemp()) / "l.json")
        b.s["closed"] = [{"pnl_usd": 30.0}, {"pnl_usd": -10.0}]
        self.assertEqual(b.realized_equity(), config.PAPER["starting_equity_usd"] + 20)


class Gates(unittest.TestCase):
    def test_chase_gate(self):
        t = {"mint": "M", "age_minutes": 45, "liquidity_usd": 30_000, "volume_h1": 60_000,
             "mcap_usd": 200_000, "buys_h1": 400, "sells_h1": 250, "change_h1": 400}
        self.assertEqual(scanner.free_kill(t), "chase")
        t["change_h1"] = 100
        self.assertIsNone(scanner.free_kill(t))


class AutoTuneTests(unittest.TestCase):
    def setUp(self):
        self.saved = {"v": config.ANALYST_VALID_SCORE, "m": config.MIN_SCORE,
                      "scan": dict(config.SCAN), "chain": dict(config.CHAIN)}
        self.d = Path(tempfile.mkdtemp())
        self.a = SolanaAgent(ledger=self.d / "p.json", bench_path=self.d / "b.db", live_ledger=self.d / "l.json")
        self.a.autotune = autotune.AutoTune(self.a, self.d / "at.json", self.d / "at.jsonl")

    def tearDown(self):
        config.ANALYST_VALID_SCORE, config.MIN_SCORE = self.saved["v"], self.saved["m"]
        config.SCAN.update(self.saved["scan"]); config.CHAIN.update(self.saved["chain"])

    def test_idle_loosens_most_blocking_gate_one_notch_and_logs(self):
        self.a.reject_reasons.update({"volume": 9, "trades": 2})
        config.SCAN["min_volume_h1_usd"] = 8_000      # the retuned default (3k) already sits on the floor
        v0 = config.SCAN["min_volume_h1_usd"]
        now = time.time() + 3 * 3600
        rec = self.a.autotune.tick(now)
        self.assertEqual((rec["gate"], rec["dir"]), ("volume_h1", "loosen"))
        self.assertEqual(config.SCAN["min_volume_h1_usd"], v0 - 1000)
        self.assertTrue((self.d / "at.jsonl").read_text().strip())

    def test_never_passes_hard_floors(self):
        for _ in range(100):
            for g in config.AUTOTUNE["gates"]:
                autotune.notch(g, True)
        self.assertGreaterEqual(config.ANALYST_VALID_SCORE, 4)
        self.assertGreaterEqual(config.SCAN["min_volume_h1_usd"], 3_000)
        self.assertGreaterEqual(config.SCAN["min_trades_h1"], 30)
        self.assertLessEqual(config.CHAIN["max_top_10"], 0.45)
        for _ in range(100):
            for g in config.AUTOTUNE["gates"]:
                autotune.notch(g, False)
        self.assertLessEqual(config.ANALYST_VALID_SCORE, 8)
        self.assertLessEqual(config.SCAN["min_volume_h1_usd"], 20_000)
        self.assertGreaterEqual(config.CHAIN["max_top_10"], 0.25)

    def test_bad_win_rate_tightens(self):
        self.a.paper.s["closed"] = [{"mint": f"M{i}", "pnl_usd": -5.0} for i in range(7)]
        rec = self.a.autotune.tick(time.time() + 31 * 60)
        self.assertEqual(rec["dir"], "tighten")

    def test_does_not_run_off_cycle_or_in_live(self):
        self.assertIsNone(self.a.autotune.tick(time.time() + 60))

    def test_does_not_touch_risk_or_scam_kills(self):
        r, ch = dict(config.RISK), dict(config.CHAIN)
        self.a.reject_reasons.update({"volume": 3})
        self.a.autotune.tick(time.time() + 3 * 3600)
        self.assertEqual(r, config.RISK)
        for k in ("max_top_wallet", "allow_mint_authority", "allow_freeze_authority"):
            self.assertEqual(ch[k], config.CHAIN[k])

    def test_demo_pool_never_fills(self):
        self.assertTrue(config.AGENT["demo_when_empty"] is False)


if __name__ == "__main__":
    unittest.main()


@legacy_book
class EndToEnd(unittest.TestCase):
    """Mocked network: one good pool goes scan -> confirm -> paper buy -> ladder exit."""
    def run_agent(self, quote):
        d = Path(tempfile.mkdtemp())
        a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        tok = {"mint": "REALMINT1", "symbol": "GOOD", "name": "GOOD", "pool": "P", "dex": "pumpswap",
               "price_usd": 0.001, "mcap_usd": 300_000, "liquidity_usd": 40_000, "volume_m5": 5000,
               "volume_h1": 90_000, "volume_h6": 150_000, "volume_h24": 300_000, "buys_h1": 500,
               "sells_h1": 300, "buyers_h1": 220, "change_m5": 3, "change_h1": 60, "change_h6": 80,
               "age_minutes": 45, "source": "geckoterminal"}
        facts = {"mint_authority": False, "freeze_authority": False, "top_wallet_pct": 0.03,
                 "top_10_pct": 0.2, "holders": 400, "supply": 1e9}
        dev = {"hard_no": [], "dev_score": 90, "dev_hold_pct": 0.01, "creator_prev_rugs": 0,
               "sniper_pct": 0.05, "reasons": []}
        ds = {"has_x": True, "has_telegram": True, "websites": ["https://x.y"], "socials": {}}
        patches = [mock.patch.object(scanner, "new_tokens", return_value=[tok]),
                   mock.patch.object(scanner, "socials", return_value={"x": "https://x.com/a", "telegram": None, "website": None}),
                   mock.patch.object(scanner, "quote", return_value=quote),
                   mock.patch("desk.chain.facts", return_value=facts),
                   mock.patch("desk.dexscreener.facts", return_value=ds),
                   mock.patch("desk.devcheck.investigate", return_value=dev),
                   mock.patch("desk.solana_agent.alert")]
        for p in patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        a.run_once()
        return a

    def test_confirmed_entry_uses_fresh_price_scalp_horizon(self):
        a = self.run_agent({"price_usd": 0.00105, "liquidity_usd": 39_000, "volume_h6": 1, "volume_h24": 1})
        if a.stats["entries"] == 0:   # surface why, if the mocks are not enough
            self.fail([e["message"] for e in list(a.events)[:8]])
        p = a.paper.s["positions"]["REALMINT1"]
        self.assertEqual(p["horizon"], "scalp")
        self.assertAlmostEqual(p["entry"], 0.00105 * (1 + config.PAPER["slippage_pct"]))
        self.assertLessEqual(p["cost"], config.RISK["max_pool_share"] * 39_000 + 0.01)

    def test_price_gap_blocks_entry(self):
        a = self.run_agent({"price_usd": 0.0015, "liquidity_usd": 40_000, "volume_h6": 1, "volume_h24": 1})
        self.assertEqual(a.stats["entries"], 0)
        self.assertEqual(a.reject_reasons["price_gap"], 1)

    def test_thin_second_source_liquidity_blocks_entry(self):
        a = self.run_agent({"price_usd": 0.001, "liquidity_usd": 5_000, "volume_h6": 1, "volume_h24": 1})
        self.assertEqual(a.stats["entries"], 0)
        self.assertEqual(a.reject_reasons["liq_unconfirmed"], 1)

    def test_no_confirm_quote_blocks_entry(self):
        a = self.run_agent(None)
        self.assertEqual(a.stats["entries"], 0)
        self.assertEqual(a.reject_reasons["no_confirm_quote"], 1)
