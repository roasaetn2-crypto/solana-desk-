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


if __name__ == "__main__":
    unittest.main()
