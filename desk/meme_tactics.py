"""Memecoin entry tactics measured from the public "high-profit meme bot" guide.

Why this module exists
----------------------
Memecoins are hard to predict because their price is driven by attention, not
cash flow. The guide that prompted this work proposes four behavioural rules on
top of a classic indicator toolkit:

    A. ATR-scaled dynamic stop        (widen when wild, tighten when calm)
    B. pullback-from-high entry       (do not buy the top of the range)
    C. flash-pump circuit breaker     (do not chase a vertical candle)
    D. volatility ceiling             (skip coins that are simply too wild)

The indicator half of that guide (RSI / MACD / MA / Bollinger) was already
tested on real GeckoTerminal candles by ``scripts/indicator_eval.py`` and lost:
3,013 and 5,974 real trades, zero indicators cleared the bar.

The behavioural half was tested by ``scripts/meme_tactics_eval.py`` and
``scripts/meme_tactics_deep.py`` on 3,629 real trades from 60 pools:

    baseline (signal BUY)                  n=3629  win 15.4%  PF 0.503
    A. ATR stop (k=1.0..2.5)               n=3629  win 13.1-14.5%  -> worse
    C. flash-pump breaker (15-50%)         n=3511-3614  win ~15%   -> no change
    D. volatility ceiling (10-40%)         n=3429-3627  win ~15%   -> no change
    B. pullback 10% below 20-bar high      n=424   win 26.9%  PF 0.745
    B. pullback 20% below 20-bar high      n=145   win 33.1%  PF 0.949

Only B moved the win rate, but on its own it still lost money (PF < 1.2). The
deep sweep then paired B with a wider stop and a patient take-profit ladder:

    B(20%) + stop 30% + TP 2x/3x           n=145  win 30.3%  PF 1.587  ADD

That cleared the old bar, but a later, larger sweep (``scripts/winrate_lab.py``,
120 pools / 114,033 candles, 2026-11-14) found a strictly better pairing:

    B(20%) + stop 30% + fast ladder        n=74   pools=11  win 68.9%  PF 3.75

The fast ladder banks half at +25% and the rest at +60%, so most "popped then
reversed" trades finish green instead of bleeding out. It survives
leave-one-pool-out (worst 58.5% win / PF 2.71), so it is not one lucky pool. The
package now ships ON by default; the older 2x/3x ladder is retired.

Everything here is pure: no I/O, no clock, no network.
"""
from __future__ import annotations

import os

from config import MEME

# The exact package that cleared the bar in scripts/winrate_lab.py on 120 pools
# / 114,033 real candles (2026-11-14):
#
#   20% pullback + fast ladder + 30% stop
#       n=74  pools=11  win 68.9%  PF 3.75  avg +17.3%
#
# It survives leave-one-pool-out: dropping the most influential pool still leaves
# 58.5% win / PF 2.71 on the remaining 10. The earlier "patient 2x/3x" ladder
# (win 30.3%, PF 1.587) is superseded: banking half at +25% converts far more
# "popped then reversed" trades into wins, and the 20% dip keeps losers small
# enough that the fast ladder stays profitable.
PULLBACK_DROP = 0.20

# Named ladders. `fast` is the 2026-11-14 promotion (68.9% win on 120 pools that
# turned out to be ~4.5 years old, no overlap with the fresh launches the desk
# trades). `patient` is the 2x/3x geometry that preceded it.
#
# scripts/ladder_replay.py replays all three on real candles with entries held
# constant. Result (see LADDER-REPLAY.md): win rate is identical to within 1pt
# across every ladder at every stop width, but expectancy is NOT -- `patient`
# earns roughly 3x `fast` at a 1.5% stop and ~2.9x at 30%. The promotion bought
# no win rate and cost most of the expectancy.
#
# Default stays `fast` so this change alters no behaviour by itself. Flip it with
# MEME["exit_ladder"] = "patient" in config.py (or BRAIN_EXIT_LADDER=patient).
LADDERS = {
    "fast": {
        "stop_pct": 0.30,
        "tp1_at": 1.25, "tp1_frac": 0.5,
        "tp2_at": 1.6, "tp2_frac": 0.5,
        "trail_pct": 0.15,
    },
    "patient": {
        "stop_pct": 0.30,
        "tp1_at": 2.0, "tp1_frac": 0.33,
        "tp2_at": 3.0, "tp2_frac": 0.5,
        "trail_pct": 0.20,
    },
    "slow": {
        "stop_pct": 0.30,
        "tp1_at": 1.5, "tp1_frac": 0.33,
        "tp2_at": 2.0, "tp2_frac": 0.5,
        "trail_pct": 0.20,
    },
    "single": {
        "stop_pct": 0.30,
        "tp1_at": 1.35, "tp1_frac": 0.5,
        "tp2_at": 1e9, "tp2_frac": 0.0,
        "trail_pct": 0.18,
    },
}
DEFAULT_LADDER = "fast"


