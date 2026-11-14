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

That clears the repo's stated bar (>=30 trades, +5pp win rate, PF >= 1.2) and it
held on both halves of the sample (PF 1.85 on half A, 1.27 on half B), so it is
not a single lucky slice.

The cost is frequency: the filter keeps only ~4% of signal BUYs. That is why it
ships OFF. Turn it on when you would rather take a handful of high-quality
pullback entries than many top-of-range chases.

Everything here is pure: no I/O, no clock, no network.
"""
from __future__ import annotations

from config import MEME

# The exact package that cleared the bar in scripts/meme_tactics_deep.py.
PULLBACK_DROP = 0.20
EXIT_OVERRIDES = {
    "stop_pct": 0.30,
    "tp1_at": 2.0, "tp1_frac": 0.33,
    "tp2_at": 3.0, "tp2_frac": 0.5,
    "trail_pct": 0.25,
}


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


def exit_overrides() -> dict:
    """Exit ladder that goes with the pullback package (empty when off)."""
    return dict(EXIT_OVERRIDES) if MEME.get("pullback_entry", False) else {}


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
