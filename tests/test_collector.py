"""The collector: discover young, harvest when the pool has aged into the band.

Why these tests exist. ``new_pools`` is a rolling ~6-minute feed (measured: 160
pools over 8 pages, all under 0.1h old), so a one-shot harvest can never see a
6-48h pool. The collector's whole premise is that a pool remembered *now* becomes
harvestable *later*. If that hand-off breaks, the store silently stops growing
and nothing errors -- the worst kind of failure. These tests pin the hand-off
with an injected clock instead of waiting six real hours.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import collector  # noqa: E402
from desk.poolstore import PoolStore  # noqa: E402

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def rec(addr="POOL1", created=None, name="TEST/SOL"):
    return {"address": addr, "name": name,
            "created_at": iso(created or T0),
            "liq": 120_000, "mcap": 900_000, "vol_h1": 40_000,
            "buys_h1": 200, "sells_h1": 180}


def bars(n=80):
    return [{"ts": int(T0.timestamp()) + i * 300, "open": 1.0, "high": 1.05,
             "low": 0.98, "close": 1.01, "volume": 100.0} for i in range(n)]


class WatchlistHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "watchlist.json")
        self.w = collector.Watchlist(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_fresh_pool_is_watched_but_not_due(self):
        self.w.add(rec())
        self.assertEqual(len(self.w.pools), 1)
        self.assertEqual(self.w.due(now=T0 + timedelta(hours=1)), [])

    def test_the_same_pool_becomes_due_once_it_ages_into_the_band(self):
        """The core promise: remembered at 0h, harvestable at 7h."""
        self.w.add(rec())
        self.assertEqual(self.w.due(now=T0 + timedelta(hours=5)), [])
        due = self.w.due(now=T0 + timedelta(hours=7))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["address"], "POOL1")

    def test_a_pool_past_the_window_is_not_due(self):
        self.w.add(rec())
        self.assertEqual(self.w.due(now=T0 + timedelta(hours=60)), [])

    def test_due_is_oldest_first_so_the_most_bars_are_fetched_first(self):
        self.w.add(rec("OLD", created=T0 - timedelta(hours=20)))
        self.w.add(rec("NEW", created=T0 - timedelta(hours=7)))
        due = self.w.due(now=T0)
        self.assertEqual([p["address"] for p in due], ["OLD", "NEW"])

    def test_a_pool_with_no_created_at_is_never_due(self):
        # Without a timestamp we cannot know its age; guessing would harvest noise.
        self.w.add({"address": "X", "name": "X/SOL", "created_at": None})
        self.assertEqual(self.w.due(now=T0 + timedelta(hours=10)), [])

    def test_adding_the_same_pool_twice_does_not_duplicate(self):
        self.assertTrue(self.w.add(rec()))
        self.assertFalse(self.w.add(rec()))
        self.assertEqual(len(self.w.pools), 1)

    def test_a_harvested_pool_is_not_due_again(self):
        self.w.add(rec())
        self.w.pools["POOL1"]["harvested_at"] = iso(T0 + timedelta(hours=7))
        self.assertEqual(self.w.due(now=T0 + timedelta(hours=8)), [])

    def test_prune_drops_pools_that_aged_out_unharvested(self):
        self.w.add(rec("DEAD", created=T0 - timedelta(hours=80)))
        self.w.add(rec("LIVE", created=T0))
        pruned = self.w.prune(now=T0)
        self.assertEqual(pruned, 1)
        self.assertIn("LIVE", self.w.pools)
        self.assertNotIn("DEAD", self.w.pools)

    def test_prune_keeps_a_harvested_pool_even_when_old(self):
        self.w.add(rec("DONE", created=T0 - timedelta(hours=200)))
        self.w.pools["DONE"]["harvested_at"] = iso(T0)
        self.assertEqual(self.w.prune(now=T0), 0)
        self.assertIn("DONE", self.w.pools)

    def test_watchlist_survives_a_reload(self):
        """A phone killed mid-run must not lose what it already learned."""
        self.w.add(rec())
        self.w.save()
        again = collector.Watchlist(self.path)
        self.assertEqual(len(again.pools), 1)
        self.assertEqual(again.pools["POOL1"]["name"], "TEST/SOL")

    def test_a_corrupt_watchlist_does_not_crash_the_run(self):
        Path(self.path).write_text("{not json", encoding="utf-8")
        w = collector.Watchlist(self.path)
        self.assertEqual(w.pools, {})


class HarvestPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watch = collector.Watchlist(str(Path(self.tmp.name) / "w.json"))
        self.store = PoolStore(str(Path(self.tmp.name) / "s.json"), max_bars=1000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_aged_pool_is_fetched_and_stored(self):
        self.watch.add(rec(created=T0 - timedelta(hours=8)))
        with mock.patch.object(collector, "_now", return_value=T0), \
             mock.patch.object(collector.ph, "fetch_ohlcv", return_value=bars(80)) as f:
            added, failed = collector.harvest_due(None, self.store, self.watch, None,
                                                  max_fetch=5,
                                                  min_age_h=6, max_age_h=48)
        self.assertEqual((added, failed), (1, 0))
        f.assert_called_once()
        self.assertEqual(self.store.count(70), 1)
        self.assertIsNotNone(self.watch.pools["POOL1"]["harvested_at"])

    def test_a_young_pool_is_not_fetched(self):
        self.watch.add(rec(created=T0 - timedelta(hours=1)))
        with mock.patch.object(collector, "_now", return_value=T0), \
             mock.patch.object(collector.ph, "fetch_ohlcv") as f:
            added, failed = collector.harvest_due(None, self.store, self.watch, None,
                                                  max_fetch=5, min_age_h=6, max_age_h=48)
        self.assertEqual((added, failed), (0, 0))
        f.assert_not_called()

    def test_a_pool_with_too_few_bars_is_not_stored_and_counts_an_attempt(self):
        self.watch.add(rec(created=T0 - timedelta(hours=8)))
        with mock.patch.object(collector, "_now", return_value=T0), \
             mock.patch.object(collector.ph, "fetch_ohlcv", return_value=bars(10)):
            added, failed = collector.harvest_due(None, self.store, self.watch, None,
                                                  max_fetch=5, min_age_h=6, max_age_h=48)
        self.assertEqual((added, failed), (0, 1))
        self.assertEqual(self.store.count(70), 0)
        self.assertEqual(self.watch.pools["POOL1"]["attempts"], 1)
        self.assertIsNone(self.watch.pools["POOL1"]["harvested_at"])

    def test_a_pool_is_abandoned_after_max_attempts(self):
        self.watch.add(rec(created=T0 - timedelta(hours=8)))
        self.watch.pools["POOL1"]["attempts"] = collector.MAX_ATTEMPTS
        with mock.patch.object(collector, "_now", return_value=T0), \
             mock.patch.object(collector.ph, "fetch_ohlcv") as f:
            added, failed = collector.harvest_due(None, self.store, self.watch, None,
                                                  max_fetch=5, min_age_h=6, max_age_h=48)
        self.assertEqual((added, failed), (0, 0))
        f.assert_not_called()

    def test_max_fetch_caps_a_single_run(self):
        """A phone run must stay short: never fetch the whole watchlist at once."""
        for i in range(6):
            self.watch.add(rec(f"P{i}", created=T0 - timedelta(hours=8)))
        with mock.patch.object(collector, "_now", return_value=T0), \
             mock.patch.object(collector.ph, "fetch_ohlcv", return_value=bars(80)) as f:
            added, _ = collector.harvest_due(None, self.store, self.watch, None,
                                             max_fetch=2, min_age_h=6, max_age_h=48)
        self.assertEqual(added, 2)
        self.assertEqual(f.call_count, 2)


class DiscoverPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watch = collector.Watchlist(str(Path(self.tmp.name) / "w.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_discover_remembers_every_pool_in_the_feed(self):
        body = {"data": [{"id": "solana_a"}, {"id": "solana_b"}]}
        with mock.patch.object(collector.ph, "get", return_value=body), \
             mock.patch.object(collector.ph, "pool_record",
                               side_effect=lambda r: {"address": r["id"],
                                                      "created_at": iso(T0)}):
            n = collector.discover(None, 1, None, self.watch)
        self.assertEqual(n, 2)
        self.assertEqual(len(self.watch.pools), 2)

    def test_discover_stops_on_an_empty_page(self):
        with mock.patch.object(collector.ph, "get", return_value={"data": []}) as g:
            n = collector.discover(None, 5, None, self.watch)
        self.assertEqual(n, 0)
        self.assertEqual(g.call_count, 1)


if __name__ == "__main__":
    unittest.main()
