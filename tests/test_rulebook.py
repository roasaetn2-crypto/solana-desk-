"""Romz's scalping rulebook: PAPER entry gates and fast exits.

Every entry gate must SKIP on missing data; fast exits must NOT force a sell on
missing data. The conftest turns the rulebook off for the older tests; these
switch it on.
"""
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
from config import RULEBOOK  # noqa: E402
from desk import devcheck, rulebook, scanner, settings  # noqa: E402
from desk.solana_agent import SolanaAgent  # noqa: E402
from legacy_book import legacy_book  # noqa: E402

DEFAULTS = {"tx_ratio_gate": True, "min_buy_sell_ratio": 1.2, "lp_gate": True,
            "min_lp_locked_pct": 0.90, "impact_gate": True, "max_price_impact": 0.02,
            "volume_rising_gate": True, "no_chase_gate": True, "max_candle_up": 0.40,
            "exit_on_liq_drop": True, "liq_drop_exit": 0.20, "exit_on_holder_sell": True,
            "top10_drop_exit": 0.05, "dev_sell_exit": 0.50, "holder_check_s": 120}


def on():
    RULEBOOK.update(DEFAULTS)


NOW = 1_800_000_000.0


def bar(t, o, c, v):
    return {"t": int(t), "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": v}


class Defaults(unittest.TestCase):
    def test_shipped_defaults_are_on(self):
        src = Path(config.__file__).read_text()
        self.assertIn('"tx_ratio_gate": True', src)
        for k in ("lp_gate", "impact_gate", "volume_rising_gate", "no_chase_gate",
                  "exit_on_liq_drop", "exit_on_holder_sell"):
            self.assertIn(f'"{k}": True', src)

    def test_settings_rows_have_bounds_and_only_change_via_confirm(self):
        on()
        keys = [k for k in settings.REGISTRY if k.startswith("rulebook.")]
        self.assertEqual(len(keys), 19)
        for k in keys:
            s = settings.REGISTRY[k]
            self.assertFalse(settings._EXCLUDED_RE.search(k), k)
            if s.kind != "bool":
                self.assertIsNotNone(s.lo)
                self.assertIsNotNone(s.hi)
        # a proposal never applies by itself; Confirm applies it (clamped to bounds)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"DESK_DATA_DIR": tmp}):
            settings.reset_runtime()
            out = settings.create([{"setting": "rulebook.max_price_impact", "proposed": "3%"},
                                   {"setting": "rulebook.lp_gate", "proposed": False}], "t", "desk_ai")
            self.assertTrue(out["ok"], out)
            self.assertEqual(RULEBOOK["max_price_impact"], 0.02)
            self.assertTrue(RULEBOOK["lp_gate"])
            pid = settings.listing()["pending"][0]["id"]
            settings.confirm(pid, "romz")
            self.assertEqual(RULEBOOK["max_price_impact"], 0.03)
            self.assertFalse(RULEBOOK["lp_gate"])
            settings.reset_runtime()


