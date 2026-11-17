"""The scalp / swing / both band routing.

These lock the fix for a real bug: `free_kill` ran the scalp gate first and then
applied SCAN's mcap band (100k-2M) anyway, so every fresh launch inside the
scalp band (10k-100k) was killed as "mcap" and the fast lane could never trade.

The rules under test:
  * with the scalp lane OFF, behaviour is exactly the legacy SCAN behaviour
  * with it ON, a coin inside the scalp age window is judged by SCALP's numbers
  * a fresh coin that is too big/deep for a scalp falls through to swing
  * a coin outside the scalp window is never rejected for being "scalp_age"
"""
import unittest

from config import SCALP, SWING
import desk.scanner as scanner
import desk.exits as exits


def coin(**kw):
    t = dict(age_minutes=8, liquidity_usd=12_000, volume_h1=9_000, volume_m5=800,
             mcap_usd=45_000, buys_h1=60, sells_h1=40, change_h1=40.0, symbol="T")
    t.update(kw)
    return t


class BandTest(unittest.TestCase):
    def setUp(self):
        self._scalp = SCALP.get("enabled")
        self._swing = SWING.get("enabled")

    def tearDown(self):
        SCALP["enabled"] = self._scalp
        SWING["enabled"] = self._swing

    # ---- scalp off: legacy behaviour must be untouched ----
    def test_scalp_off_keeps_legacy_mcap_floor(self):
        SCALP["enabled"] = False
        self.assertEqual(scanner.free_kill(coin()), "mcap")          # sub-100k still refused
        self.assertIsNone(scanner.free_kill(coin(mcap_usd=150_000)))

    def test_scalp_off_horizon_uses_legacy_boundary(self):
        SCALP["enabled"] = False
        self.assertEqual(exits.horizon_for(60), "scalp")
        self.assertEqual(exits.horizon_for(600), "swing")

    # ---- the bug itself ----
    def test_fresh_scalp_band_coin_passes_with_lane_on(self):
        SCALP["enabled"] = True
        self.assertIsNone(scanner.free_kill(coin()),
                          "a 45k-mcap, 8-minute-old coin is exactly what the scalp lane is for")

    def test_scalp_band_rejections_are_scalp_specific(self):
        SCALP["enabled"] = True
        SWING["enabled"] = False          # no fall-through, so the scalp label survives
        self.assertEqual(scanner.free_kill(coin(volume_m5=100)), "scalp_volume")
        self.assertEqual(scanner.free_kill(coin(mcap_usd=4_000)), "scalp_mcap")
        self.assertEqual(scanner.free_kill(coin(liquidity_usd=90_000)), "scalp_liquidity")

    def test_too_deep_for_scalp_reports_the_swing_reason(self):
        # 45k mcap / 90k liquidity: outside the scalp band AND below swing's 100k
        # mcap floor, so the useful reason is swing's "mcap", not "scalp_liquidity".
        SCALP["enabled"] = True
        SWING["enabled"] = True
        self.assertEqual(scanner.free_kill(coin(liquidity_usd=90_000)), "mcap")

    def test_fresh_coin_too_big_for_scalp_falls_through_to_swing(self):
        SCALP["enabled"] = True
        SWING["enabled"] = True
        # 8 minutes old but 150k mcap: outside the scalp band, a fine swing
        self.assertIsNone(scanner.free_kill(coin(mcap_usd=150_000)))

    def test_older_coin_is_never_rejected_as_scalp_age(self):
        SCALP["enabled"] = True
        SWING["enabled"] = True
        self.assertNotEqual(scanner.free_kill(coin(mcap_usd=150_000, age_minutes=600)),
                            "scalp_age")

    def test_swing_off_closes_the_older_lane(self):
        SCALP["enabled"] = True
        SWING["enabled"] = False
        self.assertEqual(scanner.free_kill(coin(mcap_usd=150_000, age_minutes=600)), "age")

    # ---- horizon follows the scalp lane's own clock ----
    def test_horizon_follows_scalp_max_age_when_on(self):
        SCALP["enabled"] = True
        self.assertEqual(exits.horizon_for(8), "scalp")
        self.assertEqual(exits.horizon_for(SCALP["max_age_minutes"] + 1), "swing")

    # ---- shared sanity checks apply to both bands ----
    def test_chase_blocks_both_bands(self):
        SCALP["enabled"] = True
        self.assertEqual(scanner.free_kill(coin(change_h1=300)), "chase")
        self.assertEqual(scanner.free_kill(coin(mcap_usd=150_000, age_minutes=600, change_h1=300)),
                         "chase")

    def test_honeypot_blocks_both_bands(self):
        SCALP["enabled"] = True
        self.assertEqual(scanner.free_kill(coin(buys_h1=500, sells_h1=10)), "no_sells")


