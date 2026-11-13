"""Offline evaluation of technical indicators — test first, add only if it wins.

This is the gate the operator asked for: *before* an indicator is allowed into
the live desk, run it here on candle paths and see whether it actually lifts the
win rate and profit factor. Nothing in this module trades, writes settings or
calls the network. It only reads candle dicts.

What it measures
----------------
For a set of candle paths we take every bar where the desk's own signal engine
says BUY (the baseline), replay the desk's exit rules forward, and record the
outcome. Then for each candidate indicator filter we keep only the BUY entries
that also pass the filter and compare:

    win rate, avg return %, profit factor, trade count, kept %

We also test indicators as *exit* timing (e.g. "sell when RSI is overbought")
by replaying the same entries with an extra exit rule.

Verdict bar (stated up front, not moved afterwards)
---------------------------------------------------
An indicator is ADD-WORTHY only if, on the sample:

  * it keeps at least ``MIN_TRADES`` trades (so the number means something),
  * lifts win rate by at least ``MIN_WIN_LIFT`` (5 percentage points) over the
    baseline, AND
  * reaches a profit factor of at least ``MIN_PF`` (1.2) that is not worse than
    the baseline's.

Anything else is DO-NOT-ADD. A filter that just shrinks the sample without
lifting the win rate is noise, not edge.
"""
from __future__ import annotations

import random

from config import PAPER
from desk import exits as exit_rules
from desk import indicators as ind
from desk import signal_engine
from desk.signal_backtest import DEFAULT_TP_PLANS, features_from_candles, simulate

MIN_TRADES = 30
MIN_WIN_LIFT = 0.05
MIN_PF = 1.2

# Indicators only look back a bounded number of bars (the longest are EMA-50 and
# the 50-bar Fibonacci swing). Computing them on the last WINDOW bars instead of
# the whole path keeps the harness fast on 1,000-bar histories without changing
# the values: EMA-50 has ~150 smoothing iterations by then, so the seed is
# long forgotten. Widen it if a longer-lookback indicator is ever added.
WINDOW = 200


def _snap(candles: list[dict], i: int) -> dict:
    """Indicator snapshot at bar ``i``, using only bars up to and including it."""
    lo = max(0, i + 1 - WINDOW)
    return ind.snapshot(candles[lo:i + 1])


# --------------------------------------------------------------------------- #
# Candidate entry filters (each: snapshot -> bool)
# --------------------------------------------------------------------------- #
def _rsi(s):
    return s.get("rsi14")


def f_rsi_oversold(s):
    v = _rsi(s)
    return v is not None and v < 35


def f_rsi_not_overbought(s):
    v = _rsi(s)
    return v is not None and v < 70


def f_rsi_reclaim(s):
    v = _rsi(s)
    return v is not None and 35 <= v < 60


def f_macd_bull(s):
    m = s.get("macd")
    return bool(m) and m["hist"] > 0 and m["macd"] > m["signal"]


def f_macd_cross_up(s):
    m = s.get("macd")
    return bool(m) and m["hist"] > 0


def f_atr_band(s):
    a, c = s.get("atr14"), s.get("close")
    if a is None or not c:
        return False
    pct = a / c
    return 0.004 <= pct <= 0.20


def f_bb_lower(s):
    b = s.get("bollinger")
    return bool(b) and b.get("pct_b") is not None and b["pct_b"] < 0.25


def f_bb_upper_half(s):
    b = s.get("bollinger")
    return bool(b) and b.get("pct_b") is not None and b["pct_b"] >= 0.5


def f_ema_stack(s):
    e9, e21, e50 = s.get("ema9"), s.get("ema21"), s.get("ema50")
    return None not in (e9, e21, e50) and e9 > e21 > e50


def f_fib_pocket(s):
    f = s.get("fib")
    return bool(f) and 0.382 <= f["position"] <= 0.786


def f_stoch_oversold(s):
    st = s.get("stoch")
    return bool(st) and st["k"] < 25


def f_stoch_rising(s):
    st = s.get("stoch")
    return bool(st) and st["k"] > st["d"] and st["k"] < 80


def f_adx_trend(s):
    a = s.get("adx14")
    return bool(a) and a["adx"] >= 20 and a["plus_di"] > a["minus_di"]


FILTERS = {
    "RSI < 35 (oversold)": f_rsi_oversold,
    "RSI < 70 (not overbought)": f_rsi_not_overbought,
    "RSI 35-60 (reclaim)": f_rsi_reclaim,
    "MACD hist>0 & above signal": f_macd_bull,
    "MACD hist>0": f_macd_cross_up,
    "ATR 0.4%-20% of price": f_atr_band,
    "Bollinger pct_b < 0.25": f_bb_lower,
    "Bollinger pct_b >= 0.5": f_bb_upper_half,
    "EMA 9>21>50 stack": f_ema_stack,
    "Fib position 0.382-0.786": f_fib_pocket,
    "Stoch %K < 25": f_stoch_oversold,
    "Stoch %K>%D & %K<80": f_stoch_rising,
    "ADX>=20 & +DI>-DI": f_adx_trend,
}

