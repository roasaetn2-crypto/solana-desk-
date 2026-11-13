"""Pure-Python technical indicators for the desk (no numpy / pandas / TA-Lib).

Why this module exists
----------------------
The desk runs on Android/Termux where pulling numpy + pandas is painful and slow
to install. Every function here is plain Python on ordinary lists, so it runs
anywhere CPython does. The maths follows the standard definitions (Wilder
smoothing for RSI/ATR/ADX, EMA for MACD, population stdev for Bollinger) and was
cross-checked against the MIT-licensed zero-dependency reference
https://github.com/Bitvested/ta.py (``ta_py``) plus the classic TA-Lib formulas.

Contract
--------
* Pure: no I/O, no clock, no global state. Same input -> same output.
* Honest: a function returns ``None`` when there is not enough history to be
  meaningful, rather than inventing a value. Callers must treat ``None`` as
  "unknown", which is the same convention the rest of the desk uses.
* Last-value helpers (``rsi``, ``ema`` ...) return a single float; series
  helpers return the full list aligned to the input length with ``None`` in the
  warm-up region.

This module is *not* wired into the live signal engine by default. It is used by
``desk/indicator_backtest.py`` to measure whether an indicator earns its place
before anything is added to the trading desk.
"""
from __future__ import annotations

import math

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _closes(candles: list[dict]) -> list[float]:
    return [float(c["close"]) for c in candles]


def _ok(values, period: int) -> bool:
    return bool(values) and len(values) >= period and period > 0


def _finite(x) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #
def sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values."""
    if not _ok(values, period):
        return None
    window = values[-period:]
    return sum(window) / period


def sma_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= period:
            run -= values[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def ema_series(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with the SMA of the first window."""
    out: list[float | None] = [None] * len(values)
    if not _ok(values, period):
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def ema(values: list[float], period: int) -> float | None:
    s = ema_series(values, period)
    return s[-1] if s and s[-1] is not None else None


# --------------------------------------------------------------------------- #
# RSI (Wilder)
# --------------------------------------------------------------------------- #
def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if not _ok(closes, period + 1):
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = d if d > 0 else 0.0
        loss = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi(closes: list[float], period: int = 14) -> float | None:
    s = rsi_series(closes, period)
    return s[-1] if s and s[-1] is not None else None


# --------------------------------------------------------------------------- #
# MACD
# --------------------------------------------------------------------------- #
def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict | None:
    """Return {macd, signal, hist} for the last bar, or None if too short."""
    if not _ok(closes, slow + signal):
        return None
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    line = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    vals = [v for v in line if v is not None]
    if len(vals) < signal:
        return None
    sig_series = ema_series(vals, signal)
    sig = sig_series[-1]
    if sig is None:
        return None
    line_last = vals[-1]
    return {"macd": line_last, "signal": sig, "hist": line_last - sig}


# --------------------------------------------------------------------------- #
# True range / ATR (Wilder)
# --------------------------------------------------------------------------- #
def true_ranges(candles: list[dict]) -> list[float]:
    out: list[float] = []
    prev_close = None
    for c in candles:
        hi, lo = float(c["high"]), float(c["low"])
        if prev_close is None:
            out.append(hi - lo)
        else:
            out.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        prev_close = float(c["close"])
    return out


def atr_series(candles: list[dict], period: int = 14) -> list[float | None]:
    tr = true_ranges(candles)
    out: list[float | None] = [None] * len(tr)
    if not _ok(tr, period):
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def atr(candles: list[dict], period: int = 14) -> float | None:
    s = atr_series(candles, period)
    return s[-1] if s and s[-1] is not None else None


# --------------------------------------------------------------------------- #
# Bollinger Bands
# --------------------------------------------------------------------------- #
def bollinger(closes: list[float], period: int = 20,
              deviations: float = 2.0) -> dict | None:
    if not _ok(closes, period):
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = math.sqrt(var)
    upper = mid + deviations * sd
    lower = mid - deviations * sd
    last = closes[-1]
    width = (upper - lower) / mid if mid else None
    pct_b = ((last - lower) / (upper - lower)) if upper > lower else None
    return {"mid": mid, "upper": upper, "lower": lower, "sd": sd,
            "bandwidth": width, "pct_b": pct_b}