if __name__ == "__main__":
    unittest.main()


class LegendScalperTest(unittest.TestCase):
    """The 'always trades' card: every entry gate at its floor.

    This is the operator's "never sit idle" setting. The point of the test is
    that a wide spread of coins all reach the broker -- not that they are good
    trades. Three protective cuts must survive: sub-$5k mcap, honeypots, 7d+.

    Values are applied straight into the config dicts rather than through
    create_preset()/confirm(), because confirm() writes a settings_change row to
    journal.jsonl and that leaks into the journal-reading tests.
    """

    def setUp(self):
        import desk.settings as st
        from desk.settings import PRESETS, resolve
        self._snap = {}
        for k, v in PRESETS["legend_scalp"]["values"].items():
            s = resolve(k)
            self._snap[s.key] = s.get()
            s.set(v)

    def tearDown(self):
        import desk.settings as st
        for k, v in self._snap.items():
            try:
                st.REGISTRY[k].set(v)
            except Exception:
                pass

    def test_every_reasonable_coin_reaches_the_broker(self):
        # FIXED: these presets used to take any coin (that is what lost); now a coin must be in the
        # dip window (>= 2h old: the dip needs ~100 min of candles) with real liquidity and trades.
        good = coin(mcap_usd=300_000, age_minutes=360, liquidity_usd=80_000, volume_h1=40_000, buys_h1=90, sells_h1=60)
        self.assertIsNone(scanner.free_kill(good), "legend scalper should trade a liquid coin in the window")
        self.assertEqual(scanner.free_kill(coin(age_minutes=8)), "age")                 # fresh: dip unmeasurable
        self.assertIsNotNone(scanner.free_kill(coin(age_minutes=360, mcap_usd=300_000, liquidity_usd=2_500)))  # thin

    def test_protective_cuts_survive(self):
        self.assertEqual(scanner.free_kill(coin(mcap_usd=3_000, age_minutes=360, liquidity_usd=80_000, volume_h1=40_000, buys_h1=90, sells_h1=60)), "mcap")
        self.assertEqual(scanner.free_kill(coin(buys_h1=100, sells_h1=0, age_minutes=360, mcap_usd=300_000, liquidity_usd=80_000, volume_h1=40_000)), "no_sells")
        self.assertEqual(scanner.free_kill(coin(age_minutes=9 * 24 * 60)), "age")

    def test_legend_relaxes_the_starving_gates(self):
        from config import MEME, SIGNAL
        # FIXED: Legend no longer relaxes these (a plain BUY signal won 15.4% of trades).
        self.assertTrue(MEME.get("pullback_entry"))
        self.assertEqual(SIGNAL["min_confidence"], 60)
        self.assertTrue(SIGNAL["require_bullish"])


