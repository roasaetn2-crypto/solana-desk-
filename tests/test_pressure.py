"""Call-pressure and last-good fixes (PAPER only):

1. a finalist's last good 5m counts / bars are reused for a few minutes while the sources cool, tagged cache;
2. rulebook.tx_ratio_unknown_pass (default OFF, Confirm-only): an UNKNOWN tx_ratio passes in paper;
3. the scanner spends less of the shared API budget on discovery: fewer pages, slower rotation, one Jupiter
   hydrate per gap with dedupe, and a harder 429 backoff (exponential, jitter, Retry-After, one probe);
4. scalp.max_age_minutes may be Confirmed up to 7 days (value and default untouched).
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import test_rulebook as tr  # noqa: E402
from config import LAST_GOOD, RULEBOOK, SCALP, SCAN_FETCH  # noqa: E402
from desk import chart, dip_recovery as dr, exits, marketdata, pricetape, pumpportal, scanner, settings  # noqa: E402
from legacy_book import legacy_book  # noqa: E402
from test_second_source import MINT, jup_row, put_row  # noqa: E402

NOW = 1_800_000_000.0
T = {"mint": MINT, "pool": MINT}


class Base(unittest.TestCase):
    def setUp(self):
        scanner.reset_runtime()
        pricetape.reset()
        p = mock.patch("desk.netutil.offline", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(scanner.reset_runtime)
        self.addCleanup(pricetape.reset)
        self.addCleanup(chart.reset)


# ---------------------------------------------------------------- 1. last-good cache
class LastGoodTx(Base):
    def miss(self, now=NOW):
        with mock.patch.object(scanner, "jup_tx_m5", return_value=(None, "jup_cooling")), \
                mock.patch.object(scanner, "gt_tx_m5", return_value=None), \
                mock.patch.object(scanner, "gt_tx_cause", return_value="gt_cooling"):
            return marketdata.tx_m5(T, {"available": False}, None, now, fetch=False)

    def test_defaults_are_five_minutes_in_config(self):
        self.assertEqual(LAST_GOOD["tx_m5_max_age_s"], 300)
        self.assertEqual(LAST_GOOD["bars_max_age_s"], 300)

    def test_a_miss_reuses_the_last_good_counts_and_says_so(self):
        marketdata.remember_tx(MINT, 40, 20, "dexscreener", NOW - 90)
        r = self.miss()
        self.assertTrue(r["available"])
        self.assertEqual((r["buys_m5"], r["sells_m5"]), (40, 20))
        self.assertEqual(r["source"], "cache")
        self.assertEqual(r["via"], "dexscreener")
        self.assertEqual(r["age_s"], 90.0)
        self.assertIn("cache:age_90s", r["chain"])

    def test_too_old_counts_are_not_reused_and_the_cause_stays(self):
        marketdata.remember_tx(MINT, 40, 20, "dexscreener", NOW - 301)
        r = self.miss()
        self.assertFalse(r["available"])
        self.assertIn("gt_cooling", r["cause"])

    def test_the_window_is_configurable(self):
        marketdata.remember_tx(MINT, 40, 20, "dexscreener", NOW - 100)
        with mock.patch.dict(LAST_GOOD, {"tx_m5_max_age_s": 60}):
            self.assertFalse(self.miss()["available"])
        with mock.patch.dict(LAST_GOOD, {"tx_m5_max_age_s": 600}):
            self.assertTrue(self.miss()["available"])

    def test_zero_counts_and_cache_reads_are_never_remembered(self):
        marketdata.remember_tx(MINT, 0, 0, "dexscreener", NOW - 5)
        marketdata.remember_tx(MINT, 9, 9, "cache", NOW - 5)
        self.assertIsNone(marketdata.last_tx(MINT, NOW))

    def test_a_fresh_read_overwrites_and_is_remembered(self):
        with mock.patch.object(scanner, "jup_tx_m5", return_value=({"available": True, "source": "jupiter",
                                                                    "buys_m5": 30, "sells_m5": 10}, None)), \
                mock.patch.object(marketdata, "ensure_jup", return_value=None):
            r = marketdata.tx_m5(T, {"available": False}, None, NOW, fetch=False)
        self.assertEqual(r["source"], "jupiter")
        self.assertEqual(marketdata.last_tx(MINT, NOW + 10)["via"], "jupiter")

    def test_the_cache_is_bounded(self):
        for i in range(marketdata._LAST_MAX + 50):
            marketdata.remember_tx(f"M{i}", 5, 5, "jupiter", NOW)
        self.assertLessEqual(len(marketdata._LAST_TX), marketdata._LAST_MAX)
        self.assertIsNotNone(marketdata.last_tx(f"M{marketdata._LAST_MAX + 49}", NOW))


class LastGoodBars(Base):
    def gt_bars(self, now):
        cs = [{"t": int(now // 60) * 60 - 60 * i, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0 + 0.01 * (10 - i), "v": 5.0}
              for i in range(10, -1, -1)]
        return cs

    def test_both_sources_missing_reuses_recent_bars_tagged_cache(self):
        cs = self.gt_bars(NOW)
        with mock.patch.object(dr, "load_candles", return_value=(cs, NOW)):
            ok = marketdata.finalist_bars(MINT, None, NOW)
        self.assertEqual(ok["source"], "geckoterminal")
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch.object(dr, "candle_cause", return_value="gt_cooling"):
            r = marketdata.finalist_bars(MINT, None, NOW + 120)
        self.assertEqual(r["source"], "cache")
        self.assertEqual(r["via"], "geckoterminal")
        self.assertEqual(r["age_s"], 120.0)
        self.assertEqual(r["5m"], ok["5m"])
        self.assertIsNone(r["cause"])

    def test_old_bars_are_not_reused(self):
        with mock.patch.object(dr, "load_candles", return_value=(self.gt_bars(NOW), NOW)):
            marketdata.finalist_bars(MINT, None, NOW)
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch.object(dr, "candle_cause", return_value="gt_cooling"):
            r = marketdata.finalist_bars(MINT, None, NOW + 301)
        self.assertEqual((r["source"], r["5m"]), ("none", []))
        self.assertIn("gt_cooling", r["cause"])

    def test_the_state_resets_with_the_scanner_runtime(self):
        marketdata.remember_tx(MINT, 5, 5, "jupiter", NOW)
        scanner.reset_runtime()
        self.assertIsNone(marketdata.last_tx(MINT, NOW))


# ---------------------------------------------------------------- 2. tx_ratio_unknown_pass
class UnknownPassSetting(unittest.TestCase):
    def test_default_off_confirm_only_bool(self):
        self.assertIs(config.RULEBOOK.get("tx_ratio_unknown_pass"), False)
        self.assertIn('"tx_ratio_unknown_pass": False', Path(config.__file__).read_text())
        s = settings.REGISTRY["rulebook.tx_ratio_unknown_pass"]
        self.assertEqual(s.kind, "bool")
        self.assertFalse(settings._EXCLUDED_RE.search(s.key))
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"DESK_DATA_DIR": d}), \
                mock.patch.dict(RULEBOOK):
            settings.reset_runtime()
            out = settings.create([{"setting": "rulebook.tx_ratio_unknown_pass", "proposed": True}], "t", "desk_ai")
            self.assertTrue(out["ok"], out)
            self.assertFalse(RULEBOOK["tx_ratio_unknown_pass"])           # a proposal changes nothing
            settings.confirm(settings.listing()["pending"][0]["id"], "romz")
            self.assertTrue(RULEBOOK["tx_ratio_unknown_pass"])
            settings.reset_runtime()

    def test_the_excluded_pattern_still_blocks_real_password_keys(self):
        for k in ("desk.pass", "x.pass_phrase", "wallet.password", "rulebook.private_key"):
            self.assertTrue(settings._EXCLUDED_RE.search(k), k)


@legacy_book
class UnknownPassAgent(unittest.TestCase):
    run_agent = tr.AgentWiring.run_agent
    skips = tr.AgentWiring.skips

    def setUp(self):
        tr.on()
        scanner.reset_runtime()
        self.addCleanup(scanner.reset_runtime)

    def go(self, on, **kw):
        RULEBOOK["tx_ratio_unknown_pass"] = on
        with mock.patch.object(scanner, "gt_tx_m5", return_value=None):
            return self.run_agent(**{"ds": {"available": False}, **kw})

    def test_off_still_skips_unknown(self):
        a = self.go(False)
        self.assertTrue(self.skips(a)[0].startswith("tx_ratio_unknown:"))
        self.assertEqual(a.stats["entries"], 0)
        self.assertNotIn("tx_ratio_unknown_pass", a.stats)

    def test_on_passes_unknown_logs_it_and_enters(self):
        a = self.go(True)
        self.assertEqual(self.skips(a), [])
        self.assertEqual(a.stats["tx_ratio_unknown_pass"], 1)
        self.assertEqual(a.stats["entries"], 1, [e["message"] for e in list(a.events)[:8]])
        ev = [e for e in a.events if e.get("reason") == "tx_ratio_unknown_pass"]
        self.assertEqual(len(ev), 1)
        self.assertIn("tx_ratio_unknown_pass", ev[0]["message"])

    def test_a_known_low_ratio_still_fails(self):
        RULEBOOK["tx_ratio_unknown_pass"] = True
        low = {**tr.GOOD_DS, "buys_m5": 10, "sells_m5": 30}
        a = self.run_agent(ds=low)
        self.assertEqual(self.skips(a), ["tx_ratio_low"])
        self.assertNotIn("tx_ratio_unknown_pass", a.stats)

    def test_every_other_gate_still_applies(self):
        a = self.go(True, dev={**tr.GOOD_DEV, "lp_locked_pct": None})
        self.assertEqual(self.skips(a), ["lp_unknown"])
        self.assertEqual(a.stats["entries"], 0)

    def test_recent_cache_is_used_before_the_unknown_pass(self):
        a0 = self.go(False, ds=tr.GOOD_DS)                       # a good DexScreener read is remembered
        self.assertEqual(a0.stats["entries"], 1)
        a = self.go(False)                                        # now DexScreener is gone: the cache answers
        self.assertEqual(self.skips(a), [])
        self.assertEqual(a.stats["src_tx_cache"], 1)

    def test_paper_only_source_guard(self):
        src = Path(sys.modules["desk.solana_agent"].__file__).read_text()
        i = src.index('RULEBOOK.get("tx_ratio_unknown_pass"')
        self.assertIn('self.mode != "live"', src[max(0, i - 3000):i])


# ---------------------------------------------------------------- 3. call pressure
class Schedule(Base):
    def count(self, cycles):
        gt = ds = 0
        slots = set()
        for c in range(cycles):
            for e in scanner._schedule(c):
                if e["source"] == "geckoterminal":
                    gt += 1
                else:
                    ds += 1
                slots.add(e["id"])
        return gt, ds, slots

    def test_fewer_discovery_calls_than_the_old_one_slot_per_cycle(self):
        n = 40
        gt, ds, _ = self.count(n)
        old_gt = n * (1 + config.SCAN["pages"])
        self.assertLess(gt, old_gt * 0.8)
        self.assertLessEqual(ds, n // SCAN_FETCH["ds_every"])
        self.assertLess(ds, n)

    def test_new_pools_page_one_still_runs_every_cycle(self):
        for c in range(12):
            self.assertIn("gt:new_pools:1", [e["id"] for e in scanner._schedule(c)])

    def test_every_discovery_slot_is_still_reached(self):
        _, _, slots = self.count(60)
        self.assertEqual({s for s in slots if s.startswith("ds:")}, {e["id"] for e in scanner._ds_rotation()})

    def test_gt_pages_are_capped(self):
        self.assertEqual(SCAN_FETCH["gt_max_page"], 2)
        for e in scanner._gt_rotation():
            self.assertLessEqual(int(e["id"].rsplit(":", 1)[1]), SCAN_FETCH["gt_max_page"])

    def test_jupiter_lists_run_on_alternate_cycles_only(self):
        s = mock.Mock()
        urls = []

        def get(url, params=None, headers=None, timeout=None):
            urls.append(url)
            from test_scanner_sources import FakeResp
            return FakeResp(200, [] if "jup" in url else {"data": [], "pairs": []})
        s.get.side_effect = get
        with mock.patch.object(scanner, "_hydrate_pumpportal", lambda *a: 0):
            for _ in range(6):
                scanner.new_tokens(s)
        lists = [u for u in urls if "jup.ag" in u and ("toptrending" in u or "toporganic" in u or "recent" in u
                                                        or "toptraded" in u or "/tag" in u or "category" in u)]
        self.assertLessEqual(len(lists), 6 // SCAN_FETCH["jup_list_every"] + 1)


class Backoff(Base):
    def cool(self, wait=0.0, host="geckoterminal"):
        before = time.time()
        scanner._cool(f"{host[:2]}:t", host, wait, "lbl", 429, "TMR")
        return scanner._HOST_COOL[host]["until"] - before

    def test_each_strike_roughly_doubles_up_to_the_cap(self):
        floor, cap, j = SCAN_FETCH["cooldown_floor_s"], SCAN_FETCH["cooldown_cap_s"], SCAN_FETCH["cooldown_jitter"]
        seen = []
        for strike in range(1, 6):
            if strike > 1:
                scanner._HOST_COOL["geckoterminal"]["until"] = 0.0                  # the cooldown ended, no success yet
            d = self.cool()
            lo = min(cap, floor * 2 ** (strike - 1))
            self.assertGreaterEqual(d, lo - 1)
            self.assertLessEqual(d, lo * (1 + j) + 1)
            seen.append(d)
        self.assertGreater(seen[2], seen[0] * 2)
        self.assertLessEqual(max(seen), cap * (1 + j) + 1)

    def test_retry_after_beyond_the_cap_is_honoured_in_full(self):
        d = self.cool(wait=SCAN_FETCH["cooldown_cap_s"] * 2)
        self.assertGreaterEqual(d, SCAN_FETCH["cooldown_cap_s"] * 2 - 1)

    def test_jitter_spreads_the_hosts(self):
        vals = set()
        for _ in range(20):
            scanner.reset_runtime()
            vals.add(round(self.cool(), 3))
        self.assertGreater(len(vals), 10)

    def test_429s_are_counted_per_provider_once_per_cooldown(self):
        self.cool(host="geckoterminal")
        self.cool(host="geckoterminal")                           # already cooling: silent
        self.cool(host="jupiter")
        self.assertEqual(scanner.CALLS_429, {"geckoterminal": 1, "jupiter": 1})

    def test_after_the_cooldown_only_one_probe_goes_out_until_it_succeeds(self):
        self.cool()
        rec = scanner._HOST_COOL["geckoterminal"]
        now = rec["until"] + 1
        self.assertFalse(scanner._probe_blocked("geckoterminal", now))        # the probe
        self.assertTrue(scanner._probe_blocked("geckoterminal", now + 1))     # the rest wait
        self.assertFalse(scanner._probe_blocked("geckoterminal", now + SCAN_FETCH["probe_gap_s"] + 1))
        rec["strikes"] = 0                                                    # a success clears it
        self.assertFalse(scanner._probe_blocked("geckoterminal", now + 2))

    def test_a_host_that_never_struck_is_never_held(self):
        self.assertFalse(scanner._probe_blocked("dexscreener", NOW))


class Hydrate(Base):
    def setUp(self):
        super().setUp()
        self.now = time.time()

    def session(self):
        s = mock.Mock()
        self.queries = []

        def get(url, params=None, headers=None, timeout=None):
            from test_scanner_sources import FakeResp
            if url.endswith("/search"):
                self.queries.append((params or {}).get("query", "").split(","))
            return FakeResp(200, [])
        s.get.side_effect = get
        return s

    def mints(self, n):
        return [f"HY{i:03d}" + "x" * 36 for i in range(n)]

    def test_one_call_per_gap_with_a_capped_batch(self):
        ms = self.mints(120)
        s = self.session()
        with mock.patch.object(pumpportal, "recent_mints", return_value=ms):
            scanner._hydrate_pumpportal(s, self.now)
            scanner._hydrate_pumpportal(s, self.now + 10)                    # inside the gap: nothing
            scanner._hydrate_pumpportal(s, self.now + SCAN_FETCH["jup_hydrate_gap_s"] + 1)
        self.assertEqual(len(self.queries), 2)
        for q in self.queries:
            self.assertLessEqual(len(q), SCAN_FETCH["jup_batch"])

    def test_mints_priced_recently_are_not_asked_again(self):
        ms = self.mints(10)
        for m in ms[:6]:
            put_row(jup_row(mint=m), at=self.now - 30)
        s = self.session()
        with mock.patch.object(pumpportal, "recent_mints", return_value=ms):
            scanner._hydrate_pumpportal(s, self.now)
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(set(self.queries[0]), set(ms[6:]))

    def test_nothing_to_ask_means_no_call_and_no_gap_used(self):
        ms = self.mints(3)
        for m in ms:
            put_row(jup_row(mint=m), at=self.now - 5)
        s = self.session()
        with mock.patch.object(pumpportal, "recent_mints", return_value=ms):
            scanner._hydrate_pumpportal(s, self.now)
        self.assertEqual(self.queries, [])
        self.assertEqual(scanner._HYDRATE_AT[0], 0.0)

    def test_a_cooling_jupiter_gets_no_hydrate_call(self):
        scanner._HOST_COOL["jupiter"] = {"until": self.now + 100, "strikes": 1}
        s = self.session()
        with mock.patch.object(pumpportal, "recent_mints", return_value=self.mints(5)):
            scanner._hydrate_pumpportal(s, self.now)
        self.assertEqual(self.queries, [])

    def test_the_numbers_behind_the_estimate(self):
        # one Jupiter hydrate per gap at most: <= 60 / gap calls a minute for hydrate
        self.assertLessEqual(60.0 / SCAN_FETCH["jup_hydrate_gap_s"], 0.7)
        self.assertEqual(SCAN_FETCH["jup_batch"], 50)


# ---------------------------------------------------------------- 4. scalp.max_age_minutes
class ScalpMaxAge(unittest.TestCase):
    def test_confirm_bound_is_seven_days_and_value_and_default_are_unchanged(self):
        s = settings.REGISTRY["scalp.max_age_minutes"]
        self.assertEqual(s.hi, 10080)
        self.assertEqual(s.lo, 30)
        self.assertEqual(s.get(), SCALP["max_age_minutes"])
        src = Path(config.__file__).read_text()
        self.assertIn('"max_age_minutes": 60,', src)                              # the config default is as it was

    def test_a_week_can_be_confirmed_and_more_than_a_week_cannot(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"DESK_DATA_DIR": d}), \
                mock.patch.dict(SCALP):
            settings.reset_runtime()
            before = SCALP["max_age_minutes"]
            out = settings.create([{"setting": "scalp.max_age_minutes", "proposed": 10080}], "t", "desk_ai")
            self.assertTrue(out["ok"], out)
            self.assertEqual(SCALP["max_age_minutes"], before)               # proposal alone changes nothing
            settings.confirm(settings.listing()["pending"][0]["id"], "romz")
            self.assertEqual(SCALP["max_age_minutes"], 10080)
            settings.reset_runtime()
            out = settings.create([{"setting": "scalp.max_age_minutes", "proposed": 20000}], "t", "desk_ai")
            if out.get("ok") and settings.listing()["pending"]:
                settings.confirm(settings.listing()["pending"][0]["id"], "romz")
            self.assertLessEqual(SCALP["max_age_minutes"], 10080)
            settings.reset_runtime()

    def test_old_coins_reach_free_kill_and_follow_the_scalp_horizon(self):
        coin = {"age_minutes": 3 * 24 * 60, "liquidity_usd": 40_000, "volume_h1": 20_000, "buys_h1": 90,
                "sells_h1": 70, "mcap_usd": 120_000, "change_h1": 5.0, "volume_m5": 5_000}
        band = {"enabled": True, "max_age_minutes": 10080, "min_age_minutes": 0, "min_mcap_usd": 5_000,
                "max_mcap_usd": 5_000_000, "min_liquidity_usd": 1_000, "max_liquidity_usd": 5_000_000,
                "min_volume_m5_usd": 0}
        with mock.patch.dict(SCALP, band):
            self.assertGreaterEqual(scanner.age_cut_minutes(), 10080)
            self.assertEqual(exits.horizon_for(coin["age_minutes"]), "scalp")
            self.assertEqual(exits.horizon_for(10080 + 1), "swing")
            self.assertTrue(scanner._scalp_window(coin))
            self.assertNotEqual(scanner.free_kill(coin), "age")
            # the absorbers keep the old pool instead of dropping it before free_kill
            with mock.patch.object(scanner, "parse_pool", return_value=coin), \
                    mock.patch.object(scanner, "_keep", side_effect=lambda x: x), \
                    mock.patch.object(scanner, "_put_pool"):
                self.assertEqual(scanner._absorb_gt({"data": [{"id": "x"}], "included": []}, 0.0, age_cut=True), 1)

    def test_with_the_current_value_the_age_cut_is_unchanged(self):
        with mock.patch.dict(SCALP, {"enabled": True}):
            self.assertEqual(scanner.age_cut_minutes(),
                             max(config.SCAN["max_age_hours"] * 60,
                                 config.ESTABLISHED["max_age_hours"] * 60 if config.ESTABLISHED.get("enabled") else 0,
                                 SCALP["max_age_minutes"]))


if __name__ == "__main__":
    unittest.main()
