"""The established lane: over-age pools may trade, but only if still alive.

Why this file exists. The pullback edge was measured on 120 pools, all older
than 7 days, while the desk's age gate is 48h -- so the measured population and
the traded population had no overlap. The lane closes that gap. These tests pin
the two things that could silently break it:

  1. the age gate must FORGIVE age only for pools that clear the depth floors;
  2. the feed absorbers must let those pools INTO the candidate pool at all,
     otherwise the lane is unreachable and the desk looks enabled while
     discarding every pool it claims to trade.
"""
from __future__ import annotations

import unittest
from unittest import mock

import config
from desk import risk, scanner


def pool(age_minutes=600, liquidity=100_000, volume=50_000, buys=80, sells=70):
    return {
        "age_minutes": age_minutes,
        "liquidity_usd": liquidity,
        "volume_h1": volume,
        "buys_h1": buys,
        "sells_h1": sells,
        "mcap_usd": 500_000,
        "change_h1": 5.0,
    }


class EstablishedGate(unittest.TestCase):
    def test_a_young_pool_is_untouched_by_the_lane(self):
        # 10h old: inside the launch window, so the lane must not even look.
        self.assertIsNone(scanner.free_kill(pool(age_minutes=600)))

    def test_an_old_pool_that_is_still_alive_passes(self):
        # 5 days old but deep, liquid and busy: this is the pool we want.
        self.assertIsNone(scanner.free_kill(pool(age_minutes=5 * 24 * 60)))

    def test_an_old_pool_without_depth_is_still_rejected(self):
        # The whole point: age is forgiven, but not for free.
        t = pool(age_minutes=5 * 24 * 60, liquidity=10_000)
        self.assertEqual(scanner.free_kill(t), "age")

    def test_an_old_pool_without_volume_is_still_rejected(self):
        t = pool(age_minutes=5 * 24 * 60, volume=1_000)
        self.assertEqual(scanner.free_kill(t), "age")

    def test_an_old_pool_without_trades_is_still_rejected(self):
        # Deep and "voluminous" but nothing is actually trading: a bot loop.
        t = pool(age_minutes=5 * 24 * 60, buys=5, sells=5)
        self.assertEqual(scanner.free_kill(t), "age")

    def test_beyond_the_lane_cap_even_a_live_pool_is_rejected(self):
        # 30 days: past ESTABLISHED["max_age_hours"], the candles stop being a range.
        t = pool(age_minutes=30 * 24 * 60)
        self.assertEqual(scanner.free_kill(t), "age")

    def test_a_pool_younger_than_min_age_is_rejected(self):
        self.assertEqual(scanner.free_kill(pool(age_minutes=0)), "age")

    def test_lane_off_restores_the_hard_48h_cut(self):
        with mock.patch.dict(config.ESTABLISHED, {"enabled": False}):
            self.assertEqual(scanner.free_kill(pool(age_minutes=5 * 24 * 60)), "age")
            self.assertEqual(scanner.age_cut_minutes(),
                             config.SCAN["max_age_hours"] * 60)


class AbsorbersAdmitOldPools(unittest.TestCase):
    """If the absorbers keep the 48h cut, free_kill never sees an old pool."""

    def test_age_cut_widens_to_the_lane_cap(self):
        self.assertEqual(scanner.age_cut_minutes(),
                         config.ESTABLISHED["max_age_hours"] * 60)

    def test_gt_absorber_admits_an_old_pool_that_free_kill_would_accept(self):
        old = pool(age_minutes=5 * 24 * 60)
        body = {"data": [], "included": []}
        with mock.patch.object(scanner, "parse_pool", return_value=old), \
             mock.patch.object(scanner, "_keep", side_effect=lambda x: x), \
             mock.patch.object(scanner, "_put_pool") as put:
            body["data"] = [{"id": "x"}]
            n = scanner._absorb_gt(body, now=0.0, age_cut=True)
        self.assertEqual(n, 1)
        put.assert_called_once()

    def test_gt_absorber_still_drops_a_dead_old_pool(self):
        # Past the lane cap: must not even enter the pool.
        dead = pool(age_minutes=30 * 24 * 60)
        body = {"data": [{"id": "x"}], "included": []}
        with mock.patch.object(scanner, "parse_pool", return_value=dead), \
             mock.patch.object(scanner, "_keep", side_effect=lambda x: x), \
             mock.patch.object(scanner, "_put_pool") as put:
            n = scanner._absorb_gt(body, now=0.0, age_cut=True)
        self.assertEqual(n, 0)
        put.assert_not_called()


class EstablishedSizing(unittest.TestCase):
    def test_scale_is_half_for_an_over_age_pool(self):
        over = config.SCAN["max_age_hours"] * 60 + 60
        self.assertEqual(risk.established_size_scale(over),
                         config.ESTABLISHED["size_scale"])

    def test_scale_is_full_for_a_fresh_pool(self):
        self.assertEqual(risk.established_size_scale(600), 1.0)

    def test_unknown_age_sizes_normally(self):
        # Guessing would halve a fresh launch by accident.
        self.assertEqual(risk.established_size_scale(None), 1.0)

    def test_lane_off_means_full_size_even_when_old(self):
        over = config.SCAN["max_age_hours"] * 60 + 60
        with mock.patch.dict(config.ESTABLISHED, {"enabled": False}):
            self.assertEqual(risk.established_size_scale(over), 1.0)

    def test_the_ticket_is_actually_halved_end_to_end(self):
        """The scale must reach the ticket, not just exist as a helper."""
        from desk.risk import Account
        thesis = mock.Mock(verdict="valid_setup", score=100)
        acct = Account(equity=10_000, day_pnl=0, open_positions=0,
                       realized_equity=10_000, cash=10_000)
        over = config.SCAN["max_age_hours"] * 60 + 60
        _, fresh, _ = risk.check_token_trade(thesis, acct, 5_000_000,
                                             stop_pct=0.30, age_minutes=600)
        _, old, _ = risk.check_token_trade(thesis, acct, 5_000_000,
                                           stop_pct=0.30, age_minutes=over)
        self.assertGreater(fresh, 0)
        self.assertAlmostEqual(old, round(fresh * config.ESTABLISHED["size_scale"], 2),
                               places=2)


if __name__ == "__main__":
    unittest.main()