class EntryGates(unittest.TestCase):
    def setUp(self):
        on()

    def test_tx_ratio(self):
        ok = {"available": True, "buys_m5": 30, "sells_m5": 20}
        self.assertIsNone(rulebook.tx_ratio_gate(ok))
        self.assertEqual(rulebook.tx_ratio_gate({**ok, "buys_m5": 24}), "tx_ratio_low")  # 1.2 is not > 1.2
        self.assertIsNone(rulebook.tx_ratio_gate({**ok, "sells_m5": 0}))
        self.assertEqual(rulebook.tx_ratio_gate({"available": False}), "tx_ratio_unknown")
        self.assertEqual(rulebook.tx_ratio_gate(None), "tx_ratio_unknown")
        self.assertEqual(rulebook.tx_ratio_gate({"available": True}), "tx_ratio_unknown")
        self.assertEqual(rulebook.tx_ratio_gate({**ok, "buys_m5": 0, "sells_m5": 0}), "tx_ratio_unknown")
        RULEBOOK["tx_ratio_gate"] = False
        self.assertIsNone(rulebook.tx_ratio_gate(None))

    def test_lp(self):
        self.assertIsNone(rulebook.lp_gate({"lp_locked_pct": 1.0}))
        self.assertEqual(rulebook.lp_gate({"lp_locked_pct": 0.5}), "lp_unlocked")
        self.assertEqual(rulebook.lp_gate({"lp_locked_pct": None}), "lp_unknown")
        self.assertEqual(rulebook.lp_gate({}), "lp_unknown")
        self.assertEqual(rulebook.lp_gate({"lp_locked": True}), "lp_unknown")  # the old guess is not enough

    def test_lp_state_from_rugcheck_markets(self):
        curve = {"markets": [{"marketType": "pump_fun", "lp": {"lpLockedPct": 100, "lpLockedUSD": 1700}}]}
        self.assertEqual(devcheck.lp_state(curve)["lp_locked_pct"], 1.0)
        amm = {"markets": [
            {"marketType": "meteoraDlmm", "lp": {"lpLockedPct": 0, "quoteUSD": 10, "baseUSD": 10}},
            {"marketType": "pump_fun_amm", "lp": {"lpLockedPct": 100, "quoteUSD": 5e5, "baseUSD": 5e5}}]}
        st = devcheck.lp_state(amm)
        self.assertEqual((st["lp_locked_pct"], st["lp_market"]), (1.0, "pump_fun_amm"))
        rug = {"markets": [{"marketType": "raydium", "lp": {"lpLockedPct": 3, "quoteUSD": 9e4}}]}
        self.assertEqual(devcheck.lp_state(rug)["lp_locked_pct"], 0.03)
        self.assertIsNone(devcheck.lp_state({})["lp_locked_pct"])
        self.assertIsNone(devcheck.lp_state({"markets": [{"lp": {}}]})["lp_locked_pct"])
        # "Large Amount of LP Unlocked" must never read as locked
        body = {"risks": [{"name": "Large Amount of LP Unlocked", "value": "90%"}], "markets": []}
        self.assertIsNone(devcheck.lp_state(body)["lp_locked"])

    def test_impact(self):
        q = {"available": True, "route": True, "impact": 0.01}
        self.assertIsNone(rulebook.impact_gate(q, 5.0, 4.0))
        self.assertEqual(rulebook.impact_gate({**q, "impact": 0.02}, 5.0, 5.0), "impact_high")
        self.assertEqual(rulebook.impact_gate({"available": False}, 5.0, 5.0), "impact_unknown")
        self.assertEqual(rulebook.impact_gate({**q, "route": False}, 5.0, 5.0), "impact_unknown")
        self.assertEqual(rulebook.impact_gate(None, None, 5.0), "impact_unknown")
        self.assertEqual(rulebook.impact_gate({**q, "impact": None}, 5.0, 5.0), "impact_unknown")
        # a quote for a SMALLER ticket is not a bound for the real one
        self.assertEqual(rulebook.impact_gate(q, 2.0, 5.0), "impact_unknown")

    def test_volume_from_scan_history(self):
        t = {"mint": "M", "source": "jupiter", "volume_m5": 900}
        self.assertEqual(rulebook.volume_gate(t, [], NOW), "vol_unknown")       # no previous 5m
        rulebook.record_volume([{**t, "volume_m5": 600}], NOW - 300)
        self.assertIsNone(rulebook.volume_gate(t, [], NOW))
        self.assertEqual(rulebook.volume_gate({**t, "volume_m5": 500}, [], NOW), "vol_falling")
        # a sample from another source is not the same series
        self.assertEqual(rulebook.volume_gate({**t, "source": "dexscreener"}, [], NOW), "vol_unknown")
        # too old / too fresh does not count as "previous 5m"
        rulebook.reset()
        rulebook.record_volume([{**t, "volume_m5": 600}], NOW - 60)
        rulebook.record_volume([{**t, "volume_m5": 600}], NOW - 900)
        self.assertEqual(rulebook.volume_gate(t, [], NOW), "vol_unknown")

    def test_volume_from_closed_candles(self):
        t = {"mint": "C", "source": "x", "volume_m5": None}
        b0 = NOW - NOW % 300
        cs = [bar(b0 - 900, 1, 1, 100), bar(b0 - 600, 1, 1, 200), bar(b0 - 300, 1, 1, 300),
              bar(b0, 1, 1, 5)]           # last bar still forming: ignored
        self.assertIsNone(rulebook.volume_gate(t, cs, NOW))
        cs[-2]["v"] = 150
        self.assertEqual(rulebook.volume_gate(t, cs, NOW), "vol_falling")
        self.assertEqual(rulebook.volume_gate(t, cs[-1:], NOW), "vol_unknown")
        self.assertEqual(rulebook.volume_gate(t, None, NOW), "vol_unknown")
        old = [bar(b0 - 3600, 1, 1, 1), bar(b0 - 3300, 1, 1, 9)]
        self.assertEqual(rulebook.volume_gate(t, old, NOW), "vol_unknown")

    def test_no_chase(self):
        b0 = NOW - 100
        self.assertIsNone(rulebook.chase_gate([bar(b0, 1.0, 1.3, 1)], now=NOW))
        self.assertEqual(rulebook.chase_gate([bar(b0, 1.0, 1.5, 1)], now=NOW), "no_chase")
        # no fresh candle: fall back to the rolling 5m change, else skip
        stale = [bar(NOW - 3600, 1.0, 1.0, 1)]
        self.assertEqual(rulebook.chase_gate(stale, {"change_m5": 55}, None, NOW), "no_chase")
        self.assertIsNone(rulebook.chase_gate([], {}, {"available": True, "change_m5": 10}, NOW))
        self.assertEqual(rulebook.chase_gate([], {}, {"available": False}, NOW), "chase_unknown")
        self.assertEqual(rulebook.chase_gate(None, None, None, NOW), "chase_unknown")