def ladder_info() -> dict:
    """The ladder in force *and* which source chose it.

    Precedence is env -> config -> default, which means a stale
    BRAIN_EXIT_LADDER silently outranks config.py. That is the single most
    common reason a config edit appears to do nothing, so the desk reports the
    winning source rather than just the value -- otherwise the only way to find
    out is to read this function.
    """
    env_raw = os.getenv("BRAIN_EXIT_LADDER", "")
    cfg_raw = MEME.get("exit_ladder", "")
    if str(env_raw).strip():
        source, raw = "env", env_raw
    elif str(cfg_raw).strip():
        source, raw = "config", cfg_raw
    else:
        source, raw = "default", DEFAULT_LADDER
    name = str(raw).strip().lower()
    known = name in LADDERS
    return {
        "name": name if known else DEFAULT_LADDER,
        "requested": name,
        "source": source,
        "valid": known,
        "fallback_used": not known,
        "known": sorted(LADDERS),
        "ladder": dict(LADDERS[name if known else DEFAULT_LADDER]),
    }


def active_ladder() -> dict:
    """The ladder in force, resolved from env then config, falling back to fast."""
    return ladder_info()["ladder"]


def current_ladder() -> str:
    """Name of the ladder actually in force right now (env -> config -> default)."""
    return ladder_info()["name"]


def set_ladder(name: str) -> str:
    """Operator-chosen ladder, applied live and persisted by desk.settings.

    BRAIN_EXIT_LADDER (env) outranks config.py, so a choice made in the dashboard
    must clear the env override or it would silently do nothing. This writes
    config.MEME["exit_ladder"] and drops the env var, then returns the name now
    in force. Raises ValueError on an unknown name so the settings layer can
    reject it cleanly instead of storing a typo.
    """
    n = str(name or "").strip().lower()
    if n not in LADDERS:
        raise ValueError(f"unknown ladder '{str(name)[:20]}'; pick one of {', '.join(sorted(LADDERS))}")
    MEME["exit_ladder"] = n
    os.environ.pop("BRAIN_EXIT_LADDER", None)
    return n


EXIT_OVERRIDES = dict(LADDERS[DEFAULT_LADDER])


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None          # reject NaN


