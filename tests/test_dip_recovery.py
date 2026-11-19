"""Dip-recovery chart reader (desk/dip_recovery.py), PAPER only.

Synthetic 1m candles for recovered_dip, dead_after_dip, no_dip and thin data; the gate
is OFF by default, blocks only dead_after_dip when ON, never on unknown, never in live;
and the new settings change only through proposal then Confirm.
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
from desk import analyst, chart, dip_recovery as dr, scanner, settings  # noqa: E402
from desk.solana_agent import SolanaAgent  # noqa: E402
from legacy_book import legacy_book  # noqa: E402

NOW = 1_800_000_000.0 + 20          # 20 s into a minute
DIP_KEYS = ("rulebook.dip_recovery_gate", "rulebook.dip_min_drop_pct", "rulebook.dip_recover_window_min",
            "rulebook.dip_min_recover_pct", "rulebook.dip_min_buy_ratio")


def candles(spec, now=NOW):
    """spec = [(open, close, volume), ...], oldest first; the last bar is the current minute."""
    n = len(spec)
    start = (int(now) // 60) * 60 - (n - 1) * 60
    return [{"t": start + 60 * i, "o": o, "h": max(o, c) * 1.002, "l": min(o, c) * 0.998, "c": c, "v": v}
            for i, (o, c, v) in enumerate(spec)]


def flat(n, px=1.0, v=100):
    return [(px, px, v)] * n


def drop_to(low, n=4, v=300):
    """n red bars falling from 1.0 to `low`."""
    step = (1.0 - low) / n
    return [(1.0 - step * i, 1.0 - step * (i + 1), v) for i in range(n)]


def recovered_spec():
    # 15 calm bars, a 25% selloff, then a green bounce on bigger volume that holds
    return (flat(15) + drop_to(0.75) +
            [(0.75, 0.82, 450), (0.82, 0.90, 500), (0.90, 0.93, 400), (0.93, 0.92, 120), (0.92, 0.94, 300)])


def dead_spec():
    # same selloff, then nothing: a flat, near-silent chart
    return flat(15) + drop_to(0.75) + [(0.75, 0.76, 20), (0.76, 0.75, 15), (0.75, 0.76, 10),
                                       (0.76, 0.76, 8), (0.76, 0.75, 5), (0.75, 0.75, 4), (0.75, 0.75, 3)]


class Classifier(unittest.TestCase):
    def test_recovered_dip(self):
        r = dr.classify(candles(recovered_spec()), now=NOW)
        self.assertEqual(r["class"], dr.RECOVERED, r)
        self.assertGreaterEqual(r["drop_pct"], 0.20)
        self.assertGreaterEqual(r["retrace_best"], 0.5)
        self.assertEqual(r["buy_basis"], "candle_volume")
        self.assertGreaterEqual(r["buy_ratio"], 1.3)

    def test_recovered_uses_5m_txn_counts_when_the_low_is_recent(self):
        c = candles(recovered_spec())
        self.assertEqual(dr.classify(c, now=NOW, buys_m5=60, sells_m5=30)["buy_basis"], "txns_m5")
        self.assertEqual(dr.classify(c, now=NOW, buys_m5=60, sells_m5=30)["class"], dr.RECOVERED)
        weak = dr.classify(c, now=NOW, buys_m5=20, sells_m5=30)      # bounced, but sellers lead
        self.assertEqual((weak["class"], weak["detail"]), (dr.UNKNOWN, "recovered_weak_buys"))

    def test_dead_after_dip(self):
        r = dr.classify(candles(dead_spec()), now=NOW)
        self.assertEqual((r["class"], r["detail"]), (dr.DEAD, "no_recovery_volume_fading"), r)
        self.assertLess(r["retrace_best"], 0.5)
        self.assertLessEqual(r["fade_ratio"], 0.5)

    def test_silent_minutes_read_as_dead_not_missing(self):
        """GeckoTerminal omits minutes with no trades; they must count as zero volume."""
        spec = candles(flat(15) + drop_to(0.75) + [(0.75, 0.75, 5)])
        r = dr.classify(spec, now=NOW + 8 * 60, fetched_at=NOW + 8 * 60)
        self.assertEqual(r["class"], dr.DEAD, r)

    def test_no_dip(self):
        wiggle = [(1.0 + 0.01 * (i % 3), 1.0 + 0.01 * ((i + 1) % 3), 100) for i in range(25)]
        r = dr.classify(candles(wiggle), now=NOW)
        self.assertEqual(r["class"], dr.NO_DIP, r)
        shallow = flat(15) + drop_to(0.88) + flat(8, 0.88)            # 12% is not a dip
        self.assertEqual(dr.classify(candles(shallow), now=NOW)["class"], dr.NO_DIP)

    def test_insufficient_or_bad_data_is_unknown(self):
        for c in ([], None, candles(flat(3)), [{"t": "x"}, {"nope": 1}]):
            self.assertEqual(dr.classify(c, now=NOW)["class"], dr.UNKNOWN)
        stale = candles(dead_spec())
        r = dr.classify(stale, now=NOW + 3600, fetched_at=NOW - 1000)
        self.assertEqual((r["class"], r["detail"]), (dr.UNKNOWN, "stale_candles"))

    def test_unresolved_dips_are_unknown(self):
        pending = candles(flat(15) + drop_to(0.75) + [(0.75, 0.76, 300), (0.76, 0.75, 300)])
        r = dr.classify(pending, now=NOW)                              # still inside the 5 min window
        self.assertEqual((r["class"], r["detail"]), (dr.UNKNOWN, "dip_pending"))
        busy = candles(flat(15) + drop_to(0.75) + [(0.75, 0.74, 300)] * 8)   # no bounce, still heavy selling
        r = dr.classify(busy, now=NOW)
        self.assertEqual((r["class"], r["detail"]), (dr.UNKNOWN, "dip_unresolved"))

    def test_bounce_that_is_given_back_is_not_recovered(self):
        spec = (flat(15) + drop_to(0.75) + [(0.75, 0.90, 400)] + [(0.90, 0.80, 50), (0.80, 0.76, 20)] +
                [(0.76, 0.75, 6)] * 6)
        r = dr.classify(candles(spec), now=NOW)
        self.assertEqual(r["class"], dr.DEAD, r)

    def test_tunables_are_read_live(self):
        c = candles(recovered_spec())
        with mock.patch.dict(RULEBOOK, {"dip_min_drop_pct": 0.40}):
            self.assertEqual(dr.classify(c, now=NOW)["class"], dr.NO_DIP)
        with mock.patch.dict(RULEBOOK, {"dip_min_buy_ratio": 50.0}):
            self.assertEqual(dr.classify(c, now=NOW)["class"], dr.UNKNOWN)
        with mock.patch.dict(RULEBOOK, {"dip_min_recover_pct": 1.0}):
            self.assertNotEqual(dr.classify(c, now=NOW)["class"], dr.RECOVERED)


class Gate(unittest.TestCase):
    def test_off_by_default(self):
        self.assertIn('"dip_recovery_gate": False', Path(config.__file__).read_text())
        saved = dict(RULEBOOK)
        self.assertIs(saved.get("dip_recovery_gate"), False)
        self.assertIsNone(dr.gate({"class": dr.DEAD}))

    def test_on_blocks_dead_only(self):
        with mock.patch.dict(RULEBOOK, {"dip_recovery_gate": True}):
            self.assertEqual(dr.gate({"class": dr.DEAD}), "dip_dead")
            for cls in (dr.RECOVERED, dr.NO_DIP, dr.UNKNOWN):
                self.assertIsNone(dr.gate({"class": cls}), cls)
            self.assertIsNone(dr.gate(None))
            self.assertIsNone(dr.gate({}))

    def test_bench_time_known(self):
        self.assertIn("dip_dead", config.BENCH_MINUTES)


class Settings(unittest.TestCase):
    def test_registered_with_bounds_and_defaults(self):
        for k in DIP_KEYS:
            s = settings.REGISTRY[k]
            self.assertFalse(settings._EXCLUDED_RE.search(k), k)
            if s.kind != "bool":
                self.assertIsNotNone(s.lo)
                self.assertIsNotNone(s.hi)
        self.assertEqual(settings.REGISTRY["rulebook.dip_recovery_gate"].get(), False)
        self.assertEqual(settings.REGISTRY["rulebook.dip_min_drop_pct"].get(), 0.20)
        self.assertEqual(settings.REGISTRY["rulebook.dip_recover_window_min"].get(), 5)
        self.assertEqual(settings.REGISTRY["rulebook.dip_min_recover_pct"].get(), 0.50)
        self.assertEqual(settings.REGISTRY["rulebook.dip_min_buy_ratio"].get(), 1.3)

    def test_proposal_then_confirm_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DESK_DATA_DIR": tmp}), \
                mock.patch.dict(RULEBOOK):
            settings.reset_runtime()
            out = settings.create([{"setting": "rulebook.dip_recovery_gate", "proposed": True},
                                   {"setting": "rulebook.dip_min_drop_pct", "proposed": "25%"},
                                   {"setting": "rulebook.dip_recover_window_min", "proposed": 99}],
                                  "t", "desk_ai")
            self.assertTrue(out["ok"], out)
            self.assertFalse(RULEBOOK["dip_recovery_gate"])               # a proposal changes nothing
            self.assertEqual(RULEBOOK["dip_min_drop_pct"], 0.20)
            pid = settings.listing()["pending"][0]["id"]
            settings.confirm(pid, "romz")
            self.assertTrue(RULEBOOK["dip_recovery_gate"])
            self.assertEqual(RULEBOOK["dip_min_drop_pct"], 0.25)
            self.assertEqual(RULEBOOK["dip_recover_window_min"], 15)      # clamped to the bound
            settings.reset_runtime()


TOK = {"mint": "DIPMINT1" + "x" * 30, "symbol": "DIPC", "name": "DIPC", "pool": "P",
       "dex": "pumpswap", "price_usd": 0.001, "mcap_usd": 300_000, "liquidity_usd": 40_000,
       "volume_m5": 5000, "volume_h1": 90_000, "volume_h6": 150_000, "volume_h24": 300_000,
       "buys_h1": 500, "sells_h1": 300, "buyers_h1": 220, "change_m5": 3, "change_h1": 60,
       "change_h6": 80, "age_minutes": 45, "source": "geckoterminal"}
CLEAN = {"chips": [], "block": [], "halve": [], "unknown": [], "parts": {}}
DS = {"available": True, "has_x": True, "has_telegram": True, "websites": ["https://x.y"],
      "socials": {}, "buys_m5": 40, "sells_m5": 20, "change_m5": 3}
DEV = {"hard_no": [], "dev_score": 90, "dev_hold_pct": 0.01, "creator_prev_rugs": 0,
       "sniper_pct": 0.05, "reasons": [], "lp_locked_pct": 1.0, "creator_hold_pct": 0.02,
       "concentration": {"top_10_pct": 0.2}}
JUP = {"available": True, "route": True, "impact": 0.004, "impact_pct": 0.4,
       "flags": [], "block": [], "halve": []}


@legacy_book
class AgentWiring(unittest.TestCase):
    def run_agent(self, spec, gate=False, mode=None):
        d = Path(tempfile.mkdtemp())
        a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        if mode:
            a.mode = mode
        now = time.time()
        facts = {"mint_authority": False, "freeze_authority": False, "top_wallet_pct": 0.03,
                 "top_10_pct": 0.2, "holders": 400, "supply": 1e9, "decimals": 6}
        quote = {"price_usd": 0.00105, "liquidity_usd": 39_000, "volume_h6": 1, "volume_h24": 1}
        cs = [] if spec is None else candles(spec, now)
        load = mock.Mock(return_value=(cs, now if cs else None))
        patches = [mock.patch.object(scanner, "new_tokens", return_value=[dict(TOK)]),
                   mock.patch.object(scanner, "socials", return_value={"x": "https://x.com/a", "telegram": None, "website": None}),
                   mock.patch.object(scanner, "quote", return_value=quote),
                   mock.patch("desk.chain.facts", return_value=facts),
                   mock.patch("desk.dexscreener.facts", return_value=dict(DS)),
                   mock.patch("desk.devcheck.investigate", return_value=dict(DEV)),
                   mock.patch("desk.solana_agent.alert"),
                   mock.patch("desk.solana_agent.signals.security", return_value=CLEAN),
                   mock.patch("desk.solana_agent.signals.entry_impact", return_value=JUP),
                   mock.patch.object(dr, "load_candles", load),
                   mock.patch.dict(RULEBOOK, {"dip_recovery_gate": gate})]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        a.run_once()
        return a, load

    def skips(self, a):
        return [e["reason"] for e in a.events if e.get("stage") == "rulebook"]

    def test_gate_off_dead_coin_still_enters_and_is_tagged(self):
        a, _ = self.run_agent(dead_spec(), gate=False)
        self.assertEqual(self.skips(a), [])
        self.assertEqual(a.stats["entries"], 1, [e["message"] for e in list(a.events)[:8]])
        dec = a.decisions[0]
        self.assertEqual(dec["dip_recovery"]["class"], dr.DEAD)
        self.assertEqual(dec["sizes"]["dip_recovery"]["class"], dr.DEAD)
        self.assertTrue(any(f["t"] == "dip_dead" for f in dec["flags"]))
        self.assertTrue(any(e.get("stage") == "dip" and "dead_after_dip" in e["message"] for e in a.events))

    def test_gate_on_skips_dead_coin(self):
        a, _ = self.run_agent(dead_spec(), gate=True)
        self.assertEqual(self.skips(a), ["dip_dead"])
        self.assertEqual(a.stats["entries"], 0)
        self.assertTrue(any(e["message"] == "gate: $DIPC skip dip_dead" for e in a.events))

    def test_gate_on_lets_recovered_unknown_and_no_dip_through(self):
        calm = flat(25)
        for name, spec in (("recovered", recovered_spec()), ("unknown", None), ("no_dip", calm)):
            with self.subTest(name):
                a, _ = self.run_agent(spec, gate=True)
                self.assertEqual(self.skips(a), [])
                self.assertEqual(a.stats["entries"], 1)

    def test_recovered_is_tagged_and_does_not_change_the_ticket(self):
        a, _ = self.run_agent(recovered_spec(), gate=True)
        dec = a.decisions[0]
        self.assertEqual(dec["dip_recovery"]["class"], dr.RECOVERED)
        self.assertTrue(any(f["t"] == "dip_recovered" for f in dec["flags"]))
        b, _ = self.run_agent(flat(25), gate=True)
        self.assertEqual(dec["ticket_usd"], b.decisions[0]["ticket_usd"])

    def test_live_mode_is_untouched(self):
        a, load = self.run_agent(dead_spec(), gate=True, mode="live")
        load.assert_not_called()
        self.assertNotIn("dip_dead", self.skips(a))
        self.assertTrue(all(e.get("stage") != "dip" for e in a.events))

    def test_every_call_site_is_behind_a_paper_check(self):
        src = Path(sys.modules["desk.solana_agent"].__file__).read_text()
        i = src.index("dip_recovery.read(")
        self.assertIn('self.mode != "live"', src[max(0, i - 300):i])


class AnalystFact(unittest.TestCase):
    def test_dip_is_a_fact_only_when_recovered_or_dead(self):
        ctx = {"scorecard": {}, "dev": {}, "ds": {}}
        base = analyst.token_state(TOK, {}, {}, ctx)
        self.assertNotIn("dip_recovery", base)
        for info in (None, {"class": dr.UNKNOWN}, {"class": dr.NO_DIP}):
            self.assertNotIn("dip_recovery", analyst.token_state(TOK, {}, {}, {**ctx, "dip": info}))
        out = analyst.token_state(TOK, {}, {}, {**ctx, "dip": {"class": dr.RECOVERED, "drop_pct": 0.25,
                                                               "retrace_best": 0.7, "buy_ratio": 2.0}})
        self.assertIn('"dip_recovery"', out)
        self.assertIn("recovered_dip", out)


class DataAccess(unittest.TestCase):
    def setUp(self):
        chart._OHLCV.clear()
        chart._ATTEMPT.clear()
        dr._BUCKET.reset()
        self.addCleanup(chart._OHLCV.clear)
        self.addCleanup(chart._ATTEMPT.clear)

    def test_fresh_cache_costs_no_request(self):
        pool = "POOLX"
        cs = candles(dead_spec())
        chart._OHLCV[chart._ohlcv_key(pool, "1m")] = {"at": NOW - 10, "candles": cs}
        with mock.patch.object(chart, "_pool_address", return_value=pool), \
                mock.patch.object(chart, "_fetch_and_store") as fetch:
            got, at = dr.load_candles("M", None, NOW)
        fetch.assert_not_called()
        self.assertEqual((got, at), (cs, NOW - 10))

    def test_uncached_reads_respect_pool_retry_gap_cooling_and_the_bucket(self):
        with mock.patch.object(chart, "_pool_address", side_effect=lambda m: "POOL" + m), \
                mock.patch.object(chart, "cg_available", return_value=False), \
                mock.patch.object(scanner, "host_cooling", return_value=False), \
                mock.patch.object(chart, "_fetch_and_store", return_value=candles(flat(10))) as fetch:
            dr.load_candles("A", None, NOW)
            dr.load_candles("B", None, NOW)
            dr.load_candles("C", None, NOW)            # bucket burst of 2 is spent
            self.assertEqual(fetch.call_count, 2)
            chart._ATTEMPT[chart._ohlcv_key("POOLA", "1m")] = NOW
            dr.load_candles("A", None, NOW + 30)       # inside the pool retry gap
            self.assertEqual(fetch.call_count, 2)
        with mock.patch.object(chart, "_pool_address", return_value="POOLZ"), \
                mock.patch.object(chart, "cg_available", return_value=False), \
                mock.patch.object(scanner, "host_cooling", return_value=True), \
                mock.patch.object(chart, "_fetch_and_store") as fetch:
            self.assertEqual(dr.load_candles("Z", None, NOW + 600), ([], None))
            fetch.assert_not_called()

    def test_read_never_raises(self):
        with mock.patch.object(dr, "load_candles", side_effect=RuntimeError("boom")):
            self.assertEqual(dr.read(TOK, DS, None)["class"], dr.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