class FastExits(unittest.TestCase):
    def setUp(self):
        on()

    def test_liquidity_drop(self):
        pos = {"entry_liq": 40_000}
        self.assertEqual(rulebook.liq_exit(pos, {"liquidity_usd": 35_000}), (None, None))
        why, _ = rulebook.liq_exit(pos, {"liquidity_usd": 32_000})
        self.assertIn("liquidity -20%", why)
        self.assertEqual(rulebook.liq_exit(pos, None)[0], None)
        self.assertEqual(rulebook.liq_exit({}, {"liquidity_usd": 1})[0], None)
        self.assertTrue(rulebook.liq_exit({}, {"liquidity_usd": 1})[1])     # logged, not sold

    def test_holder_sells(self):
        pos = {"entry_dev_hold": 0.04, "entry_top10": 0.30}
        self.assertEqual(rulebook.holder_exit(pos, {"available": True, "creator_hold_pct": 0.03,
                                                    "top_10_pct": 0.28}), (None, None))
        self.assertIn("dev sold", rulebook.holder_exit(
            pos, {"available": True, "creator_hold_pct": 0.01, "top_10_pct": 0.30})[0])
        self.assertIn("top-10", rulebook.holder_exit(
            pos, {"available": True, "creator_hold_pct": 0.04, "top_10_pct": 0.24})[0])
        why, note = rulebook.holder_exit(pos, {"available": False})
        self.assertIsNone(why)
        self.assertTrue(note)
        why, note = rulebook.holder_exit({}, {"available": True, "creator_hold_pct": 0.0, "top_10_pct": 0.1})
        self.assertIsNone(why)
        self.assertTrue(note)


TOK = {"mint": "RULEMINT1" + "x" * 30, "symbol": "RULE", "name": "RULE", "pool": "P",
       "dex": "pumpswap", "price_usd": 0.001, "mcap_usd": 300_000, "liquidity_usd": 40_000,
       "volume_m5": 5000, "volume_h1": 90_000, "volume_h6": 150_000, "volume_h24": 300_000,
       "buys_h1": 500, "sells_h1": 300, "buyers_h1": 220, "change_m5": 3, "change_h1": 60,
       "change_h6": 80, "age_minutes": 45, "source": "geckoterminal"}
CLEAN = {"chips": [], "block": [], "halve": [], "unknown": [], "parts": {}}
GOOD_DS = {"available": True, "has_x": True, "has_telegram": True, "websites": ["https://x.y"],
           "socials": {}, "buys_m5": 40, "sells_m5": 20, "change_m5": 3}
GOOD_DEV = {"hard_no": [], "dev_score": 90, "dev_hold_pct": 0.01, "creator_prev_rugs": 0,
            "sniper_pct": 0.05, "reasons": [], "lp_locked_pct": 1.0, "creator_hold_pct": 0.02,
            "concentration": {"top_10_pct": 0.2}}
GOOD_JUP = {"available": True, "route": True, "impact": 0.004, "impact_pct": 0.4,
            "flags": [], "block": [], "halve": []}


