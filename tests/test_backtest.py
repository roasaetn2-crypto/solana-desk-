"""Read-only backtest: replay recorded tickets, never invent prices, never retune live gates."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from desk import backtest
from legacy_book import legacy_book  # noqa: E402


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def write_journal(rows, folder=None):
    d = Path(folder or tempfile.mkdtemp())
    path = d / "journal.jsonl"
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def token(**kw):
    base = dict(mint="MintGood111111111111111111111111111111", symbol="GOOD",
                price_usd=0.001, mcap_usd=300_000, liquidity_usd=40_000,
                volume_m5=5_000, volume_h1=20_000, volume_h6=80_000, volume_h24=200_000,
                buys_h1=200, sells_h1=120, buyers_h1=150, change_m5=-6, change_h1=10,
                age_minutes=45, source="test")
    base.update(kw)
    return base


def thesis(score=8, verdict="valid_setup"):
    return {"verdict": verdict, "score": score, "red_flags": [], "green_flags": ["x"],
            "shape": "crowd", "invalidation": "x", "sources": ["buyers_h1"]}


def scorecard(total=66):
    return {"total": total, "verdict": "watch", "parts": {"community": 60}}


def decision(ts, tok, approved=True, reason="approved", **kw):
    feat_score = kw.pop("rules_score", None)
    row = {"time": _iso(ts), "kind": "decision", "mint": tok["mint"], "symbol": tok["symbol"],
           "token": tok, "thesis": kw.pop("thesis", thesis()), "scorecard": scorecard(),
           "approved": approved, "reason": reason, "ticket_usd": 5}
    if feat_score is not None:
        row["rules_score"] = feat_score
    row.update(kw)
    return row


class Features(unittest.TestCase):
    def test_rules_features_match_old_desk_formulas(self):
        # volume_h1 20000 -> alive 0.80; buys 200 -> organic ~1.0 (clamped);
        # change_m5 -6 and no m5 book -> flush 0.5; change_h1 10 -> chase 0.125
        f = backtest.rules_features(token())
        self.assertAlmostEqual(f["alive_6h"], 20_000 / 25_000)
        self.assertAlmostEqual(f["organic"], 1.0)
        self.assertEqual(f["flush"], 0.5)
        self.assertAlmostEqual(f["chase"], 10 / 80)
        self.assertIsInstance(f["rules_score"], int)

    def test_missing_fields_are_none_not_zero(self):
        f = backtest.rules_features({"mint": "X"})
        self.assertIsNone(f["alive_6h"])
        self.assertIsNone(f["chase"])
        self.assertIsNone(f["rules_score"])


class Gates(unittest.TestCase):
    def _cand(self, **feat):
        tok = token()
        features = backtest.rules_features(tok)
        features.update(feat)
        return {"mint": tok["mint"], "ts": 1, "symbol": "GOOD", "token": tok,
                "thesis": thesis(), "features": features, "approved": True,
                "reason": "approved", "stage": None, "hard": False}

    def test_higher_min_score_rejects(self):
        c = self._cand()
        s = dict(backtest.current_settings(), min_score=0, require_valid_setup=False,
                 min_thesis_score=None, max_h1_change_pct=None)
        self.assertEqual(backtest.decide(c, dict(s, min_score=50))[0], "pass")
        # push the recorded score under the bar
        c["features"]["rules_score"] = 52
        c["features"]["meme_score"] = 52
        self.assertEqual(backtest.decide(c, dict(s, min_score=55))[0], "reject")

    def test_current_settings_need_a_valid_setup(self):
        c = self._cand()
        c["approved"] = False
        c["thesis"] = {}
        c["reason"] = "lead: lead paused"
        self.assertEqual(backtest.decide(c, backtest.current_settings())[0], "reject")
        # alternative score gates may still consider it
        alt = dict(backtest.current_settings(), require_valid_setup=False,
                   min_thesis_score=None, min_score=0, max_chase=1, min_alive_6h=0)
        self.assertEqual(backtest.decide(c, alt)[0], "pass")

    def test_hard_kill_never_reopens(self):
        c = self._cand()
        c["hard"] = True
        c["reason"] = "mint_authority"
        c["stage"] = "chain"
        self.assertEqual(backtest.decide(c, backtest.current_settings())[0], "reject")
        loose = dict(backtest.current_settings(), min_score=0, max_chase=1, min_alive_6h=0,
                     max_price_gap_pct=90, require_valid_setup=False, min_thesis_score=None)
        self.assertEqual(backtest.decide(c, loose)[0], "reject")

    def test_price_gap_unknown_when_size_missing_and_cap_loosened(self):
        c = self._cand()
        c["reason"] = "price_gap"
        c["features"]["gap_pct"] = None
        tight = dict(backtest.current_settings(), max_price_gap_pct=10, require_valid_setup=False,
                     min_thesis_score=None)
        loose = dict(backtest.current_settings(), max_price_gap_pct=40, require_valid_setup=False,
                     min_thesis_score=None)
        self.assertEqual(backtest.decide(c, tight)[0], "reject")
        self.assertEqual(backtest.decide(c, loose)[0], "unknown")

    def test_missing_alive_is_unknown_when_gate_is_on(self):
        c = self._cand()
        c["features"]["alive_6h"] = None
        s = dict(backtest.current_settings(), min_alive_6h=0.7, min_score=0,
                 require_valid_setup=False, min_thesis_score=None, max_h1_change_pct=None,
                 max_chase=1)
        self.assertEqual(backtest.decide(c, s)[0], "unknown")


@legacy_book
class Replay(unittest.TestCase):
    def setUp(self):
        self.t0 = 1_700_000_000

    def _path_journal(self, entry=0.001, later=(0.0007, 0.0006), age=45):
        tok = token(price_usd=entry, age_minutes=age)
        t0 = self.t0
        rows = [decision(t0, tok)]
        # later quotes ~1 minute apart so the 20% scalp stop can fire
        for i, p in enumerate(later, start=1):
            rows.append({"time": _iso(t0 + i * 60), "kind": "quote", "mint": tok["mint"],
                         "symbol": tok["symbol"], "price_usd": p,
                         "volume_h6": 10_000, "volume_h24": 40_000})
        return write_journal(rows), tok

    def test_stop_uses_recorded_path_and_scalp_20_pct(self):
        path, tok = self._path_journal(entry=0.001, later=(0.0007,))  # -30%, beyond 20% scalp stop
        hist = backtest.load_journal(path)
        row = backtest.simulate(hist, backtest.current_settings())
        self.assertGreaterEqual(row["trades"], 1)
        self.assertLess(row["pnl_usd"], 0)
        self.assertEqual(row["unknown"], 0)

    def test_no_later_prices_are_unknown_not_pnl(self):
        tok = token()
        path = write_journal([decision(self.t0, tok)])
        hist = backtest.load_journal(path)
        row = backtest.simulate(hist, backtest.current_settings())
        self.assertEqual(row["trades"], 0)
        self.assertEqual(row["pnl_usd"], 0)
        self.assertGreaterEqual(row["unknown"], 1)
        self.assertIn("no later prices", row["unknown_reasons"])

    def test_higher_min_score_takes_fewer_trades(self):
        # two coins: one high score, one mid score; both have a losing path
        t0 = self.t0
        a = token(mint="MintHigh111111111111111111111111111111", symbol="HI",
                  volume_h1=25_000, buys_h1=200, change_h1=5, change_m5=-6)
        b = token(mint="MintMid1111111111111111111111111111111", symbol="MID",
                  volume_h1=18_000, buys_h1=120, change_h1=20, change_m5=1)
        rows = [
            decision(t0, a),
            {"time": _iso(t0 + 60), "kind": "quote", "mint": a["mint"], "price_usd": 0.0007,
             "volume_h6": 1, "volume_h24": 4},
            decision(t0 + 5, b),
            {"time": _iso(t0 + 65), "kind": "quote", "mint": b["mint"], "price_usd": 0.0007,
             "volume_h6": 1, "volume_h24": 4},
        ]
        hist = backtest.load_journal(write_journal(rows))
        fa, fb = backtest.rules_features(a), backtest.rules_features(b)
        # mid coin should clear 50 but sit below 65
        self.assertGreaterEqual(fa["rules_score"], 65)
        self.assertGreaterEqual(fb["rules_score"], 50)
        self.assertLess(fb["rules_score"], 65)
        loose = dict(backtest.current_settings(), min_score=50, max_chase=1.0,
                     min_alive_6h=0, max_h1_change_pct=None,
                     min_thesis_score=None, require_valid_setup=False)
        tight = dict(loose, min_score=65)
        r_loose = backtest.simulate(hist, loose)
        r_tight = backtest.simulate(hist, tight)
        self.assertGreater(r_loose["passed_gates"], r_tight["passed_gates"])
        self.assertGreaterEqual(r_loose["trades"], r_tight["trades"])

    def test_current_row_is_marked_and_config_is_untouched(self):
        before = (config.MIN_SCORE, config.ENTRY["max_price_gap_pct"],
                  config.SCAN["max_h1_change_pct"], config.PAPER["starting_equity_usd"])
        path, _ = self._path_journal()
        data = backtest.run(backtest.load_journal(path))
        current = [r for r in data["rows"] if r["current"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["label"], "current")
        self.assertEqual(current[0]["settings"]["max_price_gap_pct"], config.ENTRY["max_price_gap_pct"])
        self.assertEqual(
            (config.MIN_SCORE, config.ENTRY["max_price_gap_pct"],
             config.SCAN["max_h1_change_pct"], config.PAPER["starting_equity_usd"]),
            before,
        )

    def test_max_four_positions_and_twenty_dollar_book(self):
        t0 = self.t0
        rows = []
        # five otherwise identical names, priced path stays flat so they stay open
        for i in range(5):
            mint = f"MintOpen{i:02d}11111111111111111111111111111"
            tok = token(mint=mint, symbol=f"O{i}", price_usd=0.001, age_minutes=45)
            rows.append(decision(t0 + i, tok))
            for j in range(1, 4):
                rows.append({"time": _iso(t0 + 30 + j), "kind": "quote", "mint": mint,
                             "price_usd": 0.00101, "volume_h6": 10_000, "volume_h24": 40_000})
        hist = backtest.load_journal(write_journal(rows))
        row = backtest.simulate(hist, backtest.current_settings())
        # 5 passed the gates but the book only carries 4; none have closed (flat path)
        self.assertGreaterEqual(row["passed_gates"], 5)
        self.assertEqual(row["trades"], 0)
        self.assertEqual(row["open_end"], 4)
        self.assertLessEqual(20 - row["end_equity"], 0.5)  # still about the $20 book, fees on 4 tickets
        # four ~$5 tickets plus fees should leave a little cash, never a 5th ticket
        self.assertGreater(row["end_equity"], 0)

    def test_does_not_read_or_write_live_settings_file(self):
        path, _ = self._path_journal()
        # running the report must not create/alter config
        src = Path(__file__).resolve().parents[1] / "config.py"
        before = src.read_bytes()
        backtest.run(backtest.load_journal(path))
        self.assertEqual(src.read_bytes(), before)


class OldDeskGap(unittest.TestCase):
    def test_parse_gap_from_ticket_reason(self):
        p1, p2, gap = backtest._parse_gap_reason(
            "confirm: second source: price disagrees: GeckoTerminal 3.93958e-05 vs DexScreener 3.469e-05")
        self.assertAlmostEqual(p1, 3.93958e-05)
        self.assertAlmostEqual(p2, 3.469e-05)
        self.assertGreater(gap, 10)
        self.assertLess(gap, 20)


if __name__ == "__main__":
    unittest.main()
