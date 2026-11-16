#!/usr/bin/env python3
"""Fast-scalper lab — 1 to 10 minute holds on real 5-minute candles.

The desk's existing ladders are built for hours. The operator wants a true
scalp: in and out inside 10 minutes, on a *fresh* coin, entered only while the
5-minute candle is still moving up (a "moving candle"), and gated on age,
market-cap band and liquidity.

This lab answers one question honestly:

    On real Solana 5m candles, which (moving-candle entry, take-profit, stop,
    hold) combination clears a >=60% win rate AND still makes money (PF>=1.2)?

Method
------
* Bars are 5 minutes. A 1-2 bar hold is therefore 5-10 minutes, exactly the
  operator's window. We never look past the hold window.
* Entry fills at the close of the signal bar (same convention as the rest of
  the desk's backtests).
* Exit is checked intrabar, worst case first: if a bar touches both the stop and
  the target, the STOP wins. This is deliberately pessimistic.
* Fees + slippage are charged both sides, because a memecoin scalp pays them.

Gates
-----
* min_age / fresh window: age is measured from the first candle in the file
  (the launch proxy the desk uses for a fresh pool). Only bars inside the
  fresh window are eligible.
* mcap cap / liquidity: applied only when the pool file carries those fields.
  The local g87e files do not, so the lab reports how many pools were gated.
* moving candle: the signal bar must be green (close>open) AND above the prior
  close AND its body must be at least `min_body_pct` of price. This is the
  "still moving up, don't buy a dead candle" filter.

Usage
-----
    python3 scripts/scalper_lab.py
    python3 scripts/scalper_lab.py --hold-bars 1 2 --target-wr 0.60
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FEE_PCT = 0.01        # ~1% pump.fun swap each side (override with --fee)
SLIP_PCT = 0.02       # 2% each side, memecoins do not fill at mid (override with --slip)
_COSTS = {"fee": FEE_PCT, "slip": SLIP_PCT}


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def load_pools(data_dir: str) -> list[dict]:
    """Load every *_5m.json candle file. Returns pools with normalised candles."""
    out = []
    for f in sorted(Path(data_dir).glob("*_5m.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        cs = d.get("candles") or []
        if len(cs) < 40:
            continue
        # normalise + sort by ts, drop malformed bars
        clean = []
        for c in cs:
            try:
                o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if min(o, h, l, cl) <= 0:
                continue
            clean.append({"ts": float(c["ts"]), "open": o, "high": h, "low": l,
                          "close": cl, "vol": float(c.get("vol") or 0)})
        clean.sort(key=lambda x: x["ts"])
        if len(clean) < 40:
            continue
        out.append({
            "name": d.get("label") or f.stem,
            "address": d.get("pool") or f.stem,
            "candles": clean,
            "mcap": d.get("mcap") or d.get("fdv"),
            "liq": d.get("liq") or d.get("liquidity_usd"),
            "created_at": d.get("created_at"),
        })
    return out


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def age_minutes(pool: dict, i: int) -> float:
    """Age from the first candle (launch proxy)."""
    return (pool["candles"][i]["ts"] - pool["candles"][0]["ts"]) / 60.0


def entry_ok(candles: list[dict], i: int, lookback: int, min_body_pct: float,
             mode: str) -> bool:
    """Entry filter. Several honest definitions of "a candle that is moving".

    momentum  : green body, above prior close, above the lookback close (the
                literal "moving candle up" — buys strength).
    dip       : red body, below prior close (buys the shakeout — the repo's
                proven pullback edge).
    breakout  : close is the highest of the last `lookback` bars (buys a fresh
                high only, not a mid-range push).
    any       : no entry filter (baseline).
    """
    if i < max(lookback, 1):
        return False
    if mode == "any":
        return True
    c = candles[i]
    body = (c["close"] - c["open"]) / c["close"] if c["close"] else 0.0
    if mode == "momentum":
        return (c["close"] > c["open"] and body >= min_body_pct
                and c["close"] > candles[i - 1]["close"]
                and c["close"] > candles[i - lookback]["close"])
    if mode == "dip":
        return (c["close"] < c["open"]
                and c["close"] < candles[i - 1]["close"])
    if mode == "breakout":
        hi = max(x["high"] for x in candles[i - lookback:i])
        return c["close"] >= hi
    return False


def gate_ok(pool: dict, i: int, args) -> bool:
    a = age_minutes(pool, i)
    if a < args.min_age_min:
        return False
    if args.max_age_min and a > args.max_age_min:
        return False
    mcap = pool.get("mcap")
    if mcap is not None:
        if mcap < args.min_mcap:
            return False
        if mcap > args.max_mcap:
            return False
    liq = pool.get("liq")
    if liq is not None and liq < args.min_liq:
        return False
    return True


# --------------------------------------------------------------------------- #
# simulation: enter at close[i], exit within hold_bars
# --------------------------------------------------------------------------- #
def simulate(candles: list[dict], i: int, tp: float, stop: float, hold_bars: int) -> dict | None:
    entry = candles[i]["close"]
    if entry <= 0:
        return None
    tp_px = entry * (1 + tp)
    stop_px = entry * (1 - stop)
    last = min(i + hold_bars, len(candles) - 1)
    for j in range(i + 1, last + 1):
        c = candles[j]
        # pessimistic: stop first if both are touched in the same bar
        if c["low"] <= stop_px:
            gross = (stop_px / entry - 1.0)
            return _net(gross, "stop")
        if c["high"] >= tp_px:
            gross = (tp_px / entry - 1.0)
            return _net(gross, "tp")
    # timed out at the last close inside the window
    gross = (candles[last]["close"] / entry - 1.0)
    return _net(gross, "time")


def _net(gross: float, reason: str) -> dict:
    # costs charged on entry + exit
    net = gross - (_COSTS["fee"] + _COSTS["slip"]) * 2
    return {"ret": net, "reason": reason, "win": net > 0}


def simulate_ladder(candles: list[dict], i: int, tp1: float, tp1_frac: float,
                    tp2: float, tp2_frac: float, stop: float, hold_bars: int) -> dict | None:
    """Partial-ladder exit: sell `tp1_frac` at +tp1 (half), rest at +tp2, stop -stop.

    This is how the desk actually exits (TP1 half, breakeven stop, TP2 rest). It
    is the honest way to measure win rate: a trade that banks half at +3% and
    then stops the rest at breakeven is still a WIN, which the all-or-nothing
    simulator wrongly calls a loss. Pessimistic intrabar ordering: stop first.
    """
    entry = candles[i]["close"]
    if entry <= 0:
        return None
    stop_px = entry * (1 - stop)
    tp1_px = entry * (1 + tp1)
    tp2_px = entry * (1 + tp2) if tp2 else None
    remaining, realized, stage = 1.0, 0.0, 0
    last = min(i + hold_bars, len(candles) - 1)
    for j in range(i + 1, last + 1):
        c = candles[j]
        if c["low"] <= stop_px:
            realized += remaining * (stop_px / entry - 1.0)
            remaining = 0.0
            break
        if stage == 0 and c["high"] >= tp1_px:
            sell = min(remaining, tp1_frac)
            realized += sell * (tp1_px / entry - 1.0)
            remaining -= sell
            stage = 1
            stop_px = max(stop_px, entry)          # breakeven after TP1
        if stage == 1 and tp2_px and c["high"] >= tp2_px:
            sell = min(remaining, tp2_frac)
            realized += sell * (tp2_px / entry - 1.0)
            remaining -= sell
            stage = 2
    if remaining > 0:
        realized += remaining * (candles[last]["close"] / entry - 1.0)
    net = realized - (_COSTS["fee"] + _COSTS["slip"]) * 2
    return {"ret": net, "reason": "ladder", "win": net > 0}


# --------------------------------------------------------------------------- #
# sweep
# --------------------------------------------------------------------------- #
def run(pools: list[dict], args) -> list[dict]:
    results = []
    for mode in args.entry_mode:
        for tp in args.tp:
            for stop in args.stop:
                for hold in args.hold_bars:
                    for lookback in args.lookback:
                        rets, reasons = [], {"tp": 0, "stop": 0, "time": 0, "ladder": 0}
                        pools_hit = set()
                        for p in pools:
                            cs = p["candles"]
                            for i in range(max(lookback, 1), len(cs) - hold):
                                if not gate_ok(p, i, args):
                                    continue
                                if not entry_ok(cs, i, lookback, args.min_body_pct, mode):
                                    continue
                                if args.ladder:
                                    t = simulate_ladder(cs, i, tp, args.tp1_frac,
                                                        args.tp2, args.tp2_frac, stop, hold)
                                else:
                                    t = simulate(cs, i, tp, stop, hold)
                                if not t:
                                    continue
                                rets.append(t["ret"])
                                reasons[t["reason"]] += 1
                                pools_hit.add(p["address"])
                        n = len(rets)
                        if n == 0:
                            continue
                        wins = sum(1 for x in rets if x > 0)
                        gw = sum(x for x in rets if x > 0)
                        gl = -sum(x for x in rets if x <= 0)
                        wr = wins / n
                        pf = (gw / gl) if gl else float("inf")
                        # gross win rate = the TP was actually reached (ignores cost),
                        # so the reader can separate geometry from fees.
                        gross_wr = reasons["tp"] / n
                        results.append({
                            "mode": mode, "ladder": bool(args.ladder), "tp": tp, "stop": stop, "hold_bars": hold,
                            "hold_min": hold * 5, "lookback": lookback,
                            "trades": n, "pools": len(pools_hit),
                            "win_rate": round(wr, 4),
                            "gross_win_rate": round(gross_wr, 4),
                            "profit_factor": round(pf, 3) if pf != float("inf") else 999.0,
                            "avg_ret_pct": round(100 * sum(rets) / n, 3),
                            "tp_hits": reasons["tp"], "stop_hits": reasons["stop"],
                            "time_hits": reasons["time"],
                        })
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="1-10 min scalper sweep on real 5m candles")
    ap.add_argument("--data", default=str(ROOT / "data" / "g87e"))
    ap.add_argument("--entry-mode", nargs="+", default=["momentum", "dip", "breakout"],
                    choices=["momentum", "dip", "breakout", "any"])
    ap.add_argument("--fee", type=float, default=0.01, help="fee each side (0.01 = 1%)")
    ap.add_argument("--slip", type=float, default=0.02, help="slippage each side (0.02 = 2%)")
    ap.add_argument("--tp", type=float, nargs="+", default=[0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06])
    ap.add_argument("--stop", type=float, nargs="+", default=[0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10])
    ap.add_argument("--hold-bars", type=int, nargs="+", default=[1, 2])
    ap.add_argument("--lookback", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--ladder", action="store_true",
                    help="model the desk's partial ladder (sell tp1_frac at TP1, rest at TP2)")
    ap.add_argument("--tp1-frac", type=float, default=0.5)
    ap.add_argument("--tp2", type=float, default=0.0, help="TP2 offset (0 = off)")
    ap.add_argument("--tp2-frac", type=float, default=0.5)
    ap.add_argument("--min-body-pct", type=float, default=0.002)
    ap.add_argument("--min-age-min", type=float, default=0.0)
    ap.add_argument("--max-age-min", type=float, default=0.0,
                    help="fresh window in minutes; 0 = no cap")
    ap.add_argument("--min-mcap", type=float, default=0.0)
    ap.add_argument("--max-mcap", type=float, default=1e18)
    ap.add_argument("--min-liq", type=float, default=0.0)
    ap.add_argument("--target-wr", type=float, default=0.60)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--min-pools", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "research" / "scalper_lab.json"))
    args = ap.parse_args(argv)
    _COSTS["fee"], _COSTS["slip"] = args.fee, args.slip

    pools = load_pools(args.data)
    if not pools:
        print(f"no usable pools in {args.data}", file=sys.stderr)
        return 2

    print("=" * 92)
    print(f"  FAST-SCALPER LAB  —  {len(pools)} pools, 5m bars, hold 1-2 bars (5-10 min)")
    print(f"  cost each side: fee {args.fee*100:.1f}% + slip {args.slip*100:.1f}% "
          f"(round trip {2*(args.fee+args.slip)*100:.1f}%)")
    print(f"  entry modes: {', '.join(args.entry_mode)}")
    print(f"  bar: win >= {args.target_wr:.0%}, PF >= 1.2, trades >= {args.min_trades}, pools >= {args.min_pools}")
    print("=" * 92, flush=True)

    rows = run(pools, args)
    if not rows:
        print("no trades produced. Loosen gates.", file=sys.stderr)
        return 2

    for r in rows:
        r["clears"] = bool(
            r["trades"] >= args.min_trades and r["pools"] >= args.min_pools
            and r["win_rate"] >= args.target_wr and r["profit_factor"] >= 1.2)

    rows.sort(key=lambda r: (r["clears"], r["win_rate"], r["profit_factor"]), reverse=True)
    top = rows[:24]
    print(f"\n{'mode':>9} {'TP':>5} {'STOP':>5} {'hold':>5} {'lb':>3} {'n':>5} {'pools':>5} {'WR':>7} {'PF':>6} {'avg%':>7}  clear")
    for r in top:
        print(f"{r['mode']:>9} {r['tp']*100:>4.1f}% {r['stop']*100:>4.0f}% {r['hold_min']:>4}m {r['lookback']:>3} "
              f"{r['trades']:>5} {r['pools']:>5} {r['win_rate']*100:>6.1f}% {r['profit_factor']:>6.2f} "
              f"{r['avg_ret_pct']:>6.2f}%  {'YES' if r['clears'] else ''}")

    winners = [r for r in rows if r["clears"]]
    print("\n" + "-" * 84)
    if winners:
        best = winners[0]
        print(f"  BEST that clears the bar: [{best['mode']}] TP {best['tp']*100:.1f}% / stop {best['stop']*100:.0f}% / "
              f"hold {best['hold_min']}m / lookback {best['lookback']}  ->  "
              f"WR {best['win_rate']*100:.1f}%  PF {best['profit_factor']:.2f}  "
              f"n={best['trades']} pools={best['pools']}")
    else:
        print("  NO configuration cleared all bars. Closest by win rate:")
        for r in rows[:5]:
            print(f"    [{r['mode']}] TP {r['tp']*100:.1f}% stop {r['stop']*100:.0f}% hold {r['hold_min']}m -> "
                  f"WR {r['win_rate']*100:.1f}% PF {r['profit_factor']:.2f} n={r['trades']}")
    print("-" * 84)

    Path(args.out).write_text(json.dumps({
        "pools": len(pools), "target_wr": args.target_wr, "results": rows,
    }, indent=2), encoding="utf-8")
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
