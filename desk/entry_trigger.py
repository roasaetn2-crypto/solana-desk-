"""Entry-trigger gate (PAPER only).

Romz: "Enter trades when the RSI indicates oversold conditions (below 30) on short
timeframes, or when price breaks above resistance with 2x average volume."

A candidate must fire at least ONE trigger, on top of every other gate:

  rsi_oversold     RSI(rulebook.rsi_period) is below rulebook.rsi_oversold (30). With
                   rulebook.rsi_require_turn (ON) the latest bar must also close above
                   the bar before it (RSI turning up, not a knife still falling).
                   Timeframe rulebook.rsi_timeframe (default 15s), falling back to 1m.
  breakout_2x_vol  the latest 1m close is above the highest high of the prior
                   rulebook.breakout_lookback (20) minutes AND that candle's volume is at
                   least rulebook.breakout_vol_mult (2.0) x the average volume of those minutes.

Data, all cache-first (no request flood, no new endpoint):
  15s bars   built from the desk's own price ticks (chart.ticks / bucket_ticks), no HTTP.
             Ticks carry no per-bar volume (they only hold the rolling 5m volume), so the
             15s series feeds RSI only. A breakout needs real volume, so it reads 1m only.
  1m bars    GeckoTerminal OHLCV through dip_recovery.load_candles: the same chart cache,
             retry gap, host cooldown and bucket the dip reader uses. Minutes with no
             trades become flat, zero-volume bars so a silent coin reads as quiet.

Skip reasons: no_trigger (data was readable, nothing fired) and trigger_unknown (neither
trigger could be computed). Missing data is never a pass. `evaluate()` never raises.
Live trading never reads any of this (the agent calls it only when mode != "live").
"""
from __future__ import annotations

import logging
import time

from config import RULEBOOK
from desk import chart, dip_recovery, indicators

log = logging.getLogger("desk.trigger")

RSI_OVERSOLD, BREAKOUT = "rsi_oversold", "breakout_2x_vol"
SKIP_NONE, SKIP_UNKNOWN = "no_trigger", "trigger_unknown"
FIRE, NO, UNKNOWN = "fire", "no", "unknown"

MIN_REAL_BARS = 6              # fewer real 1m candles than this -> unknown
MIN_PRIOR_BARS = 8             # a breakout needs at least this many prior minutes
MAX_DATA_AGE_S = 300.0         # 1m candles fetched longer ago than this are stale
TICK_BAR_S = 15
TICK_MAX_AGE_S = 75.0          # the newest 15s bar must be this fresh
TICK_MAX_SPAN_S = 30 * 60.0    # the RSI window may not reach back further than this


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _cfg(key: str, default: float) -> float:
    v = _num(RULEBOOK.get(key))
    return default if v is None else v


def _load_1m(mint: str, session, now: float, fetch: bool):
    """(1m candles, fetched_at). fetch=False reads the chart cache only (no request)."""
    if fetch or chart._fetch_ohlcv is not None:
        return dip_recovery.load_candles(mint, session, now)
    if str(mint).startswith("DEMO"):
        return [], None
    pool = chart._pool_address(mint)
    row = chart._OHLCV.get(chart._ohlcv_key(pool, "1m")) if pool else None
    return (row["candles"], row["at"]) if row else ([], None)


def _bars_1m(candles, fetched_at, now: float, span_min: int):
    """(bars, real candle count), or (None, reason) when the data is stale or thin."""
    if not candles or fetched_at is None:
        return None, "no_candles"
    if now - fetched_at > MAX_DATA_AGE_S:
        return None, "stale_candles"
    end_s = min(now, fetched_at + 60.0)
    bars, real = dip_recovery._bars_per_minute(candles, end_s, span_min * 60.0)
    if real < MIN_REAL_BARS:
        return None, "insufficient_candles"
    return bars, real


# ---------- trigger A: RSI oversold ----------
def _rsi_from_closes(closes, period: int):
    if len(closes) < period + 1:
        return None
    v = indicators.rsi(closes, period)
    return None if v is None else round(v, 2)


def _rsi_15s(mint: str, period: int, now: float):
    bars = chart.bucket_ticks(chart.ticks(mint), TICK_BAR_S)
    if len(bars) < period + 1:
        return None, "thin_ticks"
    if now - bars[-1]["t"] > TICK_MAX_AGE_S:
        return None, "stale_ticks"
    window = bars[-(3 * period + 1):]
    if now - window[-min(len(window), period + 1)]["t"] > TICK_MAX_SPAN_S:
        return None, "stale_ticks"
    return [b["c"] for b in window], None


def read_rsi(mint: str, now: float, session=None, fetch: bool = True, c1m=None) -> dict:
    """RSI on the configured short timeframe, else the other one. `unknown` if neither works."""
    period = max(2, int(_cfg("rsi_period", 14)))
    want = str(RULEBOOK.get("rsi_timeframe") or "15s").lower()
    order = ["1m", "15s"] if want == "1m" else ["15s", "1m"]
    why = {}
    for tf in order:
        if tf == "15s":
            closes, err = _rsi_15s(mint, period, now)
        else:
            candles, at = c1m if c1m is not None else _load_1m(mint, session, now, fetch)
            bars, err = _bars_1m(candles, at, now, 3 * period + 2)
            closes = None if bars is None else [b["c"] for b in bars]
        if closes is None:
            why[tf] = err
            continue
        v = _rsi_from_closes(closes, period)
        if v is None:
            why[tf] = "too_few_bars"
            continue
        turn = len(closes) >= 2 and closes[-1] > closes[-2]
        return {"state": UNKNOWN, "rsi": v, "tf": tf, "period": period, "turn": turn,
                "bars": len(closes), "fell_back": tf != order[0]}
    return {"state": UNKNOWN, "rsi": None, "period": period, "tf": None,
            "detail": ",".join(f"{k}:{v}" for k, v in why.items())}


