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


def active_ladder() -> dict:
    """The ladder in force, resolved from config then env, falling back to fast."""
    name = (os.getenv("BRAIN_EXIT_LADDER", "")
            or MEME.get("exit_ladder", "")
            or DEFAULT_LADDER)
    return dict(LADDERS.get(str(name).strip().lower(), LADDERS[DEFAULT_LADDER]))


EXIT_OVERRIDES = dict(LADDERS[DEFAULT_LADDER])


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None          # reject NaN


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
