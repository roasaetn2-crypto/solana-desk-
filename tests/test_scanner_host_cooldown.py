"""A 429 is per client IP: it must cool the whole host, persist across cycles, and log once."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from desk import dexscreener, promo, scanner
from test_scanner_sources import FakeResp


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class HostCooldown(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()
        promo.reset()
        self.clock = Clock()
        self.urls = []
        self.notes = []

    def _router(self, retry_after=None, ok_hosts=()):
        def router(url, params=None, headers=None, timeout=None):
            self.urls.append(url)
            if any(h in url for h in ok_hosts):
                return FakeResp(200, {"data": [], "included": [], "pairs": []})
            return FakeResp(429, reason="Too Many Requests",
                            headers={"Retry-After": retry_after} if retry_after else {})
        return router

    def _run(self, cycles, session, step=30):
        real_note = scanner._note

        def note(source, message):
            self.notes.append(message)
            real_note(source, message)

        with mock.patch("time.time", self.clock), mock.patch.object(scanner, "_note", note):
            for _ in range(cycles):
                scanner.new_tokens(session)
                self.clock.t += step

    def test_all_endpoints_429_is_a_few_calls_not_one_per_cycle_per_endpoint(self):
        s = mock.Mock()
        s.get.side_effect = self._router()
        self._run(20, s)  # 10 minutes of 30s cycles, upstream 429s everything
        gt = [u for u in self.urls if "geckoterminal" in u]
        ds = [u for u in self.urls if "dexscreener" in u]
        # Backoff 30s -> 60s -> 120s (cap): about 7 probes in 10 minutes per host.
        self.assertLessEqual(len(gt), 8)
        self.assertLessEqual(len(ds), 8)
        # One log line per cooldown start, per host, never one per endpoint.
        self.assertEqual(len(self.notes), len(gt) + len(ds))
        self.assertEqual(scanner.CALLS["429"], len(gt) + len(ds))

    def test_429_on_one_endpoint_skips_the_other_endpoints_of_that_host(self):
        s = mock.Mock()
        s.get.side_effect = self._router()
        self._run(1, s, step=0)
        before = len(self.urls)
        self.assertTrue(scanner.host_cooling("geckoterminal", self.clock.t))
        self.assertTrue(scanner.is_cooling("gt:new_pools:2", self.clock.t))
        self.assertTrue(scanner.is_cooling("ds:profiles", self.clock.t))
        self._run(1, s, step=0)
        self.assertEqual(len(self.urls), before)

    def test_retry_after_is_honored_and_cooldown_persists_across_cycles(self):
        s = mock.Mock()
        s.get.side_effect = self._router(retry_after="100")
        self._run(1, s, step=0)
        self.assertGreaterEqual(scanner.host_cooling_until("geckoterminal", self.clock.t) - self.clock.t, 60)
        n = len(self.urls)
        self.clock.t += 30
        self._run(3, s)  # 30..90s later, still inside the 100s window
        self.assertEqual(len(self.urls), n)
        self.assertEqual(len(self.notes), 2)  # one per host, nothing repeated

    def test_success_resets_the_backoff(self):
        s = mock.Mock()
        s.get.side_effect = self._router()
        self._run(1, s)
        self.assertEqual(scanner._HOST_COOL["geckoterminal"]["strikes"], 1)
        s.get.side_effect = self._router(ok_hosts=("geckoterminal", "dexscreener"))
        self.clock.t += 400
        self._run(1, s)
        self.assertFalse(scanner.host_cooling("geckoterminal", self.clock.t))
        self.assertEqual(scanner._HOST_COOL["geckoterminal"]["strikes"], 0)

    def test_gt_schedule_is_page_one_plus_one_rotated_slot(self):
        for c in range(8):
            gt = [e["id"] for e in scanner._schedule(c) if e["source"] == "geckoterminal"]
            self.assertEqual(len(gt), 1 + config.SCAN["pages"])
            self.assertIn("gt:new_pools:1", gt)

    def test_direct_callers_respect_and_feed_the_host_cooldown(self):
        s = mock.Mock()
        s.get.side_effect = self._router()
        with mock.patch("time.time", self.clock):
            self.assertIsNone(dexscreener.best_pair("MINTX", s))
            self.assertEqual(len(self.urls), 1)
            self.assertTrue(scanner.host_cooling("dexscreener"))
            self.assertIsNone(dexscreener.best_pair("MINTY", s))      # skipped quietly
            promo.refresh(s, now=self.clock.t)                         # skipped quietly
        self.assertEqual(len(self.urls), 1)


if __name__ == "__main__":
    unittest.main()