class SuperLegendScalperTest(unittest.TestCase):
    """The loosest card the registry allows: every floor and ceiling at its limit.

    Same contract as LegendScalperTest -- a wide spread of coins must reach the
    broker, and the three protective cuts must still fire -- but with the extra
    limits Legend still enforced (1B mcap ceiling, zero volume/trade floors,
    30% stop, 60 min holds) pushed to the registry floor.
    """

    def setUp(self):
        import desk.settings as st
        from desk.settings import PRESETS, resolve
        self._snap = {}
        for k, v in PRESETS["super_legend_scalp"]["values"].items():
            s = resolve(k)
            self._snap[s.key] = s.get()
            s.set(v)

    def tearDown(self):
        import desk.settings as st
        for k, v in self._snap.items():
            try:
                st.REGISTRY[k].set(v)
            except Exception:
                pass

    def test_preset_is_valid_and_changes_something(self):
        import desk.settings as st
        from desk.settings import PRESETS, validate_changes
        # setUp already applied the values, so restore first: validate_changes
        # drops no-ops, and every value would look like one.
        for k, v in self._snap.items():
            st.REGISTRY[k].set(v)
        raw = [{"setting": k, "proposed": v}
               for k, v in PRESETS["super_legend_scalp"]["values"].items()]
        changes, dropped = validate_changes(raw)
        self.assertEqual(dropped, [], f"every value must be in bounds: {dropped}")
        self.assertGreater(len(changes), 15)

    def test_every_reasonable_coin_reaches_the_broker(self):
        # FIXED: these presets used to take any coin (that is what lost); now a coin must be in the
        # dip window (>= 2h old: the dip needs ~100 min of candles) with real liquidity and trades.
        good = coin(mcap_usd=300_000, age_minutes=360, liquidity_usd=80_000, volume_h1=40_000, buys_h1=90, sells_h1=60)
        self.assertIsNone(scanner.free_kill(good), "super legend should trade a liquid coin in the window")
        self.assertEqual(scanner.free_kill(coin(age_minutes=8)), "age")                 # fresh: dip unmeasurable
        self.assertIsNotNone(scanner.free_kill(coin(age_minutes=360, mcap_usd=300_000, liquidity_usd=2_500)))  # thin

    def test_protective_cuts_survive(self):
        self.assertEqual(scanner.free_kill(coin(mcap_usd=3_000, age_minutes=360, liquidity_usd=80_000, volume_h1=40_000, buys_h1=90, sells_h1=60)), "mcap")
        self.assertEqual(scanner.free_kill(coin(buys_h1=100, sells_h1=0, age_minutes=360, mcap_usd=300_000, liquidity_usd=80_000, volume_h1=40_000)), "no_sells")
        self.assertEqual(scanner.free_kill(coin(age_minutes=9 * 24 * 60)), "age")

    def test_super_is_at_least_as_loose_as_legend(self):
        from desk.settings import PRESETS
        sup = PRESETS["super_legend_scalp"]["values"]
        leg = PRESETS["legend_scalp"]["values"]
        # ceilings only ever go up, floors only ever go down
        self.assertGreaterEqual(sup["scan.max_mcap_usd"], leg["scan.max_mcap_usd"])
        self.assertLessEqual(sup["scan.min_mcap_usd"], leg["scan.min_mcap_usd"])
        self.assertLessEqual(sup["swing.min_volume_h1_usd"], leg["swing.min_volume_h1_usd"])
        self.assertLessEqual(sup["swing.min_trades_h1"], leg["swing.min_trades_h1"])
        self.assertGreaterEqual(sup["scalp.stop_pct"], leg["scalp.stop_pct"])
        self.assertGreaterEqual(sup["scalp.max_hold_minutes"], leg["scalp.max_hold_minutes"])


class SuperLegendV2Test(unittest.TestCase):
    """The v2 preset: Super Legend's entries, Legend's payoff ratio.

    Super Legend measured 64% win rate and still made +5.88%, because a 30%
    stop against a 1.10 blended TP is a payoff of 0.69 -- it needs 75% just to
    break even. v2 keeps the loose entries (that is where the win rate came
    from) and fixes the exit geometry. These assertions pin the fix in place.
    """

    def test_every_value_is_in_bounds(self):
        from desk.settings import PRESETS, validate_changes
        raw = [{"setting": k, "proposed": v}
               for k, v in PRESETS["super_legend_v2"]["values"].items()]
        changes, dropped = validate_changes(raw)
        self.assertEqual(dropped, [], f"every value must be in bounds: {dropped}")
        self.assertGreater(len(changes), 15)

    def test_payoff_ratio_beats_super_legend(self):
        """R:R must be > 1. A scalper that risks more than it targets loses."""
        from desk.settings import PRESETS

        def rr(name):
            v = PRESETS[name]["values"]
            f1, f2 = v["scalp.tp1_frac"], v["scalp.tp2_frac"]
            tp = (f1 * v["scalp.tp1_at"] + f2 * v["scalp.tp2_at"]) / (f1 + f2)
            return (tp - 1.0) / v["scalp.stop_pct"]

        self.assertGreater(rr("super_legend_v2"), 1.0)
        self.assertGreater(rr("super_legend_v2"), rr("super_legend_scalp") * 3)

    def test_entries_stay_as_loose_as_super_legend(self):
        """v2 must not quietly re-tighten the gates that produced the win rate."""
        from desk.settings import PRESETS
        v2 = PRESETS["super_legend_v2"]["values"]
        sup = PRESETS["super_legend_scalp"]["values"]
        for key in ("scan.min_age_minutes", "scan.min_mcap_usd",
                    "scan.min_liquidity_usd", "scan.min_trades_h1",
                    "meme.min_holders", "meme.min_community_score",
                    "analyst.valid_score", "analyst.max_red_flags",
                    "signal.min_confidence", "signal.require_bullish",
                    "entry.max_price_gap_pct"):
            self.assertEqual(v2[key], sup[key], key)

    def test_dead_token_floors_are_restored(self):
        """0 volume / 0 trades / $1B mcap is what turns a 12% stop into a gap."""
        from desk.settings import PRESETS
        v2 = PRESETS["super_legend_v2"]["values"]
        self.assertGreater(v2["swing.min_volume_h1_usd"], 0)
        self.assertGreater(v2["swing.min_trades_h1"], 0)
        self.assertLessEqual(v2["scan.max_mcap_usd"], 50_000_000)

    def test_hold_is_shorter_than_super_legend(self):
        """60 min on a scalper parks capital in a dead token."""
        from desk.settings import PRESETS
        v2 = PRESETS["super_legend_v2"]["values"]
        sup = PRESETS["super_legend_scalp"]["values"]
        self.assertLess(v2["scalp.max_hold_minutes"], sup["scalp.max_hold_minutes"])
        self.assertLessEqual(v2["scalp.timeout_minutes"], v2["scalp.max_hold_minutes"])



