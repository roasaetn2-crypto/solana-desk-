"""Known-value tests for the pure-Python indicators.

The RSI fixture is Wilder's own 14-period example from *New Concepts in
Technical Trading Systems*; the rest are exact arithmetic on hand-built series
so a regression in the maths fails loudly.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESK_OFFLINE", "1")

from desk import indicators as ind  # noqa: E402


def candles_from_closes(closes, spread=0.5):
    out = []
    for i, c in enumerate(closes):
        out.append({"ts": 1_700_000_000 + i * 300, "open": c,
                    "high": c + spread, "low": c - spread, "close": c,
                    "vol": 1000.0})
    return out


def test_rsi_matches_wilder_reference_table():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
              46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41,
              46.22, 45.64]
    series = ind.rsi_series(closes, 14)
    assert round(series[14], 2) == 70.46
    assert round(series[15], 2) == 66.25
    assert round(series[-1], 2) == 57.92


def test_rsi_extremes_and_flat():
    assert ind.rsi([float(i) for i in range(1, 40)], 14) == 100.0
    assert ind.rsi([5.0] * 40, 14) == 50.0


def test_rsi_needs_history():
    assert ind.rsi([1.0, 2.0, 3.0], 14) is None


def test_sma_and_ema_seed_agree():
    vals = [float(i) for i in range(1, 40)]
    assert ind.sma(vals, 9) == 35.0
    assert round(ind.ema(vals, 9), 6) == 35.0      # EMA seeds with the SMA
    assert ind.ema(vals, 9) == ind.ema_series(vals, 9)[-1]


def test_atr_constant_range():
    candles = [{"open": 10, "high": 11, "low": 9, "close": 10, "vol": 1}
               for _ in range(40)]
    assert round(ind.atr(candles, 14), 6) == 2.0


def test_bollinger_exact():
    closes = [float(i) for i in range(1, 41)]
    b = ind.bollinger(closes, 20, 2.0)
    # window 21..40 -> mean 30.5, population sd sqrt(33.25)
    assert round(b["mid"], 6) == 30.5
    assert round(b["sd"], 6) == round(math.sqrt(33.25), 6)
    assert round(b["upper"], 6) == round(30.5 + 2 * math.sqrt(33.25), 6)
    assert 0.0 <= b["pct_b"] <= 1.0


def test_stochastic_rising_is_high():
    closes = [float(i) for i in range(1, 41)]
    st = ind.stochastic(candles_from_closes(closes), 14, 3, 3)
    assert st["k"] > 90 and st["d"] > 90


def test_adx_strong_uptrend():
    closes = [float(i) for i in range(1, 60)]
    a = ind.adx(candles_from_closes(closes), 14)
    assert a["plus_di"] > a["minus_di"]
    assert a["adx"] > 50


def test_macd_positive_on_uptrend():
    m = ind.macd([float(i) for i in range(1, 80)])
    assert m["macd"] > 0 and m["hist"] > -1e-9


def test_fibonacci_levels_and_position():
    closes = [10.0 + i for i in range(50)]
    f = ind.fibonacci(candles_from_closes(closes, spread=0.5), 50)
    assert f["high"] == 59.5 and f["low"] == 9.5
    assert round(f["levels"][0.5], 6) == round((59.5 + 9.5) / 2, 6)
    assert 0.0 <= f["position"] <= 1.0


def test_all_short_inputs_return_none_not_raise():
    short = candles_from_closes([1.0, 2.0, 3.0])
    assert ind.atr(short, 14) is None
    assert ind.bollinger([1.0, 2.0], 20) is None
    assert ind.stochastic(short, 14) is None
    assert ind.adx(short, 14) is None
    assert ind.macd([1.0, 2.0], 12, 26, 9) is None
    assert ind.fibonacci(short, 50) is None


def test_snapshot_shape_and_none_safety():
    snap = ind.snapshot(candles_from_closes([float(i) for i in range(1, 80)]))
    for key in ("rsi14", "macd", "atr14", "bollinger", "stoch", "adx14",
                "fib", "ema9", "ema21", "ema50", "close"):
        assert key in snap
    assert snap["rsi14"] is not None and snap["ema50"] is not None
    empty = ind.snapshot([])
    assert empty["rsi14"] is None and empty["close"] is None
