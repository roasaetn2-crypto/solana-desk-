"""Entry-trigger gate (PAPER only).

Romz: "Enter trades when the RSI indicates oversold conditions (below 30) on short
timeframes, or when price breaks above resistance with 2x average volume."

A candidate must fire at least ONE enabled trigger, on top of every other gate (each trigger has its
own on/off setting; with all four off the gate has nothing to require and passes):

  rsi_oversold     RSI(rulebook.rsi_period) is below rulebook.rsi_oversold (30). With
                   rulebook.rsi_require_turn (ON) the latest bar must also close above
                   the bar before it (RSI turning up, not a knife still falling).
                   Timeframe rulebook.rsi_timeframe (default 15s), falling back to 1m.
  breakout_2x_vol  the latest 1m close is above the highest high of the prior
                   rulebook.breakout_lookback (20) minutes AND that candle's volume is at
                   least rulebook.breakout_vol_mult (2.0) x the average volume of those minutes.

  vwap_reclaim     (C) the prior close was below the session VWAP, the latest close is above it and the
                   latest 1m bar closed up. Window = min(pool age, rulebook.vwap_lookback_min). With GeckoTerminal
                   volume it is a true VWAP (vwap_kind "vwap"); on the volume-less price tape it is a plain
                   average of the closes, labelled vwap_kind "price_avg" everywhere it shows up.
  ema_cross        (D) EMA(rulebook.ema_fast) crossed above EMA(rulebook.ema_slow) within the last 2 bars and
                   the close is above both. Timeframe rulebook.ema_timeframe ("1m" or "15s", same sources as RSI).

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
VWAP_RECLAIM, EMA_CROSS = "vwap_reclaim", "ema_cross"
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


def _ticks_15s(mint: str, need: int, keep: int, now: float):
    """Closes of the last `keep` 15s bars built from price ticks, or (None, cause)."""
    bars = chart.bucket_ticks(pricetape.merged_ticks(mint), TICK_BAR_S)
    if len(bars) < need:
        return None, "too_few_bars"
    if now - bars[-1]["t"] > TICK_MAX_AGE_S:
        return None, "stale_ticks"
    window = bars[-keep:]
    if now - window[-min(len(window), need)]["t"] > TICK_MAX_SPAN_S:
        return None, "stale_ticks"
    return [b["c"] for b in window], None


def _closes(mint: str, now: float, want: str, need: int, keep: int, span: int, src: "_Sources"):
    """Closes on the short timeframe `want` ("15s" or "1m"), best source first, then the others.

    Order for "15s": 15s bars from price ticks, then 1m GeckoTerminal candles, then 1m price-tape bars.
    Order for "1m":  1m GeckoTerminal candles, then 1m price-tape bars, then 15s bars from price ticks.
    Returns (closes, meta, why): meta is {"tf", "source", "fell_back"}; closes is None when no source
    had `need` bars, and why maps each source tried to its cause."""
    order = ["1m", "tape", "15s"] if str(want).lower() == "1m" else ["15s", "1m", "tape"]
    why: dict[str, str] = {}
    for step in order:
        if step == "15s":
            closes, err = _ticks_15s(mint, need, keep, now)
            tf, source = "15s", "ticks"
        else:
            bars, _real, err = src.gt(span) if step == "1m" else src.tape(span)
            closes = None
            if bars is not None:
                if len(bars) >= need:
                    closes = [b["c"] for b in bars[-keep:]]
                else:
                    err = "too_few_bars"
            tf, source = "1m", ("geckoterminal" if step == "1m" else "price_tape")
        if closes is None:
            why[step] = err
            continue
        return closes, {"tf": tf, "source": source, "fell_back": step != order[0]}, why
    return None, None, why


def _miss_cause(why: dict) -> str:
    # the GeckoTerminal miss first (the usual root), then why the ticks / tape were thin
    return _join(why.get("1m"), why.get("tape") or why.get("15s")) or "no_data"


def read_rsi(mint: str, now: float, session=None, fetch: bool = True, src: "_Sources | None" = None) -> dict:
    """RSI on the configured short timeframe, else the other sources. `unknown` if none works."""
    period = max(2, int(_cfg("rsi_period", 14)))
    src = src or _Sources(mint, now, session, fetch)
    closes, meta, why = _closes(mint, now, RULEBOOK.get("rsi_timeframe") or "15s",
                                period + 1, 3 * period + 1, 3 * period + 2, src)
    v = _rsi_from_closes(closes, period) if closes else None
    if v is None:
        return {"state": UNKNOWN, "rsi": None, "period": period, "tf": None,
                "cause": _miss_cause(why), "detail": ",".join(f"{k}:{x}" for k, x in why.items())}
    return {"state": UNKNOWN, "rsi": v, "tf": meta["tf"], "source": meta["source"], "period": period,
            "turn": len(closes) >= 2 and closes[-1] > closes[-2], "bars": len(closes),
            "fell_back": meta["fell_back"]}


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


# ---------- trigger C: VWAP reclaim ----------
MIN_VWAP_BARS = 6
VWAP, PRICE_AVG = "vwap", "price_avg"


def _running_avg(bars, kind: str):
    """Per-bar running VWAP (volume-weighted typical price) or, with no volume, the plain mean of closes."""
    out, pv, vv, cs, n = [], 0.0, 0.0, 0.0, 0
    for b in bars:
        n += 1
        cs += b["c"]
        pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
        vv += b["v"]
        out.append((pv / vv if vv > 0 else None) if kind == VWAP else cs / n)
    return out


def read_vwap(mint: str, now: float, session=None, fetch: bool = True,
              src: "_Sources | None" = None, age_min=None) -> dict:
    """Session VWAP reclaim on 1m bars over min(pool age, rulebook.vwap_lookback_min).

    Fires when the prior close was below VWAP, the latest close is above it and the latest bar closed up.
    kind "vwap": GeckoTerminal candles that carry volume (typical price weighted by volume).
    kind "price_avg": the tape has no per-bar volume, so the level is the plain mean of the minute closes
    (a time-weighted average price). It is a weaker level than a VWAP and is always labelled as such in
    the tags, logs and analyst facts. Neither source: unknown, with the cause."""
    win = max(5, int(_cfg("vwap_lookback_min", 60)))
    age = _num(age_min)
    if age is not None:
        win = max(1, min(win, int(age)))
    src = src or _Sources(mint, now, session, fetch)
    bars, _real, gt_err = src.gt(win + 2)
    kind, source = VWAP, "geckoterminal"
    if bars is not None:
        bars = bars[-(win + 1):]
        if sum(b["v"] for b in bars) <= 0:
            bars, gt_err = None, "no_volume"
    if bars is None:
        bars, _treal, t_err = src.tape(win + 2)
        if bars is None:
            return {"state": UNKNOWN, "cause": _join(gt_err, t_err), "detail": f"gt:{gt_err},tape:{t_err}",
                    "tf": "1m"}
        bars = bars[-(win + 1):]
        kind, source = PRICE_AVG, "price_tape"
    if len(bars) < MIN_VWAP_BARS:
        return {"state": UNKNOWN, "cause": "too_few_bars", "detail": "insufficient_candles", "tf": "1m",
                "vwap_kind": kind, "source": source, "bars": len(bars), "window_min": win}
    lvl = _running_avg(bars, kind)
    now_lvl, prev_lvl = lvl[-1], lvl[-2]
    if now_lvl is None or prev_lvl is None:
        return {"state": UNKNOWN, "cause": "no_volume", "detail": "no_volume_yet", "tf": "1m",
                "vwap_kind": kind, "source": source, "bars": len(bars), "window_min": win}
    last, prev = bars[-1], bars[-2]
    up = last["c"] > last["o"] if kind == VWAP else last["c"] > prev["c"]
    info = {"tf": "1m", "vwap": now_lvl, "vwap_kind": kind, "source": source, "close": last["c"],
            "prior_close": prev["c"], "bars": len(bars), "window_min": win}
    if prev["c"] < prev_lvl and last["c"] > now_lvl and up:
        return {**info, "state": FIRE, "detail": f"reclaimed_{kind}"}
    why = ("no_up_close" if (prev["c"] < prev_lvl and last["c"] > now_lvl)
           else "was_not_below" if prev["c"] >= prev_lvl else "still_below")
    return {**info, "state": NO, "detail": why}


# ---------- trigger D: EMA cross ----------
def read_ema(mint: str, now: float, session=None, fetch: bool = True, src: "_Sources | None" = None) -> dict:
    """EMA(fast) crossed above EMA(slow) within the last 2 bars, and the close is above both.
    Timeframe rulebook.ema_timeframe ("1m" or "15s"), with the same source order as the RSI."""
    fast, slow = int(_cfg("ema_fast", 9)), int(_cfg("ema_slow", 21))
    if fast >= slow:
        return {"state": UNKNOWN, "cause": "bad_ema_periods", "detail": "fast_not_below_slow"}
    src = src or _Sources(mint, now, session, fetch)
    closes, meta, why = _closes(mint, now, RULEBOOK.get("ema_timeframe") or "1m",
                                slow + 2, 3 * slow, 3 * slow + 2, src)
    if closes is None:
        return {"state": UNKNOWN, "cause": _miss_cause(why), "detail": ",".join(f"{k}:{x}" for k, x in why.items())}
    ef, es = indicators.ema_series(closes, fast), indicators.ema_series(closes, slow)
    diff = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    if any(d is None for d in diff[-3:]):
        return {"state": UNKNOWN, "cause": "too_few_bars", "detail": "ema_not_ready"}
    px = closes[-1]
    info = {"tf": meta["tf"], "source": meta["source"], "ema_fast": round(ef[-1], 12), "ema_slow": round(es[-1], 12),
            "ema_fast_n": fast, "ema_slow_n": slow, "close": px, "bars": len(closes)}
    crossed = (diff[-1] > 0 and (diff[-2] <= 0 or diff[-3] <= 0))
    if crossed and px > ef[-1] and px > es[-1]:
        return {**info, "state": FIRE, "detail": "fast_crossed_above_slow"}
    why_no = ("price_not_above_both" if crossed else "no_fresh_cross")
    return {**info, "state": NO, "detail": why_no}


# ---------- combined ----------
TRIGGERS = (          # priority order: which one is the primary tag when several fire
    ("rsi_trigger", RSI_OVERSOLD), ("breakout_trigger", BREAKOUT),
    ("vwap_trigger", VWAP_RECLAIM), ("ema_trigger", EMA_CROSS))
OFF = "off"
SHORT = {RSI_OVERSOLD: "rsi", BREAKOUT: "breakout", VWAP_RECLAIM: "vwap", EMA_CROSS: "ema"}   # stats suffix
CHIP = {RSI_OVERSOLD: "trigger_rsi", BREAKOUT: "trigger_breakout",
        VWAP_RECLAIM: "trigger_vwap", EMA_CROSS: "trigger_ema"}


def enabled(key: str) -> bool:
    return bool(RULEBOOK.get(key, True))


def evaluate(mint: str, *, now: float | None = None, session=None, fetch: bool = True,
             age_min=None) -> dict:
    """All enabled triggers for one coin. Never raises; any failure reads as unknown.

    Passes when ANY enabled trigger fires. no_trigger when at least one enabled trigger was computed and
    none fired; trigger_unknown only when no enabled trigger could be computed. With every trigger
    switched off there is nothing to require, so the gate passes (it behaves as if it were off)."""
    now = time.time() if now is None else now
    err = {"state": UNKNOWN, "detail": "error", "cause": "error"}
    res: dict[str, dict] = {}
    try:
        src = _Sources(mint, now, session, fetch)
        readers = {
            "rsi_trigger": lambda: judge_rsi(read_rsi(mint, now, session, fetch, src)),
            "breakout_trigger": lambda: read_breakout(mint, now, session, fetch, src),
            "vwap_trigger": lambda: read_vwap(mint, now, session, fetch, src, age_min),
            "ema_trigger": lambda: read_ema(mint, now, session, fetch, src),
        }
        for key, _name in TRIGGERS:
            if not enabled(key):
                res[key] = {"state": OFF}
                continue
            try:
                res[key] = readers[key]()
            except Exception as e:
                log.warning("trigger %s failed: %s", key, type(e).__name__)
                res[key] = dict(err)
    except Exception as e:
        log.warning("trigger read failed: %s", type(e).__name__)
        for key, _name in TRIGGERS:
            res[key] = {"state": OFF} if not enabled(key) else dict(err)
    rsi, brk, vwap, ema = (res[k] for k, _ in TRIGGERS)
    fired_all = [name for key, name in TRIGGERS if res[key]["state"] == FIRE]
    fired = fired_all[0] if fired_all else None
    live = [res[k]["state"] for k, _ in TRIGGERS if enabled(k)]
    cause, reason = None, None
    if fired or not live:
        pass
    elif NO in live:
        reason = SKIP_NONE
    else:
        reason = SKIP_UNKNOWN
        toks = dict.fromkeys(tok for k, _ in TRIGGERS if enabled(k)
                             for tok in str(res[k].get("cause") or "").split("+") if tok)
        cause = "+".join(list(toks)[:3]) or "no_data"
    return {"fired": fired, "fired_all": fired_all, "reason": reason, "cause": cause, "none_enabled": not live,
            "rsi": rsi, "breakout": brk, "vwap": vwap, "ema": ema,
            "rsi_value": rsi.get("rsi"), "rsi_tf": rsi.get("tf"), "rsi_period": rsi.get("period"),
            "rsi_source": rsi.get("source"), "vol_mult": brk.get("vol_mult"),
            "vol_source": brk.get("source"),
            "vwap_value": vwap.get("vwap"), "vwap_kind": vwap.get("vwap_kind") if vwap.get("vwap") is not None else None,
            "ema_fast_value": ema.get("ema_fast"), "ema_slow_value": ema.get("ema_slow"),
            "ema_tf": ema.get("tf") if ema.get("ema_fast") is not None else None}


def gate(info: dict | None) -> str | None:
    """no_trigger / trigger_unknown when the gate is ON and nothing fired, else None."""
    if not RULEBOOK.get("entry_trigger_gate", True):
        return None
    if info is None:
        return SKIP_UNKNOWN
    if info.get("fired") or info.get("none_enabled"):
        return None
    reason = info.get("reason") or SKIP_UNKNOWN
    # trigger_unknown:<cause> (gt_cooling, gt_budget, no_pool, gt_404, too_few_bars ...): the base
    # reason stays the prefix so counters and the bench keep working
    return f"{reason}:{info['cause']}" if reason == SKIP_UNKNOWN and info.get("cause") else reason


def tag(info: dict | None) -> dict:
    """The fields stamped on the buy event, journal row, position and decision. The values of
    every computed trigger ride along, with the name of the one that fired first as `trigger`."""
    i = info or {}
    return {"trigger": i.get("fired"), "triggers": list(i.get("fired_all") or []),
            "rsi": i.get("rsi_value"), "rsi_tf": i.get("rsi_tf"), "rsi_source": i.get("rsi_source"),
            "vol_mult": i.get("vol_mult"), "vol_source": i.get("vol_source"),
            "vwap": i.get("vwap_value"), "vwap_kind": i.get("vwap_kind"),
            "ema_fast": i.get("ema_fast_value"), "ema_slow": i.get("ema_slow_value"),
            "ema_tf": i.get("ema_tf")}


def facts(info: dict | None) -> dict | None:
    """Analyst prompt facts: the triggers that fired, rsi{period}, the volume multiple, the VWAP (with
    its kind: "price_avg" is a plain average, not volume weighted) and the EMA pair."""
    if not info:
        return None
    p = info.get("rsi_period") or int(_cfg("rsi_period", 14))
    out = {"trigger": info.get("fired"), "triggers_fired": list(info.get("fired_all") or []),
           f"rsi{p}": info.get("rsi_value"), "rsi_timeframe": info.get("rsi_tf"),
           "volume_vs_avg": info.get("vol_mult"),
           "breakout_above_resistance": (info.get("breakout") or {}).get("above")}
    if info.get("vwap_value") is not None:
        v = info.get("vwap") or {}
        out.update({"vwap": info["vwap_value"], "vwap_kind": info.get("vwap_kind"),
                    "price_vs_vwap": (round(v["close"] / info["vwap_value"] - 1, 4)
                                      if v.get("close") and info["vwap_value"] else None)})
    if info.get("ema_fast_value") is not None:
        e = info.get("ema") or {}
        out.update({"ema_fast": info["ema_fast_value"], "ema_slow": info["ema_slow_value"],
                    "ema_periods": f"{e.get('ema_fast_n')}/{e.get('ema_slow_n')}",
                    "ema_timeframe": info.get("ema_tf")})
    return out


def describe(info: dict | None) -> str:
    i = info or {}
    r, b, v, e = i.get("rsi") or {}, i.get("breakout") or {}, i.get("vwap") or {}, i.get("ema") or {}
    head = i.get("fired") or i.get("reason") or "unknown"
    if len(i.get("fired_all") or []) > 1:
        head += f" (+{','.join(x for x in i['fired_all'] if x != i['fired'])})"
    bits = [head]
    if r.get("state") != OFF:
        if r.get("rsi") is not None:
            bits.append(f"rsi{r.get('period')} {r['rsi']:g} @{r.get('tf')}"
                        + ("" if r.get("turn") else " (no up-turn)"))
        elif r.get("cause"):
            bits.append(f"rsi n/a ({r['cause']})")
    if b.get("state") != OFF:
        if b.get("vol_mult") is not None:
            bits.append(f"vol {b['vol_mult']:g}x" + (" above resistance" if b.get("above") else ", under resistance"))
        elif b.get("cause"):
            bits.append(f"breakout n/a ({b['cause']})")
    if v.get("state") != OFF:
        if v.get("vwap") is not None:
            side = "reclaimed" if v.get("state") == FIRE else v.get("detail", "")
            bits.append(f"{v.get('vwap_kind')} {v['vwap']:.6g} {side}")
        elif v.get("cause"):
            bits.append(f"vwap n/a ({v['cause']})")
    if e.get("state") != OFF:
        if e.get("ema_fast") is not None:
            bits.append(f"ema{e.get('ema_fast_n')}/{e.get('ema_slow_n')} {e['ema_fast']:.6g}/{e['ema_slow']:.6g} "
                        f"@{e.get('tf')} {e.get('detail', '')}")
        elif e.get("cause"):
            bits.append(f"ema n/a ({e['cause']})")
    return ", ".join(bits)