COMBOS = {
    "EMA stack + MACD bull": lambda s: f_ema_stack(s) and f_macd_bull(s),
    "EMA stack + ADX trend": lambda s: f_ema_stack(s) and f_adx_trend(s),
    "RSI<70 + MACD bull": lambda s: f_rsi_not_overbought(s) and f_macd_bull(s),
    "Fib pocket + RSI<70": lambda s: f_fib_pocket(s) and f_rsi_not_overbought(s),
}


# --------------------------------------------------------------------------- #
# Exit-timing variants: extra predicate that closes the whole position
# --------------------------------------------------------------------------- #
def x_rsi_overbought(s):
    v = _rsi(s)
    return v is not None and v >= 75


def x_macd_bear(s):
    m = s.get("macd")
    return bool(m) and m["hist"] < 0


def x_stoch_overbought(s):
    st = s.get("stoch")
    return bool(st) and st["k"] > 85


EXITS = {
    "exit RSI >= 75": x_rsi_overbought,
    "exit MACD hist < 0": x_macd_bear,
    "exit Stoch %K > 85": x_stoch_overbought,
}


# --------------------------------------------------------------------------- #
# Replay with an optional extra exit rule
# --------------------------------------------------------------------------- #
def simulate_with_exit(candles: list[dict], entry_idx: int, horizon: str,
                       extra_exit=None, prof: dict | None = None) -> dict | None:
    if entry_idx < 0 or entry_idx >= len(candles) - 1:
        return None
    entry = candles[entry_idx]["close"]
    if not entry or entry <= 0:
        return None
    prof = prof or exit_rules.profile(horizon)
    pos = {"entry": entry, "opened": candles[entry_idx]["ts"], "horizon": horizon,
           "stop_pct": prof["stop_pct"], "peak": entry, "stage": 0, "miss": 0,
           "stop_price": entry * (1 - prof["stop_pct"])}
    fee, slip = PAPER["fee_pct"], PAPER["slippage_pct"]
    cost_basis = 1.0 * (1 + fee + slip)
    qty = 1.0 / entry
    remaining, cash, reasons, last = qty, 0.0, [], entry
    for j in range(entry_idx + 1, len(candles)):
        c = candles[j]
        price = c["close"]
        last = price
        q = {"price_usd": price, "volume_h6": c.get("vol"),
             "volume_h24": (c.get("vol") or 0) * 4}
        sig = exit_rules.evaluate(pos, q, now=c["ts"], prof=prof)
        if sig:
            frac, why = sig
            sell_qty = remaining * frac
            cash += sell_qty * price * (1 - fee - slip)
            remaining -= sell_qty
            reasons.append(why)
            if remaining <= 1e-15:
                break
        if extra_exit is not None and remaining > 1e-15:
            snap = _snap(candles, j)
            if extra_exit(snap):
                cash += remaining * price * (1 - fee - slip)
                remaining = 0.0
                reasons.append("indicator exit")
                break
    if remaining > 1e-15:
        cash += remaining * last * (1 - fee - slip)
        reasons.append("data end close")
    pnl = cash - cost_basis
    return {"entry": entry, "exit_px": last, "pnl": pnl,
            "ret_pct": pnl / cost_basis * 100.0, "reasons": reasons}


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _agg(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0, "win_rate": None, "avg_ret_pct": None,
                "profit_factor": None, "total_ret_pct": None}
    wins = sum(1 for r in rows if r["ret_pct"] > 0)
    gw = sum(r["ret_pct"] for r in rows if r["ret_pct"] > 0)
    gl = -sum(r["ret_pct"] for r in rows if r["ret_pct"] <= 0)
    return {
        "trades": n,
        "win_rate": round(wins / n, 4),
        "wins": wins,
        "avg_ret_pct": round(sum(r["ret_pct"] for r in rows) / n, 3),
        "total_ret_pct": round(sum(r["ret_pct"] for r in rows), 2),
        "profit_factor": round(gw / gl, 3) if gl else None,
    }


def _baseline_entries(paths, bar_minutes, min_history, step) -> list[dict]:
    rows = []
    for p in paths:
        candles = p["candles"]
        horizon = p.get("horizon") or "scalp"
        for i in range(min_history, len(candles) - 1, max(1, step)):
            token = features_from_candles(candles, i, bar_minutes)
            sig = signal_engine.evaluate(token)
            if sig["action"] != "BUY":
                continue
            trade = simulate(candles, i, horizon)
            if not trade:
                continue
            rows.append({"candles": candles, "idx": i, "horizon": horizon,
                         "ret_pct": trade["ret_pct"], "pnl": trade["pnl"],
                         "snap": _snap(candles, i)})
    return rows


def _tp_sweep(base: list[dict]) -> dict:
    """Replay every baseline BUY under each standard take-profit plan."""
    out = {}
    for name, overrides in DEFAULT_TP_PLANS.items():
        rows = []
        for r in base:
            prof = exit_rules.profile(r["horizon"], overrides=overrides)
            t = simulate(r["candles"], r["idx"], r["horizon"], prof=prof)
            if t:
                rows.append({"ret_pct": t["ret_pct"]})
        out[name] = _agg(rows)
    return out


