"""The backtest must measure a RETURN, not a dollar price.

`scripts/backtest_100.py` compared the raw token price (e.g. $0.000498) against a
$1.03 cost basis, so a +2.1% trade was logged as -100%. That single mistake
produced the "13% win rate / 81% rug rate" report that the whole desk was being
judged on. These tests pin the arithmetic down so it cannot come back.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
from backtest_gates import passes, prefix, simulate  # noqa: E402

HOLD_FOREVER = {
    "stop_pct": 0.99, "tp1_at": 99.0, "tp1_frac": 0.0, "tp2_at": 99.0,
    "tp2_frac": 0.0, "trail_pct": 0.99, "timeout_hours": 1e6,
    "timeout_min_gain": 0.05, "max_hold_hours": 1e6,
}


def candles_from(prices: list[float]) -> list[dict]:
    return [{"ts": i * 300, "open": p, "high": p, "low": p, "close": p, "vol": 100.0}
            for i, p in enumerate(prices)]


def round_trip(entry: float, exit_: float) -> dict:
    c = candles_from([entry, exit_])
    return simulate(c, prefix(c), 0, HOLD_FOREVER)


def test_a_ten_percent_gain_is_a_gain_not_a_total_loss():
    """The exact bug: $0.0005 -> $0.00055 used to score -100%."""
    t = round_trip(0.0005, 0.00055)
    assert t is not None
    # 1.10 gross, minus 1% fee + 2% slippage on both sides
    assert 2.0 < t["ret_pct"] < 5.0, t["ret_pct"]
    assert t["pnl"] > 0


def test_a_micro_priced_coin_can_win():
    """A token at 1e-7 is not automatically a loser."""
    t = round_trip(1.0e-7, 1.5e-7)
    assert t["ret_pct"] > 30.0


def test_a_fifty_percent_drop_is_about_minus_fifty_two():
    t = round_trip(0.01, 0.005)
    assert -56.0 < t["ret_pct"] < -48.0, t["ret_pct"]
    assert t["pnl"] < 0


def test_flat_price_loses_only_the_round_trip_cost():
    """No move at all should cost roughly fee+slippage both ways (~6%)."""
    t = round_trip(0.002, 0.002)
    assert -7.0 < t["ret_pct"] < -5.0, t["ret_pct"]


def test_return_is_scale_invariant():
    """The same percentage path must score the same at any price level."""
    a = round_trip(0.000001, 0.000002)
    b = round_trip(1.0, 2.0)
    assert abs(a["ret_pct"] - b["ret_pct"]) < 0.01


def test_returns_are_capped_at_plus_1000():
    t = round_trip(1.0e-9, 1.0e-3)   # a million-x
    assert t["ret_pct"] <= 1000.0


def test_ladder_is_cumulative_and_reports_the_first_failure():
    """Rung N must apply gates 1..N, and name the first one that said no."""
    from backtest_gates import GATES

    old_pool = {"liq": 50_000, "mcap": 1_000_000, "buys_h1": 100,
                "sells_h1": 50, "buyers_h1": 90}
    f = {"vol_1h": 10_000, "chg_1h": 0.1, "age_min": 5_000, "vol_trend": 1.0}
    sc = {"total": 60, "parts": {"community": 40}}

    ok, why = passes(GATES[:1], old_pool, f, sc)
    assert (ok, why) == (False, "too_old")

    ok, why = passes(GATES[:3], old_pool, f, sc)
    assert (ok, why) == (False, "too_old")   # scan still runs first

    young = dict(f, age_min=120)
    ok, why = passes(GATES[:3], old_pool, young, sc)
    assert ok is True


def test_fee_and_slippage_come_from_config():
    t = round_trip(0.01, 0.01)
    f, s = config.PAPER["fee_pct"], config.PAPER["slippage_pct"]
    # pay (f+s) to get in, receive (1-f-s) of the price to get out
    expected = 100.0 * ((1 - f - s) / (1 + f + s) - 1.0)
    assert abs(t["ret_pct"] - expected) < 0.01, (t["ret_pct"], expected)
