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

Data, all cache-first (no request flood, no new endpoint). Best source first, then a source that does not
depend on GeckoTerminal (which sits in a 429 cooldown most of the time on Render):
  15s bars   built from the desk's own price ticks and the Jupiter price tape, no HTTP.
             Ticks carry no per-bar volume (they only hold the rolling 5m volume), so the
             15s series feeds RSI only. A breakout needs real volume, so it reads 1m only.
  1m bars    GeckoTerminal OHLCV through dip_recovery.load_candles: the same chart cache,
             retry gap, host cooldown and bucket the dip reader uses. Minutes with no
             trades become flat, zero-volume bars so a silent coin reads as quiet.

  1m tape    price-only 1m bars from the price tape (desk/pricetape.py). RSI uses them when GeckoTerminal has
             no usable candles. A breakout then takes resistance from the tape and the volume multiple
             from Jupiter (5m volume vs the hour's average 5m volume), via desk/marketdata.py.

Skip reasons: no_trigger (data was readable, nothing fired) and trigger_unknown:<cause> (neither
trigger could be computed; the cause is e.g. gt_cooling, gt_budget, no_pool, gt_404, too_few_bars).
Missing data is never a pass. `evaluate()` never raises.
Live trading never reads any of this (the agent calls it only when mode != "live").
"""
from __future__ import annotations

import logging
import time

from config import RULEBOOK
from desk import chart, dip_recovery, indicators, pricetape

log = logging.getLogger("desk.trigger")

RSI_OVERSOLD, BREAKOUT = "rsi_oversold", "breakout_2x_vol"
SKIP_NONE, SKIP_UNKNOWN = "no_trigger", "trigger_unknown"
FIRE, NO, UNKNOWN = "fire", "no", "unknown"

MIN_REAL_BARS = 6              # fewer real 1m candles than this -> unknown
MIN_PRIOR_BARS = 8             # a breakout needs at least this many prior minutes
MIN_REAL_TAPE = 8              # price-tape bars: at least this many minutes with a real price
TAPE_MAX_AGE_S = 150.0         # the newest tape price must be this fresh
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
    """(bars, real candle count, None), or (None, 0, cause) when the data is missing, stale or thin."""
    if not candles or fetched_at is None:
        return None, 0, "no_candles"
    if now - fetched_at > MAX_DATA_AGE_S:
        return None, 0, "stale_candles"
    end_s = min(now, fetched_at + 60.0)
    bars, real = dip_recovery._bars_per_minute(candles, end_s, span_min * 60.0)
    if real < MIN_REAL_BARS:
        return None, real, "too_few_bars"
    return bars, real, None


def _tape_bars_1m(mint: str, now: float, span_min: int):
    """Price-only 1m bars from the price tape (Jupiter prices + the desk's quote ticks): the second
    source when GeckoTerminal has no usable candles. (bars, real minutes, None) or (None, n, cause)."""
    cs = pricetape.candles_1m(mint)
    if not cs:
        return None, 0, "too_few_bars"
    if now - cs[-1]["t"] > TAPE_MAX_AGE_S:
        return None, 0, "stale_ticks"
    bars, real = dip_recovery._bars_per_minute(cs, now, span_min * 60.0)
    if real < MIN_REAL_TAPE:
        return None, real, "too_few_bars"
    return bars, real, None


class _Sources:
    """The 1m data for one coin, loaded at most once per evaluation.

    gt()    GeckoTerminal 1m candles (OHLCV + volume) through the dip reader's cache and limits
    tape()  price-only 1m bars from the price tape
    A miss carries a cause such as gt_cooling, gt_budget, no_pool, gt_404, too_few_bars."""

    def __init__(self, mint, now, session, fetch):
        self.mint, self.now, self.session, self.fetch = mint, now, session, fetch
        self._gt = self._tape = None

    def gt(self, span_min: int):
        if self._gt is None:
            try:
                candles, at = _load_1m(self.mint, self.session, self.now, self.fetch)
            except Exception as e:
                log.warning("trigger candle read failed: %s", type(e).__name__)
                candles, at = [], None
            self._gt = (candles, at)
        candles, at = self._gt
        bars, real, err = _bars_1m(candles, at, self.now, span_min)
        if bars is None and err == "no_candles":
            try:
                err = dip_recovery.candle_cause(self.mint, self.now)
            except Exception:
                err = "gt_unavailable"
        return bars, real, err

    def tape(self, span_min: int):
        return _tape_bars_1m(self.mint, self.now, span_min)


def _join(*causes) -> str:
    return "+".join(dict.fromkeys(c for c in causes if c))


# ---------- trigger A: RSI oversold ----------
def _rsi_from_closes(closes, period: int):
    if len(closes) < period + 1:
        return None
    v = indicators.rsi(closes, period)
    return None if v is None else round(v, 2)


def _rsi_15s(mint: str, period: int, now: float):
    bars = chart.bucket_ticks(pricetape.merged_ticks(mint), TICK_BAR_S)
    if len(bars) < period + 1:
        return None, "too_few_bars"
    if now - bars[-1]["t"] > TICK_MAX_AGE_S:
        return None, "stale_ticks"
    window = bars[-(3 * period + 1):]
    if now - window[-min(len(window), period + 1)]["t"] > TICK_MAX_SPAN_S:
        return None, "stale_ticks"
    return [b["c"] for b in window], None


def read_rsi(mint: str, now: float, session=None, fetch: bool = True, src: "_Sources | None" = None) -> dict:
    """RSI on the configured short timeframe, else the other sources. `unknown` if none works.

    Order for "15s": 15s bars from price ticks, then 1m GeckoTerminal candles, then 1m price-tape bars.
    Order for "1m":  1m GeckoTerminal candles, then 1m price-tape bars, then 15s bars from price ticks."""
    period = max(2, int(_cfg("rsi_period", 14)))
    want = str(RULEBOOK.get("rsi_timeframe") or "15s").lower()
    order = (["1m", "tape", "15s"] if want == "1m" else ["15s", "1m", "tape"])
    src = src or _Sources(mint, now, session, fetch)
    span = 3 * period + 2
    why: dict[str, str] = {}
    for step in order:
        if step == "15s":
            closes, err = _rsi_15s(mint, period, now)
            tf, source = "15s", "ticks"
        elif step == "1m":
            bars, _real, err = src.gt(span)
            closes = None if bars is None else [b["c"] for b in bars]
            tf, source = "1m", "geckoterminal"
        else:
            bars, _real, err = src.tape(span)
            closes = None if bars is None else [b["c"] for b in bars]
            tf, source = "1m", "price_tape"
        if closes is None:
            why[step] = err
            continue
        v = _rsi_from_closes(closes, period)
        if v is None:
            why[step] = "too_few_bars"
            continue
        turn = len(closes) >= 2 and closes[-1] > closes[-2]
        return {"state": UNKNOWN, "rsi": v, "tf": tf, "source": source, "period": period, "turn": turn,
                "bars": len(closes), "fell_back": step != order[0]}
    # the cause: the GeckoTerminal miss first (the usual root), then why the ticks / tape were thin
    cause = _join(why.get("1m"), why.get("tape") or why.get("15s"))
    return {"state": UNKNOWN, "rsi": None, "period": period, "tf": None, "cause": cause or "no_data",
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
def _breakout_from(bars, n: int, mult_min: float, vol_mult, source: str, extra: dict | None = None):
    prior, last = bars[-n - 1:-1], bars[-1]
    if len(prior) < MIN_PRIOR_BARS:
        return {"state": UNKNOWN, "cause": "too_few_bars", "detail": "insufficient_candles",
                "tf": "1m", "bars": len(bars), "source": source}
    resistance = max(b["h"] for b in prior)
    info = {"tf": "1m", "source": source, "resistance": resistance, "close": last["c"],
            "lookback": len(prior), "above": last["c"] > resistance, **(extra or {})}
    if vol_mult is None:
        return {**info, "state": UNKNOWN, "cause": info.get("vol_cause") or "no_volume", "detail": "no_volume_baseline"}
    info["vol_mult"] = vol_mult
    if last["c"] > resistance and vol_mult >= mult_min:
        return {**info, "state": FIRE, "detail": "broke_resistance_on_volume"}
    return {**info, "state": NO,
            "detail": "below_resistance" if last["c"] <= resistance else "weak_volume"}


def read_breakout(mint: str, now: float, session=None, fetch: bool = True,
                  src: "_Sources | None" = None) -> dict:
    """Close above the prior-N-minute high on >= breakout_vol_mult x average volume.

    First choice: GeckoTerminal 1m candles (per-minute volume). Second choice, when those are not
    available: resistance from price-tape 1m bars and the volume multiple from Jupiter (5m volume vs
    the hour's average 5m volume). Neither: unknown, with the cause."""
    n = max(3, int(_cfg("breakout_lookback", 20)))
    mult_min = _cfg("breakout_vol_mult", 2.0)
    src = src or _Sources(mint, now, session, fetch)
    bars, _real, gt_err = src.gt(n + 2)
    if bars is not None:
        prior = bars[-n - 1:-1]
        avg_v = sum(b["v"] for b in prior) / len(prior) if prior else 0.0
        if len(prior) >= MIN_PRIOR_BARS and avg_v <= 0:
            return {"state": UNKNOWN, "cause": "no_volume", "detail": "no_volume_baseline", "tf": "1m",
                    "bars": len(bars), "source": "geckoterminal"}
        mult = round(bars[-1]["v"] / avg_v, 2) if avg_v > 0 else None
        out = _breakout_from(bars, n, mult_min, mult, "geckoterminal")
        if out["state"] != UNKNOWN:
            return out
        gt_err = out.get("cause") or gt_err
    tbars, _treal, t_err = src.tape(n + 2)
    if tbars is None:
        return {"state": UNKNOWN, "cause": _join(gt_err, t_err), "detail": f"gt:{gt_err},tape:{t_err}",
                "tf": "1m"}
    from desk import marketdata, scanner
    jc = marketdata.ensure_jup(mint, session, now, fetch)
    ratio, jcause = (scanner.jup_volume_ratio(mint, now) if jc is None else (None, jc))
    out = _breakout_from(tbars, n, mult_min, ratio, "price_tape+jupiter", {"vol_cause": jcause})
    if out["state"] == UNKNOWN:
        out["cause"] = _join(gt_err, out.get("cause"))
    return out


# ---------- combined ----------
def evaluate(mint: str, *, now: float | None = None, session=None, fetch: bool = True) -> dict:
    """Both triggers for one coin. Never raises; any failure reads as unknown."""
    now = time.time() if now is None else now
    try:
        src = _Sources(mint, now, session, fetch)
        rsi = judge_rsi(read_rsi(mint, now, session, fetch, src))
        brk = read_breakout(mint, now, session, fetch, src)
    except Exception as e:
        log.warning("trigger read failed: %s", type(e).__name__)
        rsi = {"state": UNKNOWN, "detail": "error", "cause": "error", "rsi": None}
        brk = {"state": UNKNOWN, "detail": "error", "cause": "error"}
    fired = RSI_OVERSOLD if rsi["state"] == FIRE else BREAKOUT if brk["state"] == FIRE else None
    cause = None
    if fired:
        reason = None
    elif NO in (rsi["state"], brk["state"]):
        reason = SKIP_NONE
    else:
        reason = SKIP_UNKNOWN
        cause = _join(rsi.get("cause"), brk.get("cause")) or "no_data"
    return {"fired": fired, "reason": reason, "cause": cause, "rsi": rsi, "breakout": brk,
            "rsi_value": rsi.get("rsi"), "rsi_tf": rsi.get("tf"), "rsi_period": rsi.get("period"),
            "rsi_source": rsi.get("source"), "vol_mult": brk.get("vol_mult"),
            "vol_source": brk.get("source")}


def gate(info: dict | None) -> str | None:
    """no_trigger / trigger_unknown when the gate is ON and nothing fired, else None."""
    if not RULEBOOK.get("entry_trigger_gate", True):
        return None
    if info is None:
        return SKIP_UNKNOWN
    if info.get("fired"):
        return None
    reason = info.get("reason") or SKIP_UNKNOWN
    # trigger_unknown:<cause> (gt_cooling, gt_budget, no_pool, gt_404, too_few_bars ...): the base
    # reason stays the prefix so counters and the bench keep working
    return f"{reason}:{info['cause']}" if reason == SKIP_UNKNOWN and info.get("cause") else reason


def tag(info: dict | None) -> dict:
    """The fields stamped on the buy event, journal row, position and decision."""
    i = info or {}
    return {"trigger": i.get("fired"), "rsi": i.get("rsi_value"), "rsi_tf": i.get("rsi_tf"),
            "rsi_source": i.get("rsi_source"), "vol_mult": i.get("vol_mult"),
            "vol_source": i.get("vol_source")}


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
    elif r.get("cause"):
        bits.append(f"rsi n/a ({r['cause']})")
    if b.get("vol_mult") is not None:
        bits.append(f"vol {b['vol_mult']:g}x" + (" above resistance" if b.get("above") else ", under resistance"))
    elif b.get("cause"):
        bits.append(f"breakout n/a ({b['cause']})")
    return ", ".join(bits)
