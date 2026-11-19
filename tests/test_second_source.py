"""Second data source and finalist priority (PAPER gates).

Covers: causes on tx_ratio_unknown / trigger_unknown, Jupiter 5m counts as a GeckoTerminal-free
source, the price tape feeding RSI / breakout / dip when GeckoTerminal candles are missing, the
finalist reserve on the shared GT / Jupiter buckets (discovery never starved), bench time by base
reason, and live mode untouched.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_rulebook as tr  # noqa: E402
from config import BENCH_MINUTES, RULEBOOK  # noqa: E402
from desk import chart, dip_recovery as dr, entry_trigger as et, marketdata, pricetape, pumpportal, scanner  # noqa: E402
from desk.ops import Bench  # noqa: E402
from legacy_book import legacy_book  # noqa: E402
from test_dip_recovery import candles, flat  # noqa: E402
from test_entry_trigger import NOW, falling, rising_spec, tick_prices  # noqa: E402
from test_scanner_sources import FakeResp  # noqa: E402

MINT = "TAPEMINT" + "y" * 30
POOL = "POOLADDR222"


def jup_row(mint=MINT, buys=60, sells=20, v5=3000.0, v1h=12000.0, price=0.001, upd=None, **kw):
    row = {"id": mint, "usdPrice": price,
           "stats5m": {"numBuys": buys, "numSells": sells, "buyVolume": v5 * 0.6, "sellVolume": v5 * 0.4},
           "stats1h": {"buyVolume": v1h * 0.5, "sellVolume": v1h * 0.5}, **kw}
    if upd is not None:
        row["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(upd))
    return row


def put_row(row, at=None):
    scanner._JUP_ROWS[row["id"]] = {"at": time.time() if at is None else at, "row": row}


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


class Causes(Base):
    T = {"mint": MINT, "pool": POOL}

    def test_no_pool_when_the_pool_is_the_mint(self):
        self.assertEqual(scanner.gt_tx_cause({"mint": MINT, "pool": MINT}), "no_pool")
        self.assertEqual(scanner.gt_tx_cause({"mint": MINT}), "no_pool")
        self.assertIsNone(scanner.gt_tx_m5({"mint": MINT, "pool": MINT}))

    def test_cooling_budget_cap_404_and_empty(self):
        now = time.time()
        scanner._HOST_COOL["geckoterminal"] = {"until": now + 60, "strikes": 1}
        self.assertEqual(scanner.gt_tx_cause(self.T, now), "gt_cooling")
        scanner.reset_runtime()
        scanner._BUCKETS["geckoterminal"].tokens = 0.0
        scanner._BUCKETS["geckoterminal"].t = now
        self.assertEqual(scanner.gt_tx_cause(self.T, now), "gt_budget")
        scanner._TX_BUCKET.tokens = 0.0
        scanner._TX_BUCKET.t = now
        self.assertEqual(scanner.gt_tx_cause(self.T, now), "gt_cap")
        scanner.reset_runtime()
        s = mock.Mock()
        s.get.return_value = FakeResp(404)
        self.assertIsNone(scanner.gt_tx_m5(self.T, s, now))
        self.assertEqual(scanner.gt_tx_cause(self.T, now), "gt_404")
        scanner.reset_runtime()
        s.get.return_value = FakeResp(200, {"data": {"attributes": {"transactions": {}}}})
        self.assertIsNone(scanner.gt_tx_m5(self.T, s, now))
        self.assertEqual(scanner.gt_tx_cause(self.T, now), "gt_empty")

    def test_a_404_on_one_pool_does_not_cool_the_others(self):
        now = time.time()
        s = mock.Mock()
        s.get.side_effect = [FakeResp(404), FakeResp(200, {"data": {"attributes": {"transactions": {
            "m5": {"buys": 9, "sells": 3}}}}})]
        self.assertIsNone(scanner.gt_tx_m5({"mint": "A", "pool": "P1"}, s, now))
        scanner._TX_BUCKET.reset(now)
        got = scanner.gt_tx_m5({"mint": "B", "pool": "P2"}, s, now + 1)
        self.assertEqual((got["buys_m5"], got["sells_m5"]), (9, 3))

    def test_candle_cause_names_the_limiter(self):
        now = time.time()
        with mock.patch.object(chart, "_pool_address", return_value=None):
            self.assertEqual(dr.candle_cause(MINT, now), "no_pool")
        with mock.patch.object(chart, "_pool_address", return_value="PC"), \
                mock.patch.object(chart, "cg_available", return_value=False):
            scanner._HOST_COOL["geckoterminal"] = {"until": now + 60, "strikes": 1}
            self.assertEqual(dr.candle_cause(MINT, now), "gt_cooling")
            scanner._HOST_COOL.clear()
            scanner._set_status("gt:ohlcv:PC:minute:1", 404)
            self.assertEqual(dr.candle_cause(MINT, now), "gt_404")
            scanner._set_status("gt:ohlcv:PC:minute:1", None)
            chart._ATTEMPT[chart._ohlcv_key("PC", "1m")] = now
            self.assertEqual(dr.candle_cause(MINT, now), "gt_retry_wait")
            chart._ATTEMPT.clear()
            dr._BUCKET.tokens, dr._BUCKET.t = 0.0, now
            self.assertEqual(dr.candle_cause(MINT, now), "gt_cap")
            dr._BUCKET.reset()
            self.assertEqual(dr.candle_cause(MINT, now), "gt_budget")

    def test_bench_uses_the_base_reason(self):
        b = Bench(":memory:")
        b.sit("M", "tx_ratio_unknown:gt_cooling")
        until = (b.memory.get("M") or (None, 0))[1]
        if until:                                              # in-memory fallback
            self.assertAlmostEqual(until - time.time(), BENCH_MINUTES["tx_ratio_unknown"] * 60, delta=5)
        else:
            row = b.db.execute("SELECT until FROM bench WHERE mint='M'").fetchone()
            self.assertAlmostEqual(row[0] - time.time(), BENCH_MINUTES["tx_ratio_unknown"] * 60, delta=5)


class JupiterSource(Base):
    def test_fresh_row_gives_5m_counts_without_a_request(self):
        put_row(jup_row(upd=time.time() - 20))
        got, cause = scanner.jup_tx_m5(MINT)
        self.assertIsNone(cause)
        self.assertEqual((got["buys_m5"], got["sells_m5"], got["source"]), (60, 20, "jupiter"))
        self.assertEqual(scanner.jup_volume_ratio(MINT)[0], 3.0)           # 3000 vs 12000/12 = 1000

    def test_stale_or_missing_rows_name_the_cause(self):
        self.assertEqual(scanner.jup_tx_m5(MINT)[1], "jup_none")
        put_row(jup_row(), at=time.time() - 1000)
        self.assertEqual(scanner.jup_tx_m5(MINT)[1], "jup_stale")
        put_row(jup_row(upd=time.time() - 3600))                           # Jupiter itself has not refreshed it
        self.assertEqual(scanner.jup_tx_m5(MINT)[1], "jup_stale")
        put_row(jup_row(v1h=0.0))
        self.assertEqual(scanner.jup_volume_ratio(MINT)[1], "jup_no_volume")

    def test_a_stale_cached_body_is_not_stored_as_fresh(self):
        scanner._absorb_jup([jup_row()], time.time(), fresh=False)
        self.assertEqual(scanner.jup_tx_m5(MINT)[1], "jup_stale")
        self.assertIsNone(scanner.jup_tx_m5(MINT)[0])
        put_row(jup_row(buys=5))
        scanner._absorb_jup([jup_row(buys=999)], time.time(), fresh=False)   # must not overwrite a fresh row
        self.assertEqual(scanner.jup_tx_m5(MINT)[0]["buys_m5"], 5)

    def test_fresh_rows_feed_the_price_tape(self):
        scanner._absorb_jup([jup_row(price=0.002)], time.time(), fresh=True)
        self.assertEqual([p for _, p, _ in pricetape.ticks(MINT)], [0.002])
        scanner._absorb_jup([jup_row(mint="STALE" + "z" * 30)], time.time(), fresh=False)
        self.assertEqual(pricetape.ticks("STALE" + "z" * 30), [])

    def test_tx_m5_prefers_jupiter_and_never_touches_geckoterminal(self):
        put_row(jup_row(upd=time.time() - 5))
        with mock.patch.object(scanner, "gt_tx_m5") as gt:
            r = marketdata.tx_m5({"mint": MINT, "pool": POOL}, {"available": False})
        gt.assert_not_called()
        self.assertEqual((r["available"], r["source"], r["buys_m5"]), (True, "jupiter", 60))

    def test_stale_row_gets_one_targeted_refresh(self):
        put_row(jup_row(), at=time.time() - 1000)
        s = mock.Mock()
        s.get.return_value = FakeResp(200, [jup_row(buys=77, sells=11)])
        with mock.patch.object(scanner, "_jup_enabled", return_value=True):
            r = marketdata.tx_m5({"mint": MINT, "pool": POOL}, {"available": False}, s)
        self.assertEqual((r["source"], r["buys_m5"]), ("jupiter", 77))
        self.assertEqual(s.get.call_count, 1)
        self.assertIn("/search", s.get.call_args.args[0])
        self.assertEqual(s.get.call_args.kwargs["params"]["query"], MINT)

    def test_everything_down_reports_both_causes(self):
        now = time.time()
        scanner._HOST_COOL["jupiter"] = {"until": now + 60, "strikes": 1}
        scanner._HOST_COOL["geckoterminal"] = {"until": now + 60, "strikes": 1}
        with mock.patch.object(scanner, "_jup_enabled", return_value=True):
            r = marketdata.tx_m5({"mint": MINT, "pool": POOL}, {"available": False}, mock.Mock(), now)
        self.assertEqual((r["available"], r["cause"]), (False, "jup_cooling+gt_cooling"))
        self.assertEqual(r["chain"][0], "ds:ds_no_pair")

    def test_no_pool_and_no_jupiter_row(self):
        with mock.patch.object(scanner, "_jup_enabled", return_value=True):
            s = mock.Mock()
            s.get.return_value = FakeResp(200, [])
            r = marketdata.tx_m5({"mint": MINT, "pool": MINT}, {"available": False}, s)
        self.assertEqual(r["cause"], "jup_unlisted+no_pool")

    def test_zero_counts_are_not_data(self):
        put_row(jup_row(buys=0, sells=0, upd=time.time() - 5))
        with mock.patch.object(scanner, "gt_tx_m5", return_value={"available": True, "source": "geckoterminal",
                                                                  "buys_m5": 0, "sells_m5": 0}):
            r = marketdata.tx_m5({"mint": MINT, "pool": POOL}, {"available": False})
        self.assertEqual((r["available"], r["cause"]), (False, "no_5m_txns"))

    def test_refresh_has_its_own_cap_and_respects_cooling(self):
        s = mock.Mock()
        s.get.return_value = FakeResp(200, [jup_row()])
        now = time.time()
        with mock.patch.object(scanner, "_jup_enabled", return_value=True):
            self.assertIsNone(scanner.jup_refresh([MINT], s, now))
            self.assertIsNone(scanner.jup_refresh([MINT], s, now))
            self.assertEqual(scanner.jup_refresh([MINT], s, now), "jup_cap")
            scanner._HOST_COOL["jupiter"] = {"until": now + 60, "strikes": 1}
            self.assertEqual(scanner.jup_refresh([MINT], s, now + 1), "jup_cooling")
        self.assertEqual(s.get.call_count, 2)


class FinalistPriority(Base):
    def drain(self, n=1):
        b = scanner._BUCKETS["geckoterminal"]
        for _ in range(n):
            b.allow(1)

    def test_no_reserve_without_waiting_finalists(self):
        now = time.time()
        self.assertTrue(scanner._budget_ok("geckoterminal", now, False))
        self.assertTrue(scanner._budget_ok("geckoterminal", now, False))      # burst of 2 fully usable

    def test_discovery_leaves_a_token_for_a_waiting_finalist(self):
        now = time.time()
        scanner.note_finalist_wait("geckoterminal", now)
        self.assertTrue(scanner._budget_ok("geckoterminal", now, False))      # 2 -> 1 (reserve of 1 stays)
        self.assertFalse(scanner._budget_ok("geckoterminal", now, False))     # discovery yields
        self.assertTrue(scanner._budget_ok("geckoterminal", now, True))       # the finalist takes it
        self.assertEqual(scanner.PRIORITY_STATS["discovery_yielded"], 1)

    def test_discovery_is_never_fully_starved(self):
        now = time.time()
        scanner.note_finalist_wait("geckoterminal", now)
        b = scanner._BUCKETS["geckoterminal"]
        b.tokens, b.t = 1.0, now
        got = []
        for i in range(scanner.MAX_DENY + 2):
            b.tokens, b.t = 1.0, now                                          # a token is there but reserved
            got.append(scanner._budget_ok("geckoterminal", now, False))
        self.assertEqual(got[:scanner.MAX_DENY], [False] * scanner.MAX_DENY)
        self.assertTrue(got[scanner.MAX_DENY])                                # forced through
        self.assertEqual(scanner.PRIORITY_STATS["discovery_forced"], 1)

    def test_reserve_expires(self):
        now = time.time()
        scanner.note_finalist_wait("geckoterminal", now)
        b = scanner._BUCKETS["geckoterminal"]
        b.tokens, b.t = 1.0, now + scanner.FINALIST_WAIT_S + 1
        self.assertTrue(scanner._budget_ok("geckoterminal", now + scanner.FINALIST_WAIT_S + 1, False))

    def test_finalist_lookups_register_and_use_priority(self):
        now = time.time()
        with mock.patch.object(scanner, "_fetch_live", return_value=None) as fl:
            scanner.gt_tx_m5({"mint": "M", "pool": "P"}, mock.Mock(), now)
        self.assertTrue(fl.call_args.kwargs["priority"])
        self.assertGreater(scanner._WAIT["geckoterminal"], now)
        with mock.patch.object(chart, "_pool_address", return_value="PX"), \
                mock.patch.object(chart, "cg_available", return_value=False), \
                mock.patch.object(chart, "_fetch_and_store", return_value=[]) as fs:
            dr.load_candles("M2", None, now + 500)
        self.assertTrue(fs.call_args.kwargs["priority"])

    def test_discovery_fetch_goes_through_the_reserve(self):
        now = time.time()
        scanner.note_finalist_wait("geckoterminal", now)
        b = scanner._BUCKETS["geckoterminal"]
        b.tokens, b.t = 1.0, now
        s = mock.Mock()
        ep = {"id": "gt:new_pools:1", "source": "geckoterminal", "url": "u", "label": "l", "params": {}}
        body, fresh = scanner._fetch_endpoint(s, ep, now)                     # discovery: yields
        self.assertEqual((body, fresh), (None, False))
        s.get.assert_not_called()
        s.get.return_value = FakeResp(200, {"data": []})
        ep2 = {**ep, "id": "gt:ohlcv:PZ:minute:1"}
        body, fresh = scanner._fetch_endpoint(s, ep2, now, priority=True)     # a finalist: goes out
        self.assertTrue(fresh)
        s.get.assert_called_once()


class Tape(Base):
    def test_bounded_and_price_only(self):
        for i in range(pricetape.PER_MINT + 50):
            pricetape.record(MINT, 1.0 + i * 1e-6, 1000.0 + i * 6)
        self.assertEqual(len(pricetape.ticks(MINT)), pricetape.PER_MINT)
        self.assertTrue(all(v == 0.0 for _, _, v in pricetape.ticks(MINT)))
        with mock.patch.object(pricetape, "MAX_MINTS", 5):
            for i in range(9):
                pricetape.record(f"M{i}", 1.0, 5000.0)
        self.assertLessEqual(pricetape.size(), 5)
        self.assertFalse(pricetape.record(MINT, 0, 1.0))
        self.assertFalse(pricetape.record(MINT, "x", 1.0))
        self.assertFalse(pricetape.record(None, 1.0, 1.0))

    def test_repeat_inside_the_gap_is_ignored(self):
        self.assertTrue(pricetape.record(MINT, 1.0, 100.0))
        self.assertFalse(pricetape.record(MINT, 1.1, 101.0))

    def test_candles_1m_merge_quote_ticks_and_tape(self):
        chart.record_tick(MINT, 1.00, 5000.0, ts=NOW - 130)
        pricetape.record(MINT, 1.05, NOW - 70)
        cs = pricetape.candles_1m(MINT)
        self.assertEqual(len(cs), 2)
        self.assertTrue(all(c["v"] == 0.0 for c in cs))            # the 5m volume on quote ticks is not bar volume


def tape_fill(mint, spec, now=NOW):
    """Put a price-only history on the tape: one price per minute ending now (closes of `spec`)."""
    n = len(spec)
    for i, (_o, c, _v) in enumerate(spec):
        pricetape.record(mint, c, now - 60 * (n - 1 - i) - 5)


class TapeTriggers(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(RULEBOOK, {"rsi_trigger": True, "breakout_trigger": True, "entry_trigger_gate": True, "rsi_require_turn": True,
                                       "rsi_timeframe": "1m", "rsi_period": 14, "breakout_lookback": 20,
                                       "breakout_vol_mult": 2.0, "rsi_oversold": 30})
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(dr, "load_candles", return_value=([], None))     # GeckoTerminal: nothing
        q.start()
        self.addCleanup(q.stop)
        r = mock.patch.object(chart, "_pool_address", return_value=None)
        r.start()
        self.addCleanup(r.stop)

    def oversold_spec(self):
        spec = falling(24)
        last = spec[-1][1]
        return spec + [(last, last * 1.004, 100)]

    def test_rsi_comes_from_the_tape_when_geckoterminal_is_empty(self):
        tape_fill(MINT, self.oversold_spec())
        r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertEqual((r["fired"], r["rsi_tf"], r["rsi_source"]), (et.RSI_OVERSOLD, "1m", "price_tape"))
        self.assertLess(r["rsi_value"], 30)

    def test_thin_tape_and_no_geckoterminal_is_unknown_with_causes(self):
        tape_fill(MINT, rising_spec(4))
        r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertIsNone(r["fired"])
        self.assertEqual(r["reason"], et.SKIP_UNKNOWN)
        self.assertEqual(et.gate(r), f"trigger_unknown:{r['cause']}")
        self.assertIn("no_pool", r["cause"])
        self.assertIn("too_few_bars", r["cause"])

    def test_cause_names_a_cooling_geckoterminal(self):
        with mock.patch.object(chart, "_pool_address", return_value="PCOOL"), \
                mock.patch.object(chart, "cg_available", return_value=False):
            scanner._HOST_COOL["geckoterminal"] = {"until": NOW + 60, "strikes": 1}
            with mock.patch.object(scanner.time, "time", return_value=NOW):
                r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertTrue(et.gate(r).startswith("trigger_unknown:gt_cooling"), et.gate(r))

    def test_stale_tape_is_unknown(self):
        tape_fill(MINT, self.oversold_spec(), now=NOW - 900)
        r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertEqual(r["reason"], et.SKIP_UNKNOWN)
        self.assertIn("stale_ticks", r["cause"])

    def test_breakout_from_tape_resistance_and_jupiter_volume(self):
        tape_fill(MINT, flat(20, 1.0) + [(1.0, 1.05, 0)])
        put_row(jup_row(v5=3000.0, v1h=12000.0, upd=NOW - 5), at=NOW)
        with mock.patch.object(scanner.time, "time", return_value=NOW):
            r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertEqual(r["fired"], et.BREAKOUT, r["breakout"])
        self.assertEqual((r["vol_mult"], r["vol_source"]), (3.0, "price_tape+jupiter"))
        self.assertTrue(r["breakout"]["above"])

    def test_breakout_needs_jupiter_volume_not_just_price(self):
        tape_fill(MINT, flat(20, 1.0) + [(1.0, 1.05, 0)])
        with mock.patch.object(scanner.time, "time", return_value=NOW):
            r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertNotEqual(r["fired"], et.BREAKOUT)
        self.assertEqual(r["breakout"]["state"], et.UNKNOWN)
        self.assertIn("jup_none", r["breakout"]["cause"])
        put_row(jup_row(v5=1500.0, v1h=12000.0, upd=NOW - 5), at=NOW)        # 1.5x: below the 2x bar
        with mock.patch.object(scanner.time, "time", return_value=NOW):
            r = et.evaluate(MINT, now=NOW, fetch=False)
        self.assertEqual((r["breakout"]["state"], r["breakout"]["detail"]), (et.NO, "weak_volume"))

    def test_geckoterminal_still_wins_when_it_has_candles(self):
        cs = candles(self.oversold_spec(), NOW)
        with mock.patch.object(dr, "load_candles", return_value=(cs, NOW)):
            tape_fill(MINT, rising_spec(25))
            r = et.evaluate(MINT, now=NOW)
        self.assertEqual((r["fired"], r["rsi_source"]), (et.RSI_OVERSOLD, "geckoterminal"))


class DipTape(Base):
    def test_price_only_candles_never_call_a_chart_dead(self):
        spec = flat(15) + [(1.0 - 0.0625 * i, 1.0 - 0.0625 * (i + 1), 0) for i in range(4)] + flat(8, 0.75, 0)
        cs = candles(spec, NOW)
        known = dr.classify(cs, now=NOW, fetched_at=NOW)
        blind = dr.classify(cs, now=NOW, fetched_at=NOW, volume_known=False)
        self.assertNotEqual(blind["class"], dr.DEAD)
        self.assertEqual((blind["class"], blind["detail"]), (dr.UNKNOWN, "dip_unresolved_no_volume"))
        self.assertIn(known["class"], (dr.DEAD, dr.UNKNOWN, dr.NO_DIP))

    def test_read_falls_back_to_the_tape_and_a_jupiter_buy_ratio(self):
        spec = flat(12) + [(1.0 - 0.07 * i, 1.0 - 0.07 * (i + 1), 0) for i in range(4)] + \
            [(0.72, 0.80, 0), (0.80, 0.88, 0), (0.88, 0.90, 0), (0.90, 0.91, 0)]
        tape_fill(MINT, spec)
        put_row(jup_row(buys=90, sells=30, upd=NOW - 5), at=NOW)
        t = {"mint": MINT, "pool": MINT}
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch.object(scanner.time, "time", return_value=NOW):
            r = dr.read(t, {"available": False}, None, now=NOW)
        self.assertEqual(r.get("source"), "price_tape")
        self.assertNotEqual(r["class"], dr.DEAD)

    def test_cached_tx_uses_a_fresh_jupiter_row(self):
        put_row(jup_row(buys=12, sells=4, upd=time.time() - 3))
        self.assertEqual(dr.cached_tx_m5({"mint": MINT, "pool": "nope"}), (12, 4))
        scanner._JUP_ROWS.clear()
        self.assertEqual(dr.cached_tx_m5({"mint": MINT, "pool": "nope"}), (None, None))


@legacy_book
class AgentWiring(unittest.TestCase):
    run_agent = tr.AgentWiring.run_agent
    skips = tr.AgentWiring.skips

    def setUp(self):
        tr.on()
        scanner.reset_runtime()
        self.addCleanup(scanner.reset_runtime)

    def test_jupiter_counts_pass_the_tx_gate_when_dexscreener_is_missing(self):
        put_row(jup_row(mint=tr.TOK["mint"], buys=60, sells=20, upd=time.time() - 5))
        with mock.patch.object(scanner, "gt_tx_m5") as gt:
            a = self.run_agent(ds={"available": False})
        gt.assert_not_called()
        self.assertEqual(self.skips(a), [])
        self.assertEqual(a.stats["entries"], 1, [e["message"] for e in list(a.events)[:8]])
        self.assertEqual(a.stats["src_tx_jupiter"], 1)

    def test_jupiter_counts_that_fail_the_ratio_skip_tx_ratio_low(self):
        put_row(jup_row(mint=tr.TOK["mint"], buys=10, sells=30, upd=time.time() - 5))
        a = self.run_agent(ds={"available": False})
        self.assertEqual(self.skips(a), ["tx_ratio_low"])

    def test_missing_everywhere_names_the_cause_and_keeps_the_prefix(self):
        a = self.run_agent(ds={"available": False})
        reasons = self.skips(a)
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("tx_ratio_unknown:"), reasons)
        self.assertEqual(a.stats["src_tx_missing"], 1)
        self.assertTrue(any(e["message"] == f"gate: $RULE skip {reasons[0]}" for e in a.events))
        self.assertIn(reasons[0], a.funnel)                                     # counters see the full reason

    def test_second_source_is_paper_only(self):
        src = Path(sys.modules["desk.solana_agent"].__file__).read_text()
        i = src.index("marketdata.tx_m5(")
        self.assertIn('self.mode != "live"', src[max(0, i - 900):i])


class NoPaidStream(unittest.TestCase):
    def test_pumpportal_only_ever_subscribes_to_new_tokens(self):
        # subscribeTokenTrade needs a PumpPortal API key and a funded wallet: the desk never uses it
        self.assertEqual(pumpportal.SUB, {"method": "subscribeNewToken"})
        for mod in (pumpportal, marketdata, pricetape):
            self.assertNotIn('"subscribeTokenTrade"', Path(mod.__file__).read_text().replace(
                "subscribeTokenTrade needs", ""))


if __name__ == "__main__":
    unittest.main()