def _bar(c) -> tuple[float, float, float, float, float] | None:
    """(open, high, low, close, volume) from either candle spelling, or None.

    desk/chart.py emits short keys (o/h/l/c/v) for every live series; the offline harness and
    the committed data use long ones. The live dip gate read only the long spelling, so
    `high_20` was never set and the gate silently never blocked a buy.
    """
    if not isinstance(c, dict):
        return None
    vals = [_num(c.get(a, c.get(b))) for a, b in
            (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"))]
    if any(v is None or v <= 0 for v in vals):
        return None
    vol = _num(c.get("vol", c.get("volume", c.get("v")))) or 0.0
    return vals[0], vals[1], vals[2], vals[3], vol


def bar_high(c) -> float | None:
    b = _bar(c)
    return b[1] if b else None


def scalp_entry_state(candles, mode: str | None = None, bars: int | None = None,
                      min_body: float | None = None, now: float | None = None,
                      max_stale_s: float = 600.0) -> dict:
    """The scalp lane's entry rule (SCALP["entry_mode"]) on 5m candles.

    The setting existed, the dashboard showed it and the Hyper Scalp preset set
    it, but no code ever read it: the desk bought whatever passed the other
    gates, including coins in the middle of a red 5m crash. This is the rule the
    preset and SCALPER.md describe, on the LAST bar (the candle still forming):

      moving_candle : green, body >= min_body of price, close above the prior
                      close and above the close `bars` bars back
      dip           : red and below the prior close (buy the shakeout)
      breakout      : close at or above the highest high of the prior `bars` bars

    ok=None (neutral, never a veto) when there are no usable bars: a 1-minute-old
    launch has no 5m history yet, and missing data must not starve the lane.
    With exactly one bar, moving_candle checks the bar itself (green + body).
    Also neutral when `now` is given and the last bar opened more than
    `max_stale_s` ago: a cached series from 15 minutes back is not "the candle
    still moving".
    """
    mode = str(mode or "moving_candle").lower()
    bars = max(1, int(bars or 1))
    min_body = float(min_body or 0.0)
    bs = [b for b in (_bar(c) for c in (candles or [])) if b]
    if not bs:
        return {"ok": None, "why": "no candles", "mode": mode}
    if now is not None:
        last = candles[-1] if isinstance(candles[-1], dict) else {}
        ts = _num(last.get("t", last.get("ts")))
        if ts and ts > 1e12:
            ts /= 1000.0
        if ts and now - ts > max_stale_s:
            return {"ok": None, "why": "stale candles", "mode": mode}
    o, h, l, c, _v = bs[-1]
    body = (c - o) / c if c else 0.0
    out = {"mode": mode, "body_pct": round(body * 100, 3), "n": len(bs)}
    if mode == "moving_candle":
        if c <= o:
            return {**out, "ok": False, "why": f"5m candle is red ({body * 100:+.1f}%)"}
        if body < min_body:
            return {**out, "ok": False, "why": f"5m candle body {body * 100:.2f}% under {min_body * 100:.2f}%"}
        if len(bs) >= 2 and c <= bs[-2][3]:
            return {**out, "ok": False, "why": "not above the prior 5m close"}
        if len(bs) > bars and c <= bs[-1 - bars][3]:
            return {**out, "ok": False, "why": f"not above the close {bars} bar(s) back"}
        return {**out, "ok": True, "why": "moving 5m candle"}
    if mode == "dip":
        if len(bs) < 2:
            return {**out, "ok": None, "why": "one bar: no prior close"}
        ok = c < o and c < bs[-2][3]
        return {**out, "ok": ok, "why": "red dip below prior close" if ok else "not a dip"}
    if mode == "breakout":
        if len(bs) <= bars:
            return {**out, "ok": None, "why": "too few bars for the lookback"}
        hi = max(b[1] for b in bs[-1 - bars:-1])
        ok = c >= hi
        return {**out, "ok": ok, "why": "fresh high" if ok else "below the lookback high"}
    return {**out, "ok": None, "why": f"unknown mode {mode}"}


def retest_state(candles, lookback: int | None = None, vol_mult: float | None = None,
                 zone: float | None = None, max_age: int | None = None) -> dict:
    """Breakout, then a retest of the broken level, then a bounce (the chart-pattern entry).

    resistance R = highest high of the `lookback` bars before a breakout bar. The breakout bar is
    green, closes above R, and trades >= vol_mult x the average volume of those bars. Within
    `max_age` bars after it, price must dip back to within `zone` of R (low <= R*(1+zone)) WITHOUT
    closing below R*(1-zone) (the level held), and the latest bar must be green and close above R.
    Missing or short data never passes: this can only ADD an entry, never create one from nothing.
    """
    L = int(MEME.get("retest_lookback", 20)) if lookback is None else int(lookback)
    vm = float(MEME.get("retest_volume_mult", 1.5)) if vol_mult is None else float(vol_mult)
    z = float(MEME.get("retest_zone_pct", 0.03)) if zone is None else float(zone)
    age = int(MEME.get("retest_max_age_bars", 12)) if max_age is None else int(max_age)
    bars = [b for b in (_bar(c) for c in candles or []) if b]
    n = len(bars)
    if n < L + 3:
        return {"ok": False, "why": "not enough bars"}
    last = bars[-1]
    for i in range(n - 2, max(L, n - 2 - age) - 1, -1):
        prior = bars[i - L:i]
        res = max(b[1] for b in prior)
        avg_v = sum(b[4] for b in prior) / L
        bo = bars[i]
        if avg_v <= 0 or not (bo[3] > res and bo[3] > bo[0] and bo[4] >= vm * avg_v):
            continue
        after = bars[i + 1:]
        if any(b[3] < res * (1 - z) for b in after):
            continue                                   # the broken level did not hold
        if not any(b[2] <= res * (1 + z) for b in after):
            continue                                   # never came back to retest it
        if last[3] > last[0] and last[3] > res:
            return {"ok": True, "resistance": res, "why": f"retest of broken resistance {res:.6g}"}
    return {"ok": False, "why": "no breakout-retest"}


def pullback_ok(price, high_20, min_drop: float | None = None) -> tuple[bool, str]:
    """Rule B. True when price is far enough below the recent 20-bar high.

    Unknown inputs are *neutral*, never a veto: the desk must not invent a
    rejection out of missing data.
    """
    p, hi = _num(price), _num(high_20)
    if p is None or hi is None or hi <= 0 or p <= 0:
        return True, "unknown"
    drop = float(MEME.get("pullback_min_drop", PULLBACK_DROP)) if min_drop is None else float(min_drop)
    if p > hi * (1.0 - drop):
        off = (1.0 - p / hi) * 100.0
        return False, f"at_high ({off:.1f}% below 20-bar high, need {drop*100:.0f}%)"
    return True, "pullback"


def flash_ok(change_5m, thr: float | None = None) -> tuple[bool, str]:
    """Rule C. False when the last 5m candle already ran more than ``thr``."""
    chg = _num(change_5m)
    if chg is None:
        return True, "unknown"
    limit = float(MEME.get("flash_pump_pct", 0.25)) if thr is None else float(thr)
    if chg > limit:
        return False, f"flash_pump (+{chg*100:.0f}% in 5m)"
    return True, ""


def atr_pct_from(candles: list[dict], period: int = 14) -> float | None:
    """ATR as a fraction of the last close, computed the way the eval did.

    ``scripts/meme_tactics_eval.py`` measured rules A and D on
    ``indicators.snapshot()["atr14"] / close`` with Wilder smoothing. This is
    the same number, written here so the live loop can produce it without
    importing the offline indicator library into the trading path.

    Returns None when there are fewer than ``period + 1`` usable bars, which is
    the honest answer for a coin that has not traded long enough to have an ATR.
    """
    # chart.py emits short keys (h/l/c); the offline indicator library uses
    # high/low/close. Accept both so this can be fed from either source.
    def _hlc(c):
        hi = c.get("h", c.get("high"))
        lo = c.get("l", c.get("low"))
        cl = c.get("c", c.get("close"))
        if hi is None or lo is None or cl is None:
            return None
        try:
            hi, lo, cl = float(hi), float(lo), float(cl)
        except (TypeError, ValueError):
            return None
        return (hi, lo, cl) if hi > 0 and lo > 0 and cl > 0 else None

    bars = [x for x in (_hlc(c) for c in (candles or []) if c) if x]
    if len(bars) < period + 1:
        return None
    trs = []
    for (_, _, pc), (hi, lo, _) in zip(bars, bars[1:]):
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    close = bars[-1][2]
    return (atr / close) if close > 0 else None


def atr_stop_pct(atr_pct, k: float | None = None) -> float | None:
    """Rule A. stop = clamp(k * ATR%, 8%, 60%)."""
    a = _num(atr_pct)
    if a is None or a <= 0:
        return None
    kk = float(MEME.get("atr_stop_k", 1.5)) if k is None else float(k)
    return max(0.08, min(0.60, kk * a))


def vol_ok(atr_pct, ceil: float | None = None) -> tuple[bool, str]:
    """Rule D. False when ATR% is above the ceiling."""
    a = _num(atr_pct)
    if a is None:
        return True, "unknown"
    limit = float(MEME.get("vol_ceiling_pct", 0.20)) if ceil is None else float(ceil)
    if a > limit:
        return False, f"too_volatile (ATR {a*100:.0f}% > {limit*100:.0f}%)"
    return True, ""


def flags() -> dict:
    """Which tactics the operator has switched on."""
    return {
        "pullback": bool(MEME.get("pullback_entry", False)),
        "retest": bool(MEME.get("retest_entry", False)),
        "flash": bool(MEME.get("flash_pump_guard", False)),
        "atr_stop": bool(MEME.get("atr_stop", False)),
        "vol_ceiling": bool(MEME.get("vol_ceiling", False)),
    }


def shadow_on() -> bool:
    return bool(MEME.get("pullback_shadow", False))


def shadow(token: dict | None = None, facts: dict | None = None) -> dict | None:
    """What the pullback package WOULD have decided. Never blocks anything.

    The package was measured on the *ungated* signal (15.4% baseline). The live
    desk trades a fully-gated population where the gates already lift the win
    rate to ~40%, so the +15pp may not survive the move. Rather than guess, the
    desk records the pullback verdict on every candidate it actually trades and
    compares the two groups once there are enough closed trades.

    Returns None when shadow mode is off, otherwise:
        {"would_block": bool, "why": str, "drop_pct": float|None,
         "need_drop": float, "high_20": float|None}
    """
    if not shadow_on():
        return None
    t, f = token or {}, facts or {}
    p = _num(t.get("price_usd"))
    hi = _num(f.get("high_20", t.get("high_20")))
    need = float(MEME.get("pullback_min_drop", PULLBACK_DROP))
    if p is None or hi is None or hi <= 0 or p <= 0:
        return {"would_block": False, "why": "unknown high", "drop_pct": None,
                "need_drop": need, "high_20": hi}
    off = 1.0 - p / hi
    would = off < need
    return {
        "would_block": would,
        "why": (f"at_high ({off*100:.1f}% below high, need {need*100:.0f}%)"
                if would else f"pullback ({off*100:.1f}% below high)"),
        "drop_pct": round(off, 4),
        "need_drop": need,
        "high_20": hi,
    }


def exit_overrides() -> dict:
    """Exit ladder that goes with the pullback package (empty when off)."""
    return active_ladder() if MEME.get("pullback_entry", False) else {}


def _price_plan(entry, tp: dict) -> dict:
    if not entry or entry <= 0:
        return {"entry": None, "stop": None, "tp1": None, "tp2": None,
                "risk_reward": None}
    stop = entry * (1 - tp["stop_pct"])
    tp1 = entry * tp["tp1_at"]
    tp2 = entry * tp["tp2_at"] if tp["tp2_at"] < 1e8 else None
    rr = (tp["tp1_at"] - 1.0) / tp["stop_pct"] if tp["stop_pct"] else None
    return {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "risk_reward": round(rr, 2) if rr is not None else None}


def apply(sig: dict, token: dict | None = None, facts: dict | None = None) -> dict:
    """Apply the enabled tactics to a finished signal. Pure; returns the same dict.

    A failing gate downgrades BUY to HOLD and says why. Passing gates never
    *raise* a signal -- this module can only tighten, exactly like the free
    signal chips.
    """
    t, f = token or {}, facts or {}
    # Shadow verdict is recorded regardless of whether any tactic is enabled:
    # it is a measurement, not a gate, so it must never change the outcome.
    sh = shadow(t, f)
    if sh is not None:
        sig["tactic_shadow"] = sh

    on = flags()
    if not any(on.values()):
        return sig

    reasons, warnings = sig.setdefault("reasons", []), sig.setdefault("warnings", [])

    # --- entry-side gates: only ever remove a BUY ---------------------------
    blocked = None
    if on["pullback"]:
        hi = f.get("high_20", t.get("high_20"))
        ok, why = pullback_ok(t.get("price_usd"), hi)
        if not ok and on.get("retest"):
            # "dip OR retest": a confirmed breakout-retest-bounce is a second way in.
            rt = f.get("retest") or {}
            if rt.get("ok"):
                ok, why = True, "retest"
                reasons.append(f"meme tactic: {rt.get('why', 'retest')}")
        if not ok:
            blocked = f"meme tactic: {why}"
        elif why == "unknown":
            warnings.append("meme tactic: no 20-bar high, pullback unchecked")
    if blocked is None and on["flash"]:
        ok, why = flash_ok(t.get("change_m5", f.get("chg_5m")))
        if not ok:
            blocked = f"meme tactic: {why}"
    if blocked is None and on["vol_ceiling"]:
        ok, why = vol_ok(f.get("atr_pct"))
        if not ok:
            blocked = f"meme tactic: {why}"

    if blocked and sig.get("action") == "BUY":
        sig["action"] = "HOLD"
        sig["warnings"] = list(dict.fromkeys([blocked] + warnings))[:6]
        sig["tactic_block"] = blocked
        return sig

    # --- exit-side: the pullback package uses a wider stop, slower ladder ---
    if on["atr_stop"]:
        stop = atr_stop_pct(f.get("atr_pct"))
        if stop is not None:
            sig.setdefault("take_profit", {})["stop_pct"] = stop
            sig["plan"] = _price_plan(t.get("price_usd"), sig["take_profit"])

    ov = exit_overrides()
    if ov and sig.get("action") in ("BUY", "SELL"):
        tp = dict(sig.get("take_profit") or {})
        tp.update(ov)
        sig["take_profit"] = tp
        sig["plan"] = _price_plan(t.get("price_usd"), tp)
        reasons.append("meme tactic: pullback entry, patient 2x/3x ladder")

    sig["reasons"] = list(dict.fromkeys(reasons))[:6]
    return sig