def evaluate(paths: list[dict], bar_minutes: int = 5, min_history: int = 24,
             step: int = 1) -> dict:
    base = _baseline_entries(paths, bar_minutes, min_history, step)
    baseline = _agg(base)

    entry_filters = {}
    for name, fn in {**FILTERS, **COMBOS}.items():
        kept = [r for r in base if fn(r["snap"])]
        agg = _agg(kept)
        agg["kept_pct"] = round(len(kept) / len(base), 4) if base else None
        agg["win_lift"] = (round(agg["win_rate"] - baseline["win_rate"], 4)
                           if agg["win_rate"] is not None and baseline["win_rate"] is not None
                           else None)
        entry_filters[name] = agg

    exit_rules_eval = {}
    for name, fn in EXITS.items():
        rows = []
        for r in base:
            t = simulate_with_exit(r["candles"], r["idx"], r["horizon"], extra_exit=fn)
            if t:
                rows.append({"ret_pct": t["ret_pct"]})
        agg = _agg(rows)
        agg["win_lift"] = (round(agg["win_rate"] - baseline["win_rate"], 4)
                           if agg["win_rate"] is not None and baseline["win_rate"] is not None
                           else None)
        exit_rules_eval[name] = agg

    return {
        "baseline": baseline,
        "entry_filters": entry_filters,
        "exit_rules": exit_rules_eval,
        "tp_sweep": _tp_sweep(base),
        "paths": len(paths),
        "bars": sum(len(p["candles"]) for p in paths),
    }


def verdict(agg: dict, baseline: dict) -> str:
    """ADD / DO-NOT-ADD against the stated bar."""
    if not agg or not agg.get("trades"):
        return "DO-NOT-ADD (no trades)"
    if agg["trades"] < MIN_TRADES:
        return f"DO-NOT-ADD (only {agg['trades']} trades)"
    wr, bwr = agg.get("win_rate"), baseline.get("win_rate")
    if wr is None or bwr is None:
        return "DO-NOT-ADD (no win rate)"
    if wr - bwr < MIN_WIN_LIFT:
        return f"DO-NOT-ADD (win lift {100*(wr-bwr):+.1f}pp < {100*MIN_WIN_LIFT:.0f}pp)"
    pf = agg.get("profit_factor")
    bpf = baseline.get("profit_factor")
    if pf is None or pf < MIN_PF:
        return f"DO-NOT-ADD (PF {pf} < {MIN_PF})"
    if bpf is not None and pf < bpf:
        return f"DO-NOT-ADD (PF {pf} < baseline {bpf})"
    return "ADD"


def summarize(report: dict) -> dict:
    """Attach a verdict to every filter/exit and return the add-worthy list."""
    base = report["baseline"]
    out = {"baseline": base, "entry_filters": {}, "exit_rules": {},
           "tp_sweep": report.get("tp_sweep", {}),
           "add_entry": [], "add_exit": []}
    for name, agg in report["entry_filters"].items():
        v = verdict(agg, base)
        out["entry_filters"][name] = {**agg, "verdict": v}
        if v == "ADD":
            out["add_entry"].append(name)
    for name, agg in report["exit_rules"].items():
        v = verdict(agg, base)
        out["exit_rules"][name] = {**agg, "verdict": v}
        if v == "ADD":
            out["add_exit"].append(name)
    return out


# --------------------------------------------------------------------------- #
# Synthetic paths (reproducible, memecoin-like) for offline tests
# --------------------------------------------------------------------------- #
def synth_paths(n: int = 60, bars: int = 240, bar_minutes: int = 5,
                seed: int = 7, base: float = 1e-6) -> list[dict]:
    """Fat-tailed, jumpy price paths that look like young memecoin charts."""
    rng = random.Random(seed)
    paths = []
    ts0 = 1_700_000_000
    for p in range(n):
        price = base * (0.5 + rng.random())
        candles = []
        drift = rng.uniform(-0.0008, 0.0012)
        vol = rng.uniform(0.006, 0.03)
        for b in range(bars):
            shock = rng.gauss(0, 1)
            jump = 0.0
            if rng.random() < 0.02:
                jump = rng.choice([-1, 1]) * rng.uniform(0.05, 0.25)
            ret = drift + vol * shock + jump
            price = max(1e-12, price * (1.0 + ret))
            o = price / (1.0 + ret) if ret != -1 else price
            hi = max(o, price) * (1.0 + abs(rng.gauss(0, vol / 2)))
            lo = min(o, price) * (1.0 - abs(rng.gauss(0, vol / 2)))
            volu = rng.uniform(500, 50_000) * (1.0 + abs(ret) * 20)
            candles.append({"ts": ts0 + b * bar_minutes * 60,
                            "open": o, "high": hi, "low": lo,
                            "close": price, "vol": volu})
        paths.append({"name": f"synth-{p}", "candles": candles,
                      "horizon": "scalp" if p % 2 == 0 else "swing"})
    return paths
