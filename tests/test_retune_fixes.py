"""2026-11-12 fixes: real holder counts, GT feed rotation, runner exits, RugCheck concentration
fallback, and the approved retune (with saved overrides still winning). No live network."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from desk import autotune, brain_advisor, chain, devcheck, exits, meme, scanner, settings  # noqa: E402
from test_scanner_sources import FakeResp, _gt_pool  # noqa: E402

CREATOR = "CreatorWallet1111"
CURVE = "CurvePDA11111111"
LOCKER = "LockerPDA1111111"
MARKET = "MarketPubkey1111"


def rc_body(holders=None, total=1234, extra_known=None):
    """A RugCheck report shaped like the live API (topHolders pct is in percent)."""
    top = holders if holders is not None else [
        {"address": "curveATA", "owner": CURVE, "pct": 78.0},
        {"address": "creatorATA", "owner": CREATOR, "pct": 1.0},
        {"address": "w1ATA", "owner": "Wallet1", "pct": 3.0},
        {"address": "w2ATA", "owner": "Wallet2", "pct": 2.0},
    ]
    known = {CURVE: {"name": "Pump Fun", "type": "AMM"},
             LOCKER: {"name": "Locker", "type": "LOCKER"},
             CREATOR: {"name": "Creator", "type": "CREATOR"}}
    known.update(extra_known or {})
    return {"creator": CREATOR, "topHolders": top, "totalHolders": total, "knownAccounts": known,
            "markets": [{"pubkey": MARKET, "marketType": "pump_fun",
                         "liquidityAAccount": {"owner": MARKET}, "liquidityBAccount": {"owner": MARKET}}],
            "token": {"mintAuthority": None, "freezeAuthority": None}, "risks": [], "score": 1}


class Sess:
    def __init__(self, body):
        self.body = body

    def get(self, url, headers=None, timeout=None):
        return FakeResp(200, self.body)


CF = {"mint_authority": None, "freeze_authority": None, "top_wallet_pct": None, "top_10_pct": None,
      "wallets_counted": 0, "holder_count": None}


# ---------- fix 1: holder count ----------
class HolderCount(unittest.TestCase):
    def test_rugcheck_uses_total_holders_not_the_sample(self):
        rep = devcheck.investigate("MINTA", CF, Sess(rc_body(total=1234)))
        self.assertEqual(rep["holder_count"], 1234)
        self.assertEqual(rep["holder_sample"], 4)

    def test_rugcheck_zero_total_is_unknown_not_zero(self):
        rep = devcheck.investigate("MINTA", CF, Sess(rc_body(total=0)))
        self.assertIsNone(rep["holder_count"])

    def test_rpc_only_path_never_reports_the_sample_as_holders(self):
        broken = mock.Mock()
        broken.get.side_effect = RuntimeError("rugcheck down")
        rep = devcheck.investigate("MINTA", {**CF, "wallets_counted": 20, "top_wallet_pct": 0.02}, broken)
        self.assertEqual(rep["source"], "rpc-only")
        self.assertIsNone(rep["holder_count"])

    def test_sample_only_scores_neutral_not_as_twenty_holders(self):
        facts = {**CF, "wallets_counted": 20}
        dev = {"holder_count": 20, "holder_sample": 20, "dev_score": 60}
        self.assertIsNone(meme.real_holder_count(facts, dev))
        sc = meme.scorecard({"symbol": "X"}, facts, {}, dev)
        self.assertIn("holder count unknown (neutral)", sc["reasons"]["community"])
        # neutral 15 + nothing else (no socials, no buys) = 15; the old code gave 20/150*30 = 4
        self.assertEqual(sc["parts"]["community"], 15)

    def test_real_count_above_sample_counts(self):
        facts = {**CF, "wallets_counted": 20, "holder_count": 900}
        self.assertEqual(meme.real_holder_count(facts, {}), 900)
        sc = meme.scorecard({"symbol": "X"}, facts, {}, {"dev_score": 60})
        self.assertIn("900 holders", sc["reasons"]["community"])
        self.assertEqual(sc["parts"]["community"], 30)

    def test_small_real_count_with_a_partial_sample_is_believed(self):
        # only 7 accounts exist at all: the sample was not full, so 7 is real
        self.assertEqual(meme.real_holder_count({**CF, "wallets_counted": 7}, {"holder_count": 7}), 7)

    def test_ai_summary_gets_the_real_count(self):
        s = brain_advisor.Advisor.summarize({"symbol": "X"}, {**CF, "wallets_counted": 20},
                                                 {"holder_count": 640, "holder_sample": 20})
        self.assertEqual(s["holders"], 640)
        s = brain_advisor.Advisor.summarize({"symbol": "X"}, {**CF, "wallets_counted": 20},
                                                 {"holder_count": None, "holder_sample": 20})
        self.assertIsNone(s["holders"])


# ---------- fix 4: concentration fallback ----------
class ConcentrationFallback(unittest.TestCase):
    def test_pool_locker_market_excluded_creator_kept(self):
        body = rc_body(holders=[
            {"address": "curveATA", "owner": CURVE, "pct": 70.0},
            {"address": "lockATA", "owner": LOCKER, "pct": 10.0},
            {"address": "mktATA", "owner": MARKET, "pct": 5.0},
            {"address": "creatorATA", "owner": CREATOR, "pct": 4.0},
            {"address": "w1ATA", "owner": "Wallet1", "pct": 2.0},
        ])
        c = devcheck.rugcheck_concentration(body)
        self.assertAlmostEqual(c["top_wallet_pct"], 0.04)        # the creator, not the curve
        self.assertAlmostEqual(c["top_10_pct"], 0.06)
        self.assertAlmostEqual(c["excluded_pct"], 0.85)

    def test_fills_only_when_rpc_had_nothing_and_still_blocks_whales(self):
        whale = rc_body(holders=[{"address": "curveATA", "owner": CURVE, "pct": 60.0},
                                 {"address": "wATA", "owner": "Whale", "pct": 12.0}])
        dev = devcheck.investigate("MINTW", CF, Sess(whale))
        f2 = chain.with_fallback_concentration(CF, dev)
        self.assertEqual(f2["concentration_source"], "rugcheck")
        self.assertEqual(chain.chain_kill(f2), "top_wallet")

    def test_creator_whale_is_still_blocked(self):
        body = rc_body(holders=[{"address": "curveATA", "owner": CURVE, "pct": 60.0},
                                {"address": "creatorATA", "owner": CREATOR, "pct": 9.0}])
        dev = devcheck.investigate("MINTC", CF, Sess(body))
        self.assertEqual(chain.chain_kill(chain.with_fallback_concentration(CF, dev)), "top_wallet")
        self.assertIn("dev_hold", dev["hard_no"])               # the dev kill is untouched

    def test_spread_out_coin_passes(self):
        dev = devcheck.investigate("MINTS", CF, Sess(rc_body()))
        f2 = chain.with_fallback_concentration(CF, dev)
        self.assertIsNone(chain.chain_kill(f2))

    def test_rpc_measurement_is_never_replaced(self):
        dev = devcheck.investigate("MINTS", CF, Sess(rc_body()))
        self.assertIsNone(chain.with_fallback_concentration({**CF, "top_wallet_pct": 0.5}, dev))
        self.assertEqual(chain.chain_kill({**CF, "top_wallet_pct": 0.5}), "top_wallet")


# ---------- fix 3: runner after TP1 ----------
class Runner(unittest.TestCase):
    def pos(self, horizon="swing"):
        p = {"entry": 1.0, "opened": 0.0}
        exits.init_state(p, horizon)
        return p

    def test_tp1_moves_stop_to_breakeven_and_starts_runner_clock(self):
        p = self.pos()
        frac, why = exits.evaluate(p, {"price_usd": 1.6}, 600)
        self.assertIn("TP1", why)
        self.assertEqual(p["stop_price"], 1.0)
        self.assertEqual(p["half_at"], 600)
        self.assertEqual(exits.evaluate(p, {"price_usd": 0.99}, 700), (1.0, "breakeven stop"))

    def test_runner_trails_at_most_twenty_pct_even_on_swing(self):
        self.assertEqual(config.EXITS["profiles"]["swing"]["trail_pct"], 0.30)
        p = self.pos("swing")
        exits.evaluate(p, {"price_usd": 1.6}, 600)               # TP1, peak 1.6
        self.assertIsNone(exits.evaluate(p, {"price_usd": 1.30}, 700))   # -18.75%: still held
        frac, why = exits.evaluate(p, {"price_usd": 1.27}, 800)          # -20.6% from peak
        self.assertEqual((frac, why), (1.0, "trail -20% from peak"))

    def test_runner_frees_its_slot_after_180_minutes(self):
        p = self.pos("swing")
        exits.evaluate(p, {"price_usd": 1.6}, 600)
        self.assertIsNone(exits.evaluate(p, {"price_usd": 1.6}, 600 + 179 * 60))
        frac, why = exits.evaluate(p, {"price_usd": 1.6}, 600 + 180 * 60)
        self.assertEqual(frac, 1.0)
        self.assertIn("runner time cap", why)

    def test_before_tp1_nothing_changes(self):
        p = self.pos("swing")
        self.assertIsNone(exits.evaluate(p, {"price_usd": 1.2}, 600 + 200 * 60))
        self.assertNotIn("half_at", p)


# ---------- fix 2: GT rotation ----------
class GtRotation(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()
        self.t = time.time()

    def _session(self, status=200):
        urls = []

        def get(url, params=None, headers=None, timeout=None):
            urls.append((url, dict(params or {})))
            if "geckoterminal" in url:
                return FakeResp(status, {"data": [], "included": []})
            return FakeResp(200, [] if "jup" in url else {"pairs": []})
        s = mock.Mock()
        s.get.side_effect = get
        return s, urls

    def test_rotation_walks_every_feed_and_page(self):
        ids = [e["id"] for e in scanner._gt_rotation()]
        for feed in ("new_pools", "trending_5m", "trending_1h"):
            for page in (1, 2, 3):
                if (feed, page) != ("new_pools", 1):
                    self.assertIn(f"gt:{feed}:{page}", ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_served_slots_advance_the_cursor_and_stay_under_the_gt_cap(self):
        s, urls = self._session()
        seen = set()
        with mock.patch("time.time", lambda: self.t), mock.patch.object(scanner, "_hydrate_pumpportal", lambda *a: 0):
            for _ in range(20):
                scanner.new_tokens(s)
                self.t += 30
        for url, params in urls:
            if "geckoterminal" in url:
                seen.add((url.rsplit("/", 1)[-1], params.get("duration"), params.get("page")))
        self.assertIn(("trending_pools", "5m", 1), seen)
        self.assertIn(("trending_pools", "1h", 1), seen)
        self.assertIn(("new_pools", None, 2), seen)
        gt_calls = sum(1 for u, _ in urls if "geckoterminal" in u)
        self.assertLessEqual(gt_calls, config.SCAN_FETCH["gt_per_min"] * 10 + config.SCAN_FETCH["gt_burst"])

    def test_cursor_holds_while_gt_is_cooling(self):
        s, _ = self._session(status=429)
        with mock.patch("time.time", lambda: self.t), mock.patch.object(scanner, "_hydrate_pumpportal", lambda *a: 0):
            for _ in range(4):
                scanner.new_tokens(s)
                self.t += 5
        self.assertEqual(scanner._gt_cursor[0], 0)

    def test_jupiter_fallback_still_runs_every_cycle(self):
        s, urls = self._session(status=429)
        with mock.patch("time.time", lambda: self.t), mock.patch.object(scanner, "_hydrate_pumpportal", lambda *a: 0), \
                mock.patch.object(scanner, "_jup_enabled", lambda: True):
            scanner.new_tokens(s)
        self.assertTrue(any("jup.ag" in u for u, _ in urls))

    def test_trending_feed_drops_pools_older_than_max_age(self):
        old = _gt_pool("OLDMINT", "OLD")
        old["attributes"]["pool_created_at"] = "2024-01-01T00:00:00Z"
        fresh = _gt_pool("NEWMINT", "NEW")
        body = {"data": [old, fresh], "included": []}
        self.assertEqual(scanner._absorb_gt(body, time.time(), age_cut=True), 1)
        self.assertIn("NEWMINT", scanner._POOL)
        self.assertNotIn("OLDMINT", scanner._POOL)


# ---------- fix 5: retune + saved overrides ----------
class Retune(unittest.TestCase):
    def test_defaults(self):
        S, R, E, M = config.SCAN, config.RISK, config.EXITS, config.MEME
        self.assertEqual((S["min_age_minutes"], S["max_age_hours"], S["min_liquidity_usd"],
                          S["min_volume_h1_usd"], S["min_mcap_usd"], S["min_trades_h1"], S["pages"]),
                         (1, 48, 3_000, 3_000, 8_000, 30, 1))
        self.assertEqual((R["max_open_positions"], R["max_risk_per_trade"]), (6, 0.015))
        self.assertEqual((E["stop_pct"], E["max_hold_hours"]), (0.30, 4))
        self.assertEqual(M["min_community_score"], 35)
        self.assertEqual((E["breakeven_after_half"], E["runner_trail_pct"], E["runner_max_minutes"]),
                         (True, 0.20, 180))

    def test_scam_and_loss_limits_untouched(self):
        self.assertEqual(config.CHAIN["max_top_wallet"], 0.08)
        self.assertEqual(config.CHAIN["max_top_10"], 0.35)
        self.assertEqual(config.RISK["max_daily_loss_pct"], 0.03)
        self.assertEqual(config.MEME["max_dev_hold_pct"], 0.05)
        self.assertEqual(config.MEME["max_sniper_pct"], 0.20)
        self.assertEqual(config.EXECUTION["default_mode"], "paper")

    def test_retuned_values_fit_the_settings_bounds(self):
        for k in ("scan.min_age_minutes", "scan.max_age_hours", "scan.min_liquidity_usd", "scan.min_volume_h1_usd",
                  "scan.min_mcap_usd", "scan.min_trades_h1", "risk.max_open_positions", "risk.max_risk_per_trade",
                  "exits.stop_pct", "meme.min_community_score"):
            s = settings.REGISTRY[k]
            self.assertEqual(s.coerce(s.get())[1], None, k)

    def test_autotune_drops_gates_tuned_from_old_defaults_keeps_the_rest(self):
        saved = {"v": config.ANALYST_VALID_SCORE, "m": config.MIN_SCORE, "scan": dict(config.SCAN),
                 "chain": dict(config.CHAIN)}
        try:
            d = Path(tempfile.mkdtemp())
            (d / "at.json").write_text(json.dumps({"gates": {"score": 6, "volume_h1": 9_000,
                                                             "trades_h1": 70, "top_10": 0.35}}))
            at = autotune.AutoTune(mock.Mock(), d / "at.json", d / "at.jsonl")
            self.assertEqual(config.SCAN["min_volume_h1_usd"], 3_000)   # new default stands
            self.assertEqual(config.SCAN["min_trades_h1"], 30)
            self.assertEqual(config.ANALYST_VALID_SCORE, 6)             # owner's 6 (default moved 5 -> 6)
            self.assertEqual(set(at.dropped), {"score", "volume_h1", "trades_h1"})
            at._persist()
            st = json.loads((d / "at.json").read_text())
            self.assertEqual(st["baseline"]["volume_h1"], 3_000)
            # a gate tuned against the NEW baseline is restored normally
            st["gates"]["volume_h1"] = 5_000
            (d / "at.json").write_text(json.dumps(st))
            autotune.AutoTune(mock.Mock(), d / "at.json", d / "at.jsonl")
            self.assertEqual(config.SCAN["min_volume_h1_usd"], 5_000)
        finally:
            config.ANALYST_VALID_SCORE, config.MIN_SCORE = saved["v"], saved["m"]
            config.SCAN.update(saved["scan"]); config.CHAIN.update(saved["chain"])

    def test_owner_score_six_survives_restart_and_tuner_cannot_raise_it(self):
        saved = {"v": config.ANALYST_VALID_SCORE, "m": config.MIN_SCORE}
        try:
            self.assertEqual((config.ANALYST_VALID_SCORE, config.MIN_SCORE), (6, 6))
            d = Path(tempfile.mkdtemp())
            # the live Turso file: self-tuner's 8, saved against the new-style baseline of 5
            (d / "at.json").write_text(json.dumps({"gates": {"score": 8},
                                                   "baseline": {**autotune.DEFAULTS, "score": 5}}))
            autotune.AutoTune(mock.Mock(), d / "at.json", d / "at.jsonl")
            self.assertEqual(config.ANALYST_VALID_SCORE, 6)
            # even a file claiming the current baseline is clamped to the tight end, 6
            (d / "at.json").write_text(json.dumps({"gates": {"score": 8}, "baseline": autotune.DEFAULTS}))
            autotune.AutoTune(mock.Mock(), d / "at.json", d / "at.jsonl")
            self.assertEqual(config.ANALYST_VALID_SCORE, 6)
            for _ in range(5):
                autotune.notch("score", False)                        # tighten as hard as it can
            self.assertEqual(config.ANALYST_VALID_SCORE, 6)
            self.assertFalse(autotune.can_move("score", False))
        finally:
            config.ANALYST_VALID_SCORE, config.MIN_SCORE = saved["v"], saved["m"]

    def test_swing_hold_is_four_hours_and_presets_match(self):
        sw = config.EXITS["profiles"]["swing"]
        self.assertEqual((sw["max_hold_hours"], sw["timeout_hours"]), (4.0, 3.0))
        self.assertLessEqual(sw["timeout_hours"], sw["max_hold_hours"])
        for name in ("swing", "balanced"):
            v = settings.PRESETS[name]["values"]
            self.assertEqual(v["exits.swing.max_hold_hours"], 4, name)
            self.assertEqual(v["exits.swing.timeout_hours"], 3, name)
            vals = settings.current_values(); vals.update(v)
            self.assertEqual(settings._cross_check(vals), [], name)

    def test_operator_confirmed_values_still_win(self):
        saved = (dict(config.SCAN), dict(config.RISK))
        try:
            vals = {"scan.max_age_hours": 6.0, "risk.max_risk_per_trade": 0.10}
            with mock.patch.object(settings, "_read", return_value={"values": vals}):
                applied = settings.apply_saved()
            self.assertEqual(applied, vals)
            self.assertEqual(config.SCAN["max_age_hours"], 6.0)
            self.assertEqual(config.RISK["max_risk_per_trade"], 0.10)
        finally:
            config.SCAN.update(saved[0]); config.RISK.update(saved[1])


if __name__ == "__main__":
    unittest.main()
