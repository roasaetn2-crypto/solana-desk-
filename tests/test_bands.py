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
        cases = [
            coin(),                                        # fresh 45k
            coin(mcap_usd=8_000, age_minutes=2),           # tiny and brand new
            coin(mcap_usd=5_000_000, age_minutes=30),      # big and fresh
            coin(mcap_usd=300_000, age_minutes=60),        # 1h old
            coin(mcap_usd=1_000_000, age_minutes=720),     # 12h old
            coin(mcap_usd=800_000, age_minutes=7_200),     # 5d old
            coin(liquidity_usd=2_500),                     # thin
            coin(volume_m5=0),                             # no 5m print yet
            coin(buys_h1=3, sells_h1=2),                   # 5 trades
            coin(change_h1=800),                           # vertical
        ]
        for c in cases:
            self.assertIsNone(scanner.free_kill(c),
                              f"legend scalper should trade this: {c}")

    def test_protective_cuts_survive(self):
        self.assertEqual(scanner.free_kill(coin(mcap_usd=3_000)), "mcap")
        self.assertEqual(scanner.free_kill(coin(buys_h1=100, sells_h1=0)), "no_sells")
        self.assertEqual(scanner.free_kill(coin(age_minutes=9 * 24 * 60)), "age")

    def test_legend_relaxes_the_starving_gates(self):
        from config import MEME, SIGNAL
        self.assertFalse(MEME.get("pullback_entry"))
        self.assertEqual(SIGNAL["min_confidence"], 30)
        self.assertFalse(SIGNAL["require_bullish"])


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
        self.assertGreater(len(changes), 30)

    def test_every_reasonable_coin_reaches_the_broker(self):
        cases = [
            coin(),
            coin(mcap_usd=8_000, age_minutes=2),
            coin(mcap_usd=5_000_000, age_minutes=30),
            coin(mcap_usd=300_000, age_minutes=60),
            coin(mcap_usd=1_000_000, age_minutes=720),
            coin(mcap_usd=800_000, age_minutes=7_200),
            coin(liquidity_usd=2_500),
            coin(volume_m5=0),
            coin(buys_h1=3, sells_h1=2),
            coin(change_h1=800),
            coin(mcap_usd=400_000_000, age_minutes=90),   # huge, legend refused it
            coin(volume_h1=0, buys_h1=0, sells_h1=0),    # dead volume
        ]
        for c in cases:
            self.assertIsNone(scanner.free_kill(c),
                              f"super legend should trade this: {c}")

    def test_protective_cuts_survive(self):
        self.assertEqual(scanner.free_kill(coin(mcap_usd=3_000)), "mcap")
        self.assertEqual(scanner.free_kill(coin(buys_h1=100, sells_h1=0)), "no_sells")
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
        self.assertGreater(len(changes), 30)

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


if __name__ == "__main__":
    unittest.main()
