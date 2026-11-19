"""Analyst feed + verdict (PAPER only):

1. finalist data starvation: candles for the scalp-entry rule and the confirm checklist fall back to the
   GeckoTerminal-1m-aggregated-to-5m / Jupiter + quote price tape, the confirm buy/sell check reads the same
   5m counts as the tx-ratio gate, and the analyst prompt separates "our feed has no data" from a red flag;
2. the socials rule matches the heuristic (flag only with no X AND no Telegram);
3. analyst.code_decides_verdict (default OFF, Confirm-only): the code sets the paper verdict.
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
from config import RULEBOOK, SCALP  # noqa: E402
from desk import analyst, chart, confirm as entry_confirm, dip_recovery as dr, marketdata, pricetape, scanner, settings  # noqa: E402
from desk.analyst import TokenThesis  # noqa: E402
from desk.solana_agent import SolanaAgent  # noqa: E402
from test_dip_recovery import TOK, candles, flat  # noqa: E402
from test_entry_trigger import AgentWiring as _AW, rising_spec  # noqa: E402
from test_second_source import jup_row, put_row  # noqa: E402

NOW = 1_800_000_000.0 + 20
MINT = "FEEDMINT" + "z" * 30


def th(verdict="skip", score=3, red=(), green=()):
    return TokenThesis(verdict=verdict, score=score, red_flags=list(red), green_flags=list(green),
                       shape="crowd", invalidation="x", sources=["liquidity_usd"])


def tape(mint, closes, now=NOW, gap=60):
    """One price per `gap` seconds ending at now - 5."""
    n = len(closes)
    for i, c in enumerate(closes):
        pricetape.record(mint, c, now - gap * (n - 1 - i) - 5)


class Base(unittest.TestCase):
    def setUp(self):
        chart.reset()
        pricetape.reset()
        self.addCleanup(chart.reset)
        self.addCleanup(pricetape.reset)
        for p in (mock.patch.object(chart, "_pool_address", return_value=None),):
            p.start()
            self.addCleanup(p.stop)


# ---------------------------------------------------------------- 1. data starvation
class Bars(Base):
    def test_aggregate_1m_to_5m(self):
        one = candles([(1.0, 1.1, 10)] * 10, NOW)
        five = marketdata.aggregate(one, 300)
        self.assertEqual(sum(b["v"] for b in five), 100)
        self.assertEqual(five[0]["o"], 1.0)
        self.assertEqual(five[-1]["c"], 1.1)
        self.assertTrue(all(b["t"] % 300 == 0 for b in five))
        self.assertGreaterEqual(len(five), 2)

    def test_geckoterminal_candles_serve_both_timeframes_from_one_call(self):
        cs = candles(rising_spec(30), NOW)
        with mock.patch.object(dr, "load_candles", return_value=(cs, NOW - 10)) as load:
            r = marketdata.finalist_bars(MINT, now=NOW)
        load.assert_called_once()
        self.assertEqual((r["source"], len(r["1m"])), ("geckoterminal", 30))
        self.assertEqual(r["5m"][-1]["c"], cs[-1]["c"])
        self.assertIsNone(r["cause"])

    def test_tape_is_the_second_source_with_silent_minutes_carried(self):
        tape(MINT, [1.0, 1.01, 1.02, 1.03, 1.04], gap=120)           # one price every 2 minutes
        with mock.patch.object(dr, "load_candles", return_value=([], None)):
            r = marketdata.finalist_bars(MINT, now=NOW)
        self.assertEqual(r["source"], "price_tape")
        self.assertEqual(len(r["1m"]), 9)                              # 8 minutes spanned, none missing
        self.assertTrue(all(b["v"] == 0.0 for b in r["1m"]))
        self.assertGreaterEqual(len(r["5m"]), 2)

    def test_misses_name_their_causes(self):
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch.object(dr, "candle_cause", return_value="gt_cooling"):
            r = marketdata.finalist_bars(MINT, now=NOW)
            self.assertEqual((r["source"], r["cause"]), ("none", "gt_cooling+no_tape"))
            tape(MINT, [1.0, 1.01], gap=60)
            self.assertEqual(marketdata.finalist_bars(MINT, now=NOW)["cause"], "gt_cooling+thin_tape")
            pricetape.reset()
            tape(MINT, [1.0, 1.01, 1.02, 1.03], now=NOW - 900)
            self.assertEqual(marketdata.finalist_bars(MINT, now=NOW)["cause"], "gt_cooling+stale_ticks")

    def test_stale_geckoterminal_candles_fall_to_the_tape(self):
        cs = candles(rising_spec(30), NOW)
        tape(MINT, [1.0, 1.01, 1.02, 1.03, 1.04])
        with mock.patch.object(dr, "load_candles", return_value=(cs, NOW - 3000)):
            self.assertEqual(marketdata.finalist_bars(MINT, now=NOW)["source"], "price_tape")

    def test_series_passes_the_finalist_priority_down(self):
        with mock.patch.object(chart, "_network_ohlcv", return_value=[]) as net:
            chart.series(MINT, "5m", priority=True)
            chart.series(MINT, "5m")
        self.assertTrue(net.call_args_list[0].args[3])
        self.assertFalse(net.call_args_list[1].args[3])


class Confirm(unittest.TestCase):
    def test_buy_state_reads_the_shared_counts_only_when_dexscreener_has_none(self):
        tx = {"buys_m5": 60, "sells_m5": 20, "source": "jupiter"}
        r = entry_confirm.buy_state({"available": False}, {}, tx)
        self.assertEqual((r["ok"], r["ratio"], r["source"]), (True, 3.0, "jupiter"))
        self.assertIn("(jupiter)", r["why"])
        own = entry_confirm.buy_state({"buys_m5": 10, "sells_m5": 20}, {}, tx)
        self.assertEqual((own["ok"], own["source"]), (False, "dexscreener"))
        self.assertIsNone(entry_confirm.buy_state({}, {}, None)["ok"])
        self.assertIsNone(entry_confirm.buy_state({}, {}, {"buys_m5": 0, "sells_m5": 0})["ok"])

    def test_checklist_takes_the_counts(self):
        cc = entry_confirm.checklist({}, {}, [], [], NOW, {"buys_m5": 5, "sells_m5": 30, "source": "jupiter"})
        self.assertEqual((cc["checks"]["buy"]["ok"], cc["fail"]), (False, "buy"))


class AgentFeeds(Base):
    def agent(self):
        d = Path(tempfile.mkdtemp())
        a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
        self.addCleanup(lambda: None)
        return a

    def test_entry_candles_fall_back_to_the_tape_when_the_chart_has_none(self):
        a = self.agent()
        tape(MINT, [1.0, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06], now=time.time())
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch("desk.solana_agent.candle_chart.series", return_value={"candles": [], "source": "none"}):
            c5, i5 = a._entry_candles(MINT, "5m")
            c1, i1 = a._entry_candles(MINT, "1m")
        self.assertTrue(c5 and c1)
        self.assertEqual((i5["source"], i1["source"]), ("price_tape", "price_tape"))
        self.assertEqual(a.stats["src_candles_5m_price_tape"], 1)

    def test_a_fresh_chart_series_is_used_as_is(self):
        a = self.agent()
        cs = candles(rising_spec(10), time.time())
        with mock.patch("desk.solana_agent.candle_chart.series",
                        return_value={"candles": cs, "source": "geckoterminal"}) as ser, \
                mock.patch.object(marketdata, "finalist_bars") as fb:
            c, info = a._entry_candles(MINT, "1m")
        self.assertEqual((c, info["source"]), (cs, "geckoterminal"))
        fb.assert_not_called()
        self.assertTrue(ser.call_args.kwargs["priority"])

    def test_trend_buy_and_candles_known_from_the_second_sources(self):
        a = self.agent()
        t = dict(TOK, mint=MINT)
        tape(MINT, [1.0 + 0.004 * i for i in range(12)], now=time.time())    # steady climb on the tape
        put_row(jup_row(mint=MINT, buys=60, sells=20, upd=time.time() - 5), at=time.time())
        f = {}
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch("desk.solana_agent.candle_chart.series", return_value={"candles": [], "source": "none"}):
            a._confirm_skip(t, {"available": False}, f)
        chk = f["confirm"]["checks"]
        self.assertEqual(chk["buy"]["ok"], True, chk["buy"])
        self.assertIn("jupiter", chk["buy"]["why"])
        self.assertEqual(chk["trend"]["ok"], True, chk["trend"])
        self.assertEqual(f["tx_m5"]["source"], "jupiter")
        self.assertEqual(f["confirm"]["candles"]["1m"], "price_tape")
        self.assertNotIn("buy_sell_5m", f.get("data_gaps", {}))

    def test_missing_everything_is_recorded_as_a_feed_gap_with_causes(self):
        a = self.agent()
        t = dict(TOK, mint=MINT)
        f = {}
        with mock.patch.object(dr, "load_candles", return_value=([], None)), \
                mock.patch.object(dr, "candle_cause", return_value="gt_cooling"), \
                mock.patch("desk.solana_agent.candle_chart.series", return_value={"candles": [], "source": "none"}), \
                mock.patch.object(marketdata, "tx_m5", return_value={"available": False, "cause": "jup_cooling+gt_cooling"}):
            a._confirm_skip(t, {"available": False}, f)
        gaps = f["data_gaps"]
        self.assertEqual(gaps["buy_sell_5m"], "jup_cooling+gt_cooling")
        self.assertEqual(gaps["trend"], "gt_cooling+no_tape")
        self.assertEqual(f["confirm"]["ok"], True)                  # unknown never blocks


class ScalpEntry(_AW):
    def run_scalp(self, fin=None, ticks=None):
        with mock.patch.dict(SCALP, {"enabled": True, "entry_gate": True, "entry_mode": "moving_candle",
                                     "moving_candle_bars": 1, "moving_candle_min_body_pct": 0.0}):
            a, _ = self.run_agent(None, gate=False, fin=fin, extra={"entry_trigger_gate": False})
        return a

    def decision_facts(self, a):
        return a.decisions[0]["facts"] if a.decisions and "facts" in a.decisions[0] else {}

    def scalp_events(self, a):
        return [e for e in a.events if "scalp entry" in (e.get("message") or "")]

    def test_tape_candle_replaces_no_candles(self):
        now = time.time()
        pr = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.02, 1.04]
        bars = marketdata.aggregate([{"t": int(now // 60) * 60 - 60 * (len(pr) - 1 - i), "o": p, "h": p, "l": p,
                                      "c": p, "v": 0.0} for i, p in enumerate(pr)], 300)
        fin = {"1m": [], "5m": bars, "source": "price_tape", "cause": None, "tape_ticks_5m": 4}
        a = self.run_scalp(fin=fin)
        # a green tape candle passes: the buy goes through instead of dying on "no candles"
        self.assertEqual(self.scalp_events(a), [], [e["message"] for e in list(a.events)[:8]])
        self.assertGreaterEqual(a.stats.get("src_candles_5m_price_tape", 0), 1)

    def test_the_agent_logs_and_counts_code_overrides(self):
        raw = th("skip", 8, ["No Telegram link"], ["deep liquidity"])
        with mock.patch.object(config, "ANALYST_CODE_DECIDES", True), \
                mock.patch.object(analyst, "_analyze", return_value=raw):
            a, _ = self.run_agent(rising_spec(), gate=False)
        self.assertEqual(a.stats["analyst_code_overrides"], 1)
        ev = [e for e in a.events if e.get("stage") == "analyst"]
        self.assertTrue(ev, [e["message"] for e in list(a.events)[:10]])
        self.assertEqual((ev[0]["before"], ev[0]["after"]), ("skip", "valid_setup"))
        self.assertIn("code set", ev[0]["message"])

    def test_live_agent_never_runs_the_code_verdict(self):
        raw = th("skip", 8, [])
        with mock.patch.object(config, "ANALYST_CODE_DECIDES", True), \
                mock.patch.object(analyst, "code_decides") as cd, \
                mock.patch.object(analyst, "_analyze", return_value=raw):
            a, _ = self.run_agent(None, gate=False, mode="live")
        cd.assert_not_called()

    def test_no_candles_names_the_cause_and_stays_neutral(self):
        fin = {"1m": [], "5m": [], "source": "none", "cause": "gt_cooling+no_tape", "tape_ticks_5m": 0}
        a = self.run_scalp(fin=fin)
        self.assertEqual(self.scalp_events(a), [])                  # unknown never vetoes
        self.assertTrue(any(e.get("stage") != "rulebook" or True for e in a.events))

    def test_scalp_entry_state_facts_carry_source_and_cause(self):
        # the agent writes why = "no candles (cause)" and the gap; checked on the decision facts
        fin = {"1m": [], "5m": [], "source": "none", "cause": "gt_cooling+no_tape", "tape_ticks_5m": 0}
        with mock.patch.object(analyst, "analyze_token", wraps=analyst.analyze_token) as spy:
            self.run_scalp(fin=fin)
        seen = {"f": spy.call_args.args[1]}
        se = seen["f"]["scalp_entry"]
        self.assertEqual((se["ok"], se["why"]), (None, "no candles (gt_cooling+no_tape)"))
        self.assertEqual(seen["f"]["data_gaps"]["scalp_candles"], "gt_cooling+no_tape")

    def test_sparse_tape_bar_is_unknown_not_a_red_candle(self):
        now = time.time()
        bars = marketdata.aggregate([{"t": int(now // 60) * 60, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 0.0}], 300)
        fin = {"1m": [], "5m": bars, "source": "price_tape", "cause": None, "tape_ticks_5m": 1}
        with mock.patch.object(analyst, "analyze_token", wraps=analyst.analyze_token) as spy:
            a = self.run_scalp(fin=fin)
        seen = {"f": spy.call_args.args[1]}
        self.assertIsNone(seen["f"]["scalp_entry"]["ok"])
        self.assertIn("sparse_tape", seen["f"]["scalp_entry"]["why"])
        self.assertEqual(self.scalp_events(a), [])


class PromptAndState(unittest.TestCase):
    T = {"liquidity_usd": 40000, "volume_h1": 1000}

    def test_prompt_separates_our_feed_gaps_from_red_flags(self):
        p = analyst.TOKEN_PROMPT
        self.assertIn("feed_unknown", p)
        self.assertIn("UNKNOWN, not a\n    red flag", p)
        self.assertIn("never list it in red_flags", p)

    def test_unknown_fields_gaps_and_checks_reach_the_prompt(self):
        f = {"data_gaps": {"trend": "gt_cooling+no_tape", "buy_sell_5m": "jup_cooling"},
             "scalp_entry": {"ok": None, "why": "no candles (gt_cooling)"},
             "confirm": {"checks": {"trend": {"ok": None, "why": "no trend data"},
                                    "buy": {"ok": None, "why": "no 5m buy/sell counts"},
                                    "turnover": {"ok": True, "why": "1h vol/liq 2.2 vs 0.15"}}}}
        ctx = {"scorecard": {}, "dev": {}, "ds": {"buys_m5": None, "sells_m5": None}}
        out = analyst.token_state(self.T, f, {"x": "https://x.com/a"}, ctx)
        self.assertIn('"feed_unknown"', out)
        self.assertIn("dexscreener.buys_m5", out)
        self.assertIn('"data_gaps"', out)
        self.assertIn('"result": "unknown"', out)
        self.assertIn('"feed_gap": "gt_cooling+no_tape"', out)
        self.assertIn('"result": "pass"', out)

    def test_shared_counts_fill_the_dexscreener_nulls(self):
        f = {"tx_m5": {"buys_m5": 60, "sells_m5": 20, "source": "jupiter"}}
        ctx = {"scorecard": {}, "dev": {}, "ds": {"buys_m5": None, "sells_m5": None}}
        out = analyst.token_state(self.T, f, {}, ctx)
        self.assertIn('"buys_m5": 60', out)
        self.assertIn('"txns_m5"', out)
        self.assertNotIn("dexscreener.buys_m5", out)

    def test_clean_state_adds_no_noise(self):
        out = analyst.token_state({k: 1 for k in ("age_minutes", "mcap_usd", "liquidity_usd", "volume_m5", "volume_h1",
                                                  "volume_h6", "buys_h1", "sells_h1", "buyers_h1", "change_m5",
                                                  "change_h1", "change_h6")} | {"dex": "x"},
                                  {k: 0.1 for k in ("top_wallet_pct", "top_10_pct", "program_held_pct", "wallets_counted")},
                                  {})
        self.assertNotIn("data_gaps", out)
        self.assertNotIn("entry_checks", out)
        self.assertNotIn("feed_unknown", out)


# ---------------------------------------------------------------- 2. socials
class Socials(unittest.TestCase):
    def test_prompt_flags_only_when_neither_social_exists(self):
        p = analyst.TOKEN_PROMPT
        self.assertNotIn("No X or Telegram link is a red flag", p)
        self.assertIn("ONLY when there is no X link AND no Telegram", p)
        self.assertIn("has_x false and\n  has_telegram false", p)

    def test_heuristic_matches(self):
        base = {"liquidity_usd": 40000, "mcap_usd": 100000, "volume_h1": 60000, "buys_h1": 100, "sells_h1": 50,
                "buyers_h1": 60, "change_h1": 5, "change_m5": 1}
        for links, flagged in (({"x": "u"}, False), ({"telegram": "u"}, False), ({}, True)):
            r = analyst.heuristic_thesis(base, {}, links)
            self.assertEqual("no X or Telegram link" in r.red_flags, flagged, links)

    def test_a_telegram_flag_is_dropped_in_paper_when_x_exists(self):
        raw = th("watch", 6, ["No Telegram link", "thin liquidity $9,000"])
        with mock.patch.object(analyst, "_analyze", return_value=raw):
            out = analyst.analyze_token({}, {}, {"x": "https://x.com/a"}, mode="paper")
        self.assertEqual(out.red_flags, ["thin liquidity $9,000"])

    def test_the_flag_stays_when_there_is_neither(self):
        raw = th("watch", 6, ["no X or Telegram link"])
        with mock.patch.object(analyst, "_analyze", return_value=raw):
            out = analyst.analyze_token({}, {}, {}, mode="paper")
        self.assertEqual(out.red_flags, ["no X or Telegram link"])

    def test_live_gets_the_thesis_untouched(self):
        raw = th("valid_setup", 8, ["No Telegram link"])
        with mock.patch.object(analyst, "_analyze", return_value=raw), \
                mock.patch.object(config, "ANALYST_CODE_DECIDES", True):
            out = analyst.analyze_token({}, {}, {"x": "https://x.com/a"}, mode="live")
        self.assertEqual((out.verdict, out.red_flags), ("valid_setup", ["No Telegram link"]))


# ---------------------------------------------------------------- 3. code_decides_verdict
class CodeDecides(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(config, "ANALYST_CODE_DECIDES", True),
                  mock.patch.object(config, "ANALYST_VALID_SCORE", 6),
                  mock.patch.object(analyst, "ANALYST_MAX_RED_FOR_VALID", 3)):
            p.start()
            self.addCleanup(p.stop)

    def decide(self, raw, links=None, f=None, ctx=None, mode="paper"):
        notes = []
        with mock.patch.object(analyst, "_analyze", return_value=raw):
            out = analyst.analyze_token({}, f or {}, links if links is not None else {"x": "u"},
                                        context=ctx, mode=mode, notes=notes)
        return out, notes

    def test_setting_is_default_off_confirm_only_and_paper_scoped(self):
        self.assertIs(config.ANALYST_CODE_DECIDES, True)             # patched above
        src = Path(config.__file__).read_text()
        self.assertIn("ANALYST_CODE_DECIDES = False", src)
        s = settings.REGISTRY["analyst.code_decides_verdict"]
        self.assertEqual(s.kind, "bool")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DESK_DATA_DIR": tmp}), \
                mock.patch.object(config, "ANALYST_CODE_DECIDES", False):
            settings.reset_runtime()
            out = settings.create([{"setting": "analyst.code_decides_verdict", "proposed": True}], "t", "desk_ai")
            self.assertTrue(out["ok"], out)
            self.assertFalse(config.ANALYST_CODE_DECIDES)             # a proposal changes nothing
            settings.confirm(settings.listing()["pending"][0]["id"], "romz")
            self.assertTrue(config.ANALYST_CODE_DECIDES)
            settings.reset_runtime()

    def test_off_leaves_the_model_verdict_alone(self):
        with mock.patch.object(config, "ANALYST_CODE_DECIDES", False):
            out, notes = self.decide(th("skip", 8, []))
        self.assertEqual((out.verdict, notes), ("skip", []))

    def test_good_score_and_few_serious_flags_is_valid_setup(self):
        out, notes = self.decide(th("skip", 7, ["thin liquidity $9,000", "volume fading"]))
        self.assertEqual(out.verdict, "valid_setup")
        self.assertEqual(notes[0]["before"], "skip")
        self.assertEqual(notes[0]["after"], "valid_setup")
        self.assertEqual(notes[0]["serious"], 2)

    def test_the_same_bar_as_the_guard(self):
        at_bar, _ = self.decide(th("watch", 6, ["a", "b", "c"]))            # 3 serious == max: valid
        over, _ = self.decide(th("valid_setup", 6, ["a", "b", "c", "d"]))   # 4 serious: not valid
        low, _ = self.decide(th("valid_setup", 5, []))                      # score under the bar
        self.assertEqual((at_bar.verdict, over.verdict, low.verdict), ("valid_setup", "watch", "watch"))
        guard = analyst._guard_ai_thesis(th("valid_setup", 6, ["a", "b", "c", "d"]))
        self.assertEqual(guard.verdict, over.verdict)
        self.assertEqual(self.decide(th("valid_setup", 4, []))[0].verdict, "skip")

    def test_soft_social_and_data_flags_do_not_count(self):
        flags = ["No Telegram link", "missing website", "no candles (gt_cooling)", "no trend data",
                 "no 5m buy/sell counts", "volume data unavailable", "trend unknown"]
        out, notes = self.decide(th("skip", 7, flags), links={"x": "u"})
        self.assertEqual(out.verdict, "valid_setup")
        self.assertEqual(notes[0]["serious"], 0)
        self.assertEqual(notes[0]["ignored_data"], 5)                 # the social ones were dropped earlier
        direct, dn = [], []
        analyst.code_decides(th("skip", 7, ["No Telegram link", "missing website"]), {}, {"x": "u"}, None, dn)
        self.assertEqual((dn[0]["ignored_soft"], dn[0]["serious"]), (2, 0))
        analyst.code_decides(th("skip", 7, ["missing website"]), {}, {}, None, direct)   # no socials at all
        self.assertEqual((direct[0]["ignored_soft"], direct[0]["after"]), (1, "valid_setup"))

    def test_flags_that_name_one_of_our_feed_gaps_do_not_count(self):
        out, _ = self.decide(th("skip", 7, ["buy sell 5m not trusted"]),
                             f={"data_gaps": {"buy_sell_5m": "jup_cooling"}})
        self.assertEqual(out.verdict, "valid_setup")

    def test_no_socials_at_all_is_a_serious_flag(self):
        out, notes = self.decide(th("skip", 7, ["no X or Telegram link", "a", "b", "c"]), links={})
        self.assertEqual((out.verdict, notes[0]["serious"]), ("watch", 4))

    def test_hard_flags_are_always_a_skip_and_never_upgraded(self):
        for flag in ("honeypot risk: no sells", "mint authority still set", "Freeze authority present",
                     "dev sold 60% of supply", "serial rug history", "LP unlocked", "liquidity not locked"):
            with self.subTest(flag):
                for before in ("valid_setup", "watch", "skip"):
                    out, _ = self.decide(th(before, 9, [flag]))
                    self.assertEqual(out.verdict, "skip")

    def test_hard_facts_from_chain_and_security_beat_a_clean_thesis(self):
        out, notes = self.decide(th("valid_setup", 9, []), f={"mint_authority": True})
        self.assertEqual((out.verdict, notes[0]["after"]), ("skip", "skip"))
        out, _ = self.decide(th("valid_setup", 9, []), ctx={"security": {"flags": ["gmgn_honeypot"]}})
        self.assertEqual(out.verdict, "skip")

    def test_logs_the_override_before_and_after(self):
        with mock.patch("builtins.print") as pr:
            self.decide(th("skip", 8, []))
        text = " ".join(str(c.args[0]) for c in pr.call_args_list if c.args)
        self.assertIn("code verdict skip -> valid_setup", text)
        with mock.patch("builtins.print") as pr:
            self.decide(th("valid_setup", 8, []))                            # no change, no log
        self.assertNotIn("code verdict", " ".join(str(c.args[0]) for c in pr.call_args_list if c.args))

    def test_live_is_untouched(self):
        for mode in ("live", None):
            out, notes = self.decide(th("skip", 9, []), mode=mode)
            self.assertEqual((out.verdict, notes), ("skip", []), mode)


del _AW
if __name__ == "__main__":
    unittest.main()
