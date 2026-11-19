"""rulebook.momentum_exit_timeframe ("1m" default / "5m"): the momentum exit's entry-candle-low check
can read the last CLOSED 5m bar. "1m" is exactly today's behaviour."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from config import RULEBOOK  # noqa: E402
from desk import chart, confirm, dip_recovery as dr, marketdata, settings  # noqa: E402
from desk.broker import PaperBroker  # noqa: E402
from desk.solana_agent import SolanaAgent  # noqa: E402
from legacy_book import legacy_book  # noqa: E402

NOW = 1_800_000_000.0                      # a multiple of 300: bar buckets line up
OPENED = NOW - 400


def bars(closes, tf=300, end=NOW - 10, lows=None):
    """Bars ending at `end` (the last bar is closed when end <= NOW)."""
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({"t": end - (n - i) * tf, "o": o, "h": max(o, c), "l": lows[i] if lows else min(o, c),
                    "c": c, "v": 1})
    return out


def pos(**kw):
    p = {"entry": 1.24, "opened": OPENED, "stage": 0, "entry_candle_low": 1.22, "entry_candle_low_5m": 1.20}
    p.update(kw)
    return p


UP = [1.0 + 0.01 * i for i in range(25)]


class Regression1m(unittest.TestCase):
    def test_default_is_one_minute_and_nothing_else_changed(self):
        self.assertEqual(RULEBOOK["momentum_exit_timeframe"], "1m")
        c = bars(UP[:-1] + [1.21], tf=60, end=NOW - 1)
        for kw in ({}, {"timeframe": "1m"}):
            why = confirm.momentum_exit(pos(opened=NOW - 300), c, NOW, **kw)
            self.assertEqual(why, "momentum: 1m closed under the entry candle low")
        self.assertIn("9 EMA", confirm.momentum_exit(
            pos(opened=NOW - 300, entry_candle_low=1.0),
            bars(UP[:-3] + [1.22, 1.22, 1.18], tf=60, end=NOW - 1), NOW) or "")

    def test_a_5m_series_is_ignored_in_1m_mode(self):
        c1 = bars(UP, tf=60, end=NOW - 1)
        under = bars([1.3, 1.3, 1.0], end=NOW - 10)                      # a closed 5m bar far under the 5m low
        self.assertIsNone(confirm.momentum_exit(pos(opened=NOW - 300, entry=1.2, entry_candle_low=1.19),
                                                c1, NOW, under, "1m"))


class FiveMinute(unittest.TestCase):
    def test_a_closed_5m_bar_under_the_low_exits(self):
        c5 = bars([1.3, 1.25, 1.19])                                      # last bar closed at NOW-10, 1.19 < 1.20
        why = confirm.momentum_exit(pos(), [], NOW, c5, "5m")
        self.assertEqual(why, "momentum: 5m closed under the entry candle low")
        self.assertEqual(confirm.momentum_exit_5m(pos(), c5, NOW), why)

    def test_a_closed_bar_at_or_over_the_low_holds(self):
        self.assertIsNone(confirm.momentum_exit_5m(pos(), bars([1.3, 1.25, 1.20]), NOW))
        self.assertIsNone(confirm.momentum_exit_5m(pos(), bars([1.3, 1.25, 1.26]), NOW))

    def test_the_forming_bar_is_ignored(self):
        c5 = bars([1.3, 1.25, 1.10], end=NOW + 200)                       # newest bar still forming, close 1.10
        self.assertEqual([b[0] for b in confirm._bars(c5, NOW, 300, closed_only=True)][-1], c5[-2]["t"])
        self.assertIsNone(confirm.momentum_exit_5m(pos(), c5, NOW))       # the closed bar before it is 1.25
        only_forming = [b for b in c5][-1:]
        self.assertIsNone(confirm.momentum_exit_5m(pos(), only_forming, NOW))

    def test_only_the_last_closed_bar_counts(self):
        c5 = bars([1.0, 1.3, 1.26])                                       # an earlier bar under the low, last one fine
        self.assertIsNone(confirm.momentum_exit_5m(pos(), c5, NOW))

    def test_the_1m_low_no_longer_triggers_in_5m_mode(self):
        c1 = bars(UP[:-1] + [1.21], tf=60, end=NOW - 1)                   # would exit in 1m mode (low 1.22)
        c5 = bars([1.3, 1.25, 1.26])
        self.assertIsNone(confirm.momentum_exit(pos(opened=NOW - 300), c1, NOW, c5, "5m"))
        self.assertIn("1m closed under", confirm.momentum_exit(pos(opened=NOW - 300), c1, NOW) or "")

    def test_the_ema_break_clause_still_runs(self):
        c1 = bars(UP[:-3] + [1.22, 1.22, 1.18], tf=60, end=NOW - 1)
        why = confirm.momentum_exit(pos(opened=NOW - 300, entry_candle_low=1.0), c1, NOW, bars([1.3, 1.3, 1.3]), "5m")
        self.assertIn("9 EMA", why or "")

    def test_the_anchor_is_computed_from_the_entry_bar_when_missing(self):
        # entry at OPENED sits in the bar that starts at floor(OPENED/300)*300; its low is 1.20
        t0 = int(OPENED // 300) * 300
        c5 = [{"t": t0 - 300, "o": 1.3, "h": 1.3, "l": 1.0, "c": 1.3, "v": 1},
              {"t": t0, "o": 1.25, "h": 1.26, "l": 1.20, "c": 1.24, "v": 1},
              {"t": t0 + 300, "o": 1.24, "h": 1.24, "l": 1.15, "c": 1.15, "v": 1}]
        p = pos(entry_candle_low_5m=None)
        self.assertEqual(confirm.momentum_exit_5m(p, c5, NOW), "momentum: 5m closed under the entry candle low")
        c5[-1]["c"] = 1.21
        self.assertIsNone(confirm.momentum_exit_5m(p, c5, NOW))
        # an anchor above the price paid is capped at the fill
        c5[1]["l"] = 1.30
        c5[-1]["c"] = 1.25
        self.assertIsNone(confirm.momentum_exit_5m(pos(entry_candle_low_5m=None, entry=1.24), c5, NOW))
        c5[-1]["c"] = 1.2
        self.assertEqual(confirm.momentum_exit_5m(pos(entry_candle_low_5m=None, entry=1.24), c5, NOW),
                         "momentum: 5m closed under the entry candle low")

    def test_missing_data_never_exits(self):
        self.assertIsNone(confirm.momentum_exit_5m(pos(), [], NOW))
        self.assertIsNone(confirm.momentum_exit_5m(pos(), None, NOW))
        self.assertIsNone(confirm.momentum_exit(pos(), [], NOW, None, "5m"))
        # no anchor and no bar that contains the entry: skipped
        far = bars([1.0, 1.0, 1.0], end=NOW - 10)
        self.assertIsNone(confirm.momentum_exit_5m(pos(entry_candle_low_5m=None, opened=NOW - 100), far, NOW))
        # a stale closed bar (older than 10 minutes) is not a read
        self.assertIsNone(confirm.momentum_exit_5m(pos(), bars([1.3, 1.0], end=NOW - 1500), NOW))
        # nothing has closed since the entry
        self.assertIsNone(confirm.momentum_exit_5m(pos(opened=NOW - 5), bars([1.3, 1.0], end=NOW - 10), NOW))

    def test_hold_time_and_tp1_still_gate_it(self):
        c5 = bars([1.3, 1.25, 1.19])
        self.assertIsNone(confirm.momentum_exit_5m(pos(opened=NOW - 30), c5, NOW))
        self.assertIsNone(confirm.momentum_exit_5m(pos(stage=1), c5, NOW))
        with mock.patch.dict(config.CONFIRM, {"momentum_exit": False}):
            self.assertIsNone(confirm.momentum_exit_5m(pos(), c5, NOW))

    def test_entry_candle_low_5m_reads_the_newest_fresh_bar(self):
        c5 = bars([1.3, 1.25, 1.26], end=NOW + 100, lows=[1.0, 1.1, 1.15])
        self.assertEqual(confirm.entry_candle_low_5m(c5, NOW), 1.15)
        self.assertIsNone(confirm.entry_candle_low_5m(bars([1.3], end=NOW - 3000), NOW))
        self.assertIsNone(confirm.entry_candle_low_5m([], NOW))

    def test_the_broker_never_anchors_above_the_fill(self):
        b = PaperBroker(Path(tempfile.mkdtemp()) / "l.json")
        b.buy("M" * 32, "SYM", 5.0, 1.0, horizon="scalp", meta={"entry_candle_low": 5.0, "entry_candle_low_5m": 7.0})
        p = b.s["positions"]["M" * 32]
        self.assertLessEqual(p["entry_candle_low_5m"], p["entry"])
        self.assertLessEqual(p["entry_candle_low"], p["entry"])


class Setting(unittest.TestCase):
    def test_schema(self):
        s = settings.REGISTRY["rulebook.momentum_exit_timeframe"]
        self.assertEqual((s.kind, s.choices), ("choice", ("1m", "5m")))
        self.assertEqual(s.get(), "1m")
        self.assertFalse(settings._EXCLUDED_RE.search(s.key))
        self.assertIn('"momentum_exit_timeframe": "1m"', Path(config.__file__).read_text())
        self.assertEqual(s.coerce("5m")[0], "5m")
        with self.assertRaises(ValueError):
            s.coerce("15m")

    def test_proposal_changes_nothing_and_confirm_applies(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"DESK_DATA_DIR": d}), \
                mock.patch.dict(RULEBOOK):
            settings.reset_runtime()
            out = settings.create([{"setting": "rulebook.momentum_exit_timeframe", "proposed": "5m"}], "t", "desk_ai")
            self.assertTrue(out["ok"], out)
            self.assertEqual(RULEBOOK["momentum_exit_timeframe"], "1m")
            settings.confirm(settings.listing()["pending"][0]["id"], "romz")
            self.assertEqual(RULEBOOK["momentum_exit_timeframe"], "5m")
            settings.reset_runtime()
            bad = settings.create([{"setting": "rulebook.momentum_exit_timeframe", "proposed": "1h"}], "t", "desk_ai")
            self.assertFalse(bad.get("ok") and settings.listing()["pending"], bad)


@legacy_book
class AgentExit(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        self.mint = "MOMMINT1" + "x" * 30
        self.addCleanup(chart.reset)
        marketdata.reset()

    def run_exit(self, tf, c1, p=None, fb=None):
        p = p or pos(horizon="scalp", opened=OPENED)
        p.setdefault("horizon", "scalp")
        series = mock.Mock(return_value={"candles": c1, "source": "geckoterminal" if c1 else "none"})
        load = mock.Mock(return_value=([], None))
        patches = [mock.patch("desk.solana_agent.candle_chart.series", series),
                   mock.patch.object(dr, "load_candles", load),
                   mock.patch.object(chart, "_network_ohlcv", mock.Mock(return_value=[])),
                   mock.patch("desk.solana_agent.time.time", return_value=NOW),
                   mock.patch.dict(RULEBOOK, {"momentum_exit_timeframe": tf})]
        if fb is not None:
            patches.append(mock.patch.object(marketdata, "finalist_bars", return_value=fb))
        for x in patches:
            x.start()
        self.addCleanup(lambda: [x.stop() for x in patches])
        return self.a._rulebook_exit(self.mint, p, None), load

    def c1_under(self):
        # 1m candles for the last 15 minutes; the last closed 5m bucket closes at 1.19 (< 5m low 1.20)
        out = []
        for i in range(15):
            t = NOW - 15 * 60 + i * 60
            c = 1.30 if i < 10 else 1.19
            out.append({"t": t, "o": c, "h": c, "l": c, "c": c, "v": 1})
        return out

    def test_5m_mode_aggregates_the_real_1m_candles_and_exits(self):
        r, load = self.run_exit("5m", self.c1_under())
        self.assertEqual(r, (1.0, "momentum: 5m closed under the entry candle low"))
        load.assert_not_called()                                           # no extra request

    def test_1m_mode_is_untouched_by_5m_data(self):
        r, _ = self.run_exit("1m", self.c1_under(), p=pos(horizon="scalp", opened=OPENED, entry=1.0, entry_candle_low=1.0))
        self.assertIsNone(r)

    def test_5m_mode_with_no_bars_skips_and_counts(self):
        fb = {"1m": [], "5m": [], "source": "none", "cause": "gt_cooling", "tape_ticks_5m": 0}
        r, load = self.run_exit("5m", [], fb=fb)
        self.assertIsNone(r)
        self.assertEqual(self.a.stats["momentum5_no_bars"], 1)
        load.assert_not_called()

    def test_5m_mode_falls_back_to_the_cached_bars_and_the_last_good_cache(self):
        under = bars([1.3, 1.25, 1.19])
        for src, extra in (("geckoterminal", {}), ("cache", {"via": "geckoterminal", "age_s": 120.0})):
            fb = {"1m": [], "5m": under, "source": src, "cause": None, "tape_ticks_5m": 0, **extra}
            self.setUp()
            r, load = self.run_exit("5m", [], fb=fb)
            self.assertEqual(r, (1.0, "momentum: 5m closed under the entry candle low"), src)

    def test_a_thin_price_tape_is_not_a_5m_read(self):
        thin = {"1m": [], "5m": bars([1.3, 1.25, 1.19]), "source": "price_tape", "cause": None, "tape_ticks_5m": 1}
        self.assertIsNone(self.run_exit("5m", [], fb=thin)[0])
        self.setUp()
        ok = {**thin, "tape_ticks_5m": 6}
        self.assertEqual(self.run_exit("5m", [], fb=ok)[0], (1.0, "momentum: 5m closed under the entry candle low"))

    def test_bars_come_from_cache_only(self):
        with mock.patch.object(marketdata, "finalist_bars", return_value={"5m": []}) as fbm:
            self.a._exit_bars_5m(self.mint, [], NOW)
        self.assertIs(fbm.call_args.kwargs.get("fetch", fbm.call_args.args[3] if len(fbm.call_args.args) > 3 else None), False)

    def test_paper_and_entry_wiring(self):
        src = Path(sys.modules["desk.solana_agent"].__file__).read_text()
        i = src.index("self._rulebook_exit(")
        self.assertIn('self.mode != "live"', src[max(0, i - 400):i])
        self.assertIn('f["entry_candle_low_5m"] = low5', src)
        self.assertIn('_meta["entry_candle_low_5m"] = min(f["entry_candle_low_5m"], buy_price)', src)


if __name__ == "__main__":
    unittest.main()