@legacy_book
class AgentWiring(unittest.TestCase):
    def setUp(self):
        on()

    def run_agent(self, ds=GOOD_DS, dev=GOOD_DEV, jup=GOOD_JUP, history=True, mode=None):
        d = Path(tempfile.mkdtemp())
        a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        if history:
            rulebook.record_volume([{**TOK, "volume_m5": 3000}], time.time() - 300)
        facts = {"mint_authority": False, "freeze_authority": False, "top_wallet_pct": 0.03,
                 "top_10_pct": 0.2, "holders": 400, "supply": 1e9, "decimals": 6}
        quote = {"price_usd": 0.00105, "liquidity_usd": 39_000, "volume_h6": 1, "volume_h24": 1}
        cs = {"candles": [bar(time.time() - 60, 1.0, 1.05, 10)], "tf": "5m"}
        patches = [mock.patch.object(scanner, "new_tokens", return_value=[dict(TOK)]),
                   mock.patch.object(scanner, "socials", return_value={"x": "https://x.com/a", "telegram": None, "website": None}),
                   mock.patch.object(scanner, "quote", return_value=quote),
                   mock.patch("desk.chain.facts", return_value=facts),
                   mock.patch("desk.dexscreener.facts", return_value=dict(ds)),
                   mock.patch("desk.devcheck.investigate", return_value=dict(dev)),
                   mock.patch("desk.solana_agent.alert"),
                   mock.patch("desk.solana_agent.candle_chart.series", return_value=cs),
                   mock.patch("desk.solana_agent.signals.security", return_value=CLEAN),
                   mock.patch("desk.solana_agent.signals.entry_impact", return_value=jup)]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        a.run_once()
        return a

    def skips(self, a):
        return [e["reason"] for e in a.events if e.get("stage") == "rulebook"]

    def test_all_rules_pass_and_entry_records_entry_state(self):
        a = self.run_agent()
        self.assertEqual(self.skips(a), [])
        self.assertEqual(a.stats["entries"], 1, [e["message"] for e in list(a.events)[:8]])
        pos = a.paper.s["positions"][TOK["mint"]]
        self.assertEqual(pos["entry_liq"], 39_000)
        self.assertEqual(pos["entry_top10"], 0.2)
        self.assertEqual(pos["entry_dev_hold"], 0.02)

    def test_missing_data_skips_each_gate(self):
        for kw, reason in ((dict(ds={"available": False}), "tx_ratio_unknown"),
                           (dict(dev={**GOOD_DEV, "lp_locked_pct": None}), "lp_unknown"),
                           (dict(history=False), "vol_unknown"),
                           (dict(jup={"available": False, "flags": [], "block": [], "halve": []}),
                            "impact_unknown")):
            rulebook.reset()
            with self.subTest(reason=reason):
                a = self.run_agent(**kw)
                self.assertEqual(a.stats["entries"], 0)
                self.assertIn(reason, self.skips(a))
                self.assertTrue(any(e["message"] == f"gate: $RULE skip {reason}" for e in a.events))

    def test_bad_values_skip(self):
        a = self.run_agent(jup={**GOOD_JUP, "impact": 0.05})
        self.assertEqual(self.skips(a), ["impact_high"])
        self.assertEqual(a.stats["entries"], 0)

    def _held(self, **meta):
        d = Path(tempfile.mkdtemp())
        a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        a.paper.buy(TOK["mint"], "RULE", 5.0, 0.001, horizon="scalp",
                    meta={"entry_liq": 40_000, "entry_top10": 0.30, "entry_dev_hold": 0.04, **meta})
        return a

    def _manage(self, a, liq, snap):
        q = {TOK["mint"]: {"price_usd": 0.001, "liquidity_usd": liq, "volume_h6": 1, "volume_h24": 1}}
        with mock.patch.object(scanner, "quote_many", return_value=q), \
                mock.patch("desk.solana_agent.candle_chart.prefetch"), \
                mock.patch("desk.devcheck.holder_snapshot", return_value=snap), \
                mock.patch("desk.solana_agent.signal_engine.should_sell", return_value=(False, "")):
            a._manage_positions()

    def test_liquidity_drop_sells_everything(self):
        a = self._held()
        self._manage(a, 30_000, {"available": True, "creator_hold_pct": 0.04, "top_10_pct": 0.30})
        self.assertNotIn(TOK["mint"], a.paper.s["positions"])
        self.assertIn("liquidity", a.paper.s["closed"][-1].get("reason", ""))

    def test_dev_sell_sells_everything(self):
        a = self._held()
        self._manage(a, 40_000, {"available": True, "creator_hold_pct": 0.0, "top_10_pct": 0.30})
        self.assertNotIn(TOK["mint"], a.paper.s["positions"])

    def test_missing_exit_data_is_not_a_forced_exit(self):
        a = self._held()
        self._manage(a, None, {"available": False})
        self.assertIn(TOK["mint"], a.paper.s["positions"])
        b = self._held(entry_liq=None, entry_top10=None, entry_dev_hold=None)
        self._manage(b, 1_000, {"available": True, "creator_hold_pct": 0.0, "top_10_pct": 0.0})
        self.assertIn(TOK["mint"], b.paper.s["positions"])

    def test_live_mode_never_runs_the_rulebook(self):
        """Every rulebook call site in the agent sits behind a paper-mode check."""
        src = Path(sys.modules["desk.solana_agent"].__file__).read_text()
        for call in ("rulebook.tx_ratio_gate(ds)", "rulebook.impact_gate(", "self._rulebook_exit("):
            i = src.index(call)
            self.assertIn('self.mode != "live"', src[max(0, i - 400):i], call)

if __name__ == "__main__":
    unittest.main()