class GuideScalpTest(unittest.TestCase):
    """The BONKbot playbook, translated to Solana.

    The guide (bonkbot.io/library/meme-coin-scalping-strategy) tells you to
    scalp 0.5-1% moves. That advice is written for CEXs -- it names Binance,
    Coinbase and OKX -- where the round trip costs about 0.1%. On a Solana DEX
    the round trip costs about 0.5% in fees and real AMM impact, so a 1%
    target leaves nothing and a 0.5% target is a guaranteed loss. Replaying the
    guide's literal numbers over 4,200 real entries gave a 0.0% win rate: every
    single "win" was smaller than the cost of taking it.

    These assertions pin the correction in place: keep the guide's discipline
    (fast exits, tight risk, real liquidity floors) but move the targets far
    enough above cost that a win is actually a win.
    """

    def test_every_value_is_in_bounds(self):
        from desk.settings import PRESETS, validate_changes
        raw = [{"setting": k, "proposed": v}
               for k, v in PRESETS["guide_scalp"]["values"].items()]
        changes, dropped = validate_changes(raw)
        self.assertEqual(dropped, [], f"every value must be in bounds: {dropped}")
        self.assertGreater(len(changes), 15)

    def test_scalp_block_is_actually_enabled(self):
        """exits.profile() ignores every scalp.* key unless this is True.

        This is a live trap: a preset can look like it sets a 60-minute hold
        and a 20% stop while the engine silently runs the swing profile.
        """
        # FIXED: the lane is now deliberately OFF (its tiny stop overrode the ladder and lost),
        # so the dip entry + ladder decide the exits instead.
        from desk.settings import PRESETS
        v = PRESETS["guide_scalp"]["values"]
        self.assertFalse(v["scalp.enabled"])
        self.assertTrue(v["meme.pullback_entry"])
        self.assertEqual(v["exits.ladder"], "fast")

    def test_targets_clear_the_round_trip_cost(self):
        """A 0.5-1% target is what killed the guide's literal numbers."""
        from desk.settings import PRESETS
        v = PRESETS["guide_scalp"]["values"]
        # Realistic round trip on a Solana DEX: ~0.5% fees + AMM impact.
        cost_pct = 0.005
        self.assertGreater(v["scalp.tp1_at"] - 1.0, cost_pct * 5)
        self.assertGreater(v["scalp.tp2_at"] - 1.0, v["scalp.tp1_at"] - 1.0)

    def test_payoff_ratio_is_above_one(self):
        from desk.settings import PRESETS
        v = PRESETS["guide_scalp"]["values"]
        f1, f2 = v["scalp.tp1_frac"], v["scalp.tp2_frac"]
        tp = (f1 * v["scalp.tp1_at"] + f2 * v["scalp.tp2_at"]) / (f1 + f2)
        self.assertGreater((tp - 1.0) / v["scalp.stop_pct"], 1.0)

    def test_hold_never_exceeds_one_hour(self):
        """The guide is a scalper. A 25-min hold on a 1h bar is a bug, not a
        scalp: max-hold fires inside the first candle and every trade dies."""
        from desk.settings import PRESETS
        v = PRESETS["guide_scalp"]["values"]
        self.assertLessEqual(v["scalp.max_hold_minutes"], 60)
        self.assertLessEqual(v["scalp.timeout_minutes"], v["scalp.max_hold_minutes"])

    def test_liquidity_floor_is_real(self):
        """The guide's first filter is 'is there enough liquidity to get out'."""
        from desk.settings import PRESETS
        v = PRESETS["guide_scalp"]["values"]
        self.assertGreaterEqual(v["scan.min_liquidity_usd"], 10_000)
        self.assertGreaterEqual(v["scalp.min_liquidity_usd"], 10_000)
        self.assertGreater(v["scan.min_volume_h1_usd"], 0)
        self.assertGreater(v["scan.min_trades_h1"], 0)