def judge_rsi(r: dict) -> dict:
    if r.get("rsi") is None:
        return {**r, "state": UNKNOWN}
    below = r["rsi"] < _cfg("rsi_oversold", 30.0)
    need_turn = bool(RULEBOOK.get("rsi_require_turn", True))
    if below and (r["turn"] or not need_turn):
        return {**r, "state": FIRE, "detail": "oversold_turning_up" if need_turn else "oversold"}
    return {**r, "state": NO, "detail": "no_turn_yet" if below else "not_oversold"}


# ---------- trigger B: breakout above resistance on 2x volume ----------
def read_breakout(mint: str, now: float, session=None, fetch: bool = True, c1m=None) -> dict:
    n = max(3, int(_cfg("breakout_lookback", 20)))
    mult_min = _cfg("breakout_vol_mult", 2.0)
    candles, at = c1m if c1m is not None else _load_1m(mint, session, now, fetch)
    bars, err = _bars_1m(candles, at, now, n + 2)
    if bars is None:
        return {"state": UNKNOWN, "detail": err, "tf": "1m"}
    prior, last = bars[-n - 1:-1], bars[-1]
    if len(prior) < MIN_PRIOR_BARS:
        return {"state": UNKNOWN, "detail": "insufficient_candles", "tf": "1m", "bars": len(bars)}
    resistance = max(b["h"] for b in prior)
    avg_v = sum(b["v"] for b in prior) / len(prior)
    if avg_v <= 0:
        return {"state": UNKNOWN, "detail": "no_volume_baseline", "tf": "1m", "bars": len(bars)}
    mult = round(last["v"] / avg_v, 2)
    info = {"tf": "1m", "resistance": resistance, "close": last["c"], "vol_mult": mult,
            "lookback": len(prior), "above": last["c"] > resistance}
    if last["c"] > resistance and mult >= mult_min:
        return {**info, "state": FIRE, "detail": "broke_resistance_on_volume"}
    return {**info, "state": NO,
            "detail": "below_resistance" if last["c"] <= resistance else "weak_volume"}


# ---------- combined ----------
def evaluate(mint: str, *, now: float | None = None, session=None, fetch: bool = True) -> dict:
    """Both triggers for one coin. Never raises; any failure reads as unknown."""
    now = time.time() if now is None else now
    try:
        c1m = None
        try:
            c1m = _load_1m(mint, session, now, fetch)
        except Exception as e:
            log.warning("trigger candle read failed: %s", type(e).__name__)
            c1m = ([], None)
        rsi = judge_rsi(read_rsi(mint, now, session, fetch, c1m))
        brk = read_breakout(mint, now, session, fetch, c1m)
    except Exception as e:
        log.warning("trigger read failed: %s", type(e).__name__)
        rsi, brk = {"state": UNKNOWN, "detail": "error", "rsi": None}, {"state": UNKNOWN, "detail": "error"}
    fired = RSI_OVERSOLD if rsi["state"] == FIRE else BREAKOUT if brk["state"] == FIRE else None
    if fired:
        reason = None
    elif NO in (rsi["state"], brk["state"]):
        reason = SKIP_NONE
    else:
        reason = SKIP_UNKNOWN
    return {"fired": fired, "reason": reason, "rsi": rsi, "breakout": brk,
            "rsi_value": rsi.get("rsi"), "rsi_tf": rsi.get("tf"), "rsi_period": rsi.get("period"),
            "vol_mult": brk.get("vol_mult")}


def gate(info: dict | None) -> str | None:
    """no_trigger / trigger_unknown when the gate is ON and nothing fired, else None."""
    if not RULEBOOK.get("entry_trigger_gate", True):
        return None
    if info is None:
        return SKIP_UNKNOWN
    if info.get("fired"):
        return None
    return info.get("reason") or SKIP_UNKNOWN


def tag(info: dict | None) -> dict:
    """The fields stamped on the buy event, journal row, position and decision."""
    i = info or {}
    out = {"trigger": i.get("fired"), "rsi": i.get("rsi_value"), "rsi_tf": i.get("rsi_tf"),
           "vol_mult": i.get("vol_mult")}
    return out


def facts(info: dict | None) -> dict | None:
    """Analyst prompt facts: the trigger, rsi{period} and the volume multiple."""
    if not info:
        return None
    p = info.get("rsi_period") or int(_cfg("rsi_period", 14))
    return {"trigger": info.get("fired"), f"rsi{p}": info.get("rsi_value"),
            "rsi_timeframe": info.get("rsi_tf"), "volume_vs_avg": info.get("vol_mult"),
            "breakout_above_resistance": (info.get("breakout") or {}).get("above")}


def describe(info: dict | None) -> str:
    i = info or {}
    r, b = i.get("rsi") or {}, i.get("breakout") or {}
    bits = [i.get("fired") or i.get("reason") or "unknown"]
    if r.get("rsi") is not None:
        bits.append(f"rsi{r.get('period')} {r['rsi']:g} @{r.get('tf')}"
                    + ("" if r.get("turn") else " (no up-turn)"))
    elif r.get("detail"):
        bits.append(f"rsi n/a ({r['detail']})")
    if b.get("vol_mult") is not None:
        bits.append(f"vol {b['vol_mult']:g}x" + (" above resistance" if b.get("above") else ", under resistance"))
    elif b.get("detail"):
        bits.append(f"breakout n/a ({b['detail']})")
    return ", ".join(bits)