# --------------------------------------------------------------------------- #
# Stochastic oscillator
# --------------------------------------------------------------------------- #
def stochastic(candles: list[dict], k_period: int = 14, d_period: int = 3,
               smooth: int = 3) -> dict | None:
    if not _ok(candles, k_period):
        return None
    raw_k: list[float] = []
    for i in range(k_period - 1, len(candles)):
        window = candles[i - k_period + 1:i + 1]
        hi = max(float(c["high"]) for c in window)
        lo = min(float(c["low"]) for c in window)
        close = float(candles[i]["close"])
        raw_k.append(50.0 if hi == lo else 100.0 * (close - lo) / (hi - lo))
    if len(raw_k) < smooth + d_period - 1:
        return None
    k_line = _sma_list(raw_k, smooth)
    d_line = _sma_list(k_line, d_period)
    if k_line[-1] is None or d_line[-1] is None:
        return None
    return {"k": k_line[-1], "d": d_line[-1]}


def _sma_list(values: list[float | None], period: int) -> list[float | None]:
    """SMA over a list that may contain ``None`` in its warm-up region."""
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / period
    return out


# --------------------------------------------------------------------------- #
# ADX / directional movement (Wilder)
# --------------------------------------------------------------------------- #
def adx(candles: list[dict], period: int = 14) -> dict | None:
    """Return {adx, plus_di, minus_di} for the last bar."""
    if not _ok(candles, 2 * period + 1):
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr = true_ranges(candles)
    for i in range(1, len(candles)):
        up = float(candles[i]["high"]) - float(candles[i - 1]["high"])
        down = float(candles[i - 1]["low"]) - float(candles[i]["low"])
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    # Wilder-smooth the DM/TR streams (skip the first TR, which has no DM pair)
    tr_s = _wilder(tr[1:], period)
    pdm_s = _wilder(plus_dm, period)
    mdm_s = _wilder(minus_dm, period)
    dx: list[float] = []
    plus_di = minus_di = None
    for i in range(len(tr_s)):
        if tr_s[i] is None or pdm_s[i] is None or mdm_s[i] is None or tr_s[i] == 0:
            continue
        pdi = 100.0 * pdm_s[i] / tr_s[i]
        mdi = 100.0 * mdm_s[i] / tr_s[i]
        plus_di, minus_di = pdi, mdi
        denom = pdi + mdi
        dx.append(0.0 if denom == 0 else 100.0 * abs(pdi - mdi) / denom)
    if len(dx) < period or plus_di is None:
        return None
    adx_val = sum(dx[:period]) / period
    for v in dx[period:]:
        adx_val = (adx_val * (period - 1) + v) / period
    return {"adx": adx_val, "plus_di": plus_di, "minus_di": minus_di}


def _wilder(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if not _ok(values, period):
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


# --------------------------------------------------------------------------- #
# Fibonacci retracement
# --------------------------------------------------------------------------- #
FIB_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)


def fibonacci(candles: list[dict], lookback: int = 50) -> dict | None:
    """Retracement levels of the last ``lookback`` bars' swing.

    ``position`` is where the last close sits in the swing: 0.0 at the low,
    1.0 at the high. ``nearest`` is the closest ratio to that position.
    """
    if not _ok(candles, lookback):
        return None
    window = candles[-lookback:]
    hi = max(float(c["high"]) for c in window)
    lo = min(float(c["low"]) for c in window)
    if hi <= lo:
        return None
    last = float(candles[-1]["close"])
    span = hi - lo
    levels = {r: hi - span * r for r in FIB_RATIOS}
    position = (last - lo) / span
    nearest = min(FIB_RATIOS, key=lambda r: abs((1.0 - r) - position))
    return {"high": hi, "low": lo, "levels": levels,
            "position": position, "nearest": nearest}


# --------------------------------------------------------------------------- #
# One-shot snapshot (used by the backtester; only candles[:i+1] are passed in)
# --------------------------------------------------------------------------- #
def snapshot(candles: list[dict]) -> dict:
    """Compute every indicator once for the last bar of ``candles``.

    Each field is ``None`` when there is not enough history. Never raises on
    short input.
    """
    closes = _closes(candles)
    return {
        "rsi14": rsi(closes, 14),
        "macd": macd(closes),
        "atr14": atr(candles, 14),
        "bollinger": bollinger(closes, 20, 2.0),
        "stoch": stochastic(candles, 14, 3, 3),
        "adx14": adx(candles, 14),
        "fib": fibonacci(candles, 50),
        "ema9": ema(closes, 9),
        "ema21": ema(closes, 21),
        "ema50": ema(closes, 50),
        "close": closes[-1] if closes else None,
    }