class UltraScalpTest(unittest.TestCase):
    """The operator's on-chain scalping spec, as a preset.

    Replaces the generic article settings ("take 0.5-1%", "a few million in
    daily volume") which cannot work on-chain: fees, priority fees and AMM
    impact take 1-3% per round trip, so a 1% target is underwater on entry.
    These assertions pin the numbers that do survive that cost.
    """

    def setUp(self):
        from desk.settings import PRESETS
        self.v = PRESETS["ultra_scalp"]["values"]

    def test_every_value_is_in_bounds(self):
        from desk.settings import PRESETS, validate_changes
        raw = [{"setting": k, "proposed": v}
               for k, v in PRESETS["ultra_scalp"]["values"].items()]
        changes, dropped = validate_changes(raw)
        self.assertEqual(dropped, [], f"every value must be in bounds: {dropped}")
        self.assertGreater(len(changes), 15)

    def test_scalp_block_is_enabled_and_swing_is_off(self):
        """exits.profile() ignores every scalp.* key unless enabled is True."""
        # FIXED: lane off, main lane on with the dip entry; the 30% ladder stop decides exits.
        self.assertFalse(self.v["scalp.enabled"])
        self.assertTrue(self.v["swing.enabled"])
        self.assertTrue(self.v["meme.pullback_entry"])
        self.assertEqual(self.v["trading.style"], "scalp")

    def test_targets_clear_a_real_round_trip(self):
        """1-3% is the real round-trip cost; a target under it is a loss."""
        self.assertGreater(self.v["scalp.tp1_at"] - 1.0, 0.03)
        self.assertGreater(self.v["scalp.tp2_at"] - 1.0, self.v["scalp.tp1_at"] - 1.0)

    def test_risk_reward_is_about_one_to_one_point_five(self):
        f1, f2 = self.v["scalp.tp1_frac"], self.v["scalp.tp2_frac"]
        tp = (f1 * self.v["scalp.tp1_at"] + f2 * self.v["scalp.tp2_at"]) / (f1 + f2)
        rr = (tp - 1.0) / self.v["scalp.stop_pct"]
        self.assertGreaterEqual(rr, 1.5)
        self.assertLessEqual(rr, 2.5)

    def test_scale_out_is_half_at_tp1(self):
        self.assertAlmostEqual(self.v["scalp.tp1_frac"], 0.5)
        self.assertAlmostEqual(self.v["scalp.tp2_frac"], 0.5)

    def test_time_stop_is_minutes_not_hours(self):
        """A scalper's edge is turnover. 3-5 minutes, not an hour."""
        self.assertLessEqual(self.v["scalp.timeout_minutes"], 5)
        self.assertLessEqual(self.v["scalp.timeout_minutes"],
                             self.v["scalp.max_hold_minutes"])

    def test_age_window_is_15m_to_6h(self):
        """Under 15m is sniper/rug territory; over 6h is past the momentum."""
        self.assertGreaterEqual(self.v["scan.min_age_minutes"], 15)
        self.assertLessEqual(self.v["scan.max_age_hours"], 6)
        self.assertEqual(self.v["scalp.max_age_minutes"], 360)

    def test_liquidity_and_mcap_band(self):
        self.assertGreaterEqual(self.v["scan.min_liquidity_usd"], 30_000)
        self.assertGreaterEqual(self.v["scan.min_mcap_usd"], 100_000)
        self.assertLessEqual(self.v["scan.max_mcap_usd"], 3_000_000)
        self.assertLess(self.v["scan.min_mcap_usd"], self.v["scan.max_mcap_usd"])

    def test_holder_floor_and_no_vertical_chase(self):
        self.assertGreaterEqual(self.v["meme.min_holders"], 300)
        self.assertLessEqual(self.v["scan.max_h1_change_pct"], 300)
        self.assertLessEqual(self.v["entry.max_price_gap_pct"], 20)

    def test_sell_side_floor_rejects_honeypots(self):
        """A token nobody can sell is a honeypot, not a discount."""
        self.assertGreaterEqual(self.v["scan.min_sells_ratio_h1"], 0.40)

    def test_three_losses_pause_the_day(self):
        self.assertEqual(self.v["breaker.streak_pause"], 3)
        self.assertEqual(self.v["breaker.streak_reduce"], 2)
        self.assertFalse(self.v["risk.auto_continue_after_daily_stop"])

    def test_position_size_is_one_to_two_percent(self):
        self.assertLessEqual(self.v["risk.max_position_pct"], 0.02)
        self.assertGreaterEqual(self.v["risk.max_position_pct"], 0.01)

    def test_volume_decay_exit_is_armed(self):
        self.assertLessEqual(self.v["exits.volume_decay_ratio"], 0.5)


if __name__ == "__main__":
    unittest.main()
