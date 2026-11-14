#!/usr/bin/env python3
"""Replay two exit ladders over the same real setups and compare them honestly.

The question: the desk runs a FAST ladder (TP1 +25% half, TP2 +60%, 15% trail,
-30% stop) that was validated at 68.9% win on 120 pools. The operator's live
journal shows 30.3% (40W/92L). Does the fast ladder actually make money on the
population the desk trades, or only on the population it was tuned on?

Method: take every entry the backtest already found, then walk the real candles
forward bar by bar and apply each ladder with intrabar high/low. No new entries
are invented -- only the exit rule changes, so the comparison isolates the ladder.

Conservative rule: when a bar's range contains BOTH the stop and a take-profit,
the stop is assumed to fill first. That understates every ladder equally, so the
ranking between them stays fair.

Usage:
    python3 scripts/ladder_replay.py                 # both ladders, real candles
    python3 scripts/ladder_replay.py --stop 0.20     # override the hard stop
    python3 scripts/ladder_replay.py --json          # machine-readable
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# name -> ladder geometry
LADDERS = {
    "fast (live)":   {"tp1_at": 1.25, "tp1_frac": 0.50, "tp2_at": 1.60, "tp2_frac": 0.50,
                      "trail_pct": 0.15, "stop_pct": 0.30},
    "slow (config)": {"tp1_at": 1.50, "tp1_frac": 0.33, "tp2_at": 2.00, "tp2_frac": 0.50,
                      "trail_pct": 0.20, "stop_pct": 0.30},
    "single 1.35x":  {"tp1_at": 1.35, "tp1_frac": 0.50, "tp2_at": None, "tp2_frac": 0.0,
                      "trail_pct": 0.18, "stop_pct": 0.30},
}


def load_candles() -> dict:
    out = {}
    for f in glob.glob(str(ROOT / "data" / "g87e" / "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        key = (d.get("label"), d.get("tf"))
        if key[0] and key[1]:
            out[key] = d.get("candles") or []
    return out


def replay(trade: dict, candles: list, L: dict, max_bars: int = 96):
    """Walk forward from the entry bar and return the ladder's realised result."""
    i0 = trade.get("entry_i")
    entry = trade.get("entry")
    if i0 is None or not entry:
        return None
    long_side = (trade.get("side") or "long") == "long"

    stop_pct = L["stop_pct"]
    tp1_at, tp1_frac = L["tp1_at"], L["tp1_frac"]
    tp2_at, tp2_frac = L["tp2_at"], L["tp2_frac"]
    trail_pct = L["trail_pct"]

    remaining = 1.0
    realised = 0.0
    peak = entry
    stop_px = entry * (1 - stop_pct) if long_side else entry * (1 + stop_pct)
    tp1_done = False
    tp2_done = tp2_at is None

    for j in range(i0 + 1, min(len(candles), i0 + 1 + max_bars)):
        bar = candles[j]
        hi, lo = bar["high"], bar["low"]

        # conservative: the stop is checked before any take-profit
        hit_stop = (lo <= stop_px) if long_side else (hi >= stop_px)
        if hit_stop:
            r = (stop_px / entry - 1.0) if long_side else (1.0 - stop_px / entry)
            realised += remaining * r * 100.0
            return {"ret_pct": realised, "reason": "stop" if not tp1_done else "breakeven/trail"}

        if not tp1_done:
            reached = (hi >= entry * tp1_at) if long_side else (lo <= entry * tp1_at)
            if reached:
                realised += tp1_frac * (tp1_at - 1.0) * 100.0
                remaining -= tp1_frac
                tp1_done = True
                stop_px = entry
                if long_side:
                    peak = max(peak, entry * tp1_at)
        elif not tp2_done:
            reached = (hi >= entry * tp2_at) if long_side else (lo <= entry * tp2_at)
            if reached:
                realised += remaining * tp2_frac * (tp2_at - 1.0) * 100.0
                remaining -= remaining * tp2_frac
                tp2_done = True

        peak = max(peak, hi) if long_side else min(peak, lo)
        if tp1_done and remaining > 1e-9:
            trail_px = peak * (1 - trail_pct) if long_side else peak * (1 + trail_pct)
            hit = (lo <= trail_px) if long_side else (hi >= trail_px)
            if hit:
                r = (trail_px / entry - 1.0) if long_side else (1.0 - trail_px / entry)
                realised += remaining * r * 100.0
                return {"ret_pct": realised, "reason": "trail"}

        if remaining <= 1e-9:
            return {"ret_pct": realised, "reason": "tp2"}

    last = candles[min(len(candles) - 1, i0 + max_bars)]
    r = (last["close"] / entry - 1.0) if long_side else (1.0 - last["close"] / entry)
    realised += remaining * r * 100.0
    return {"ret_pct": realised, "reason": "timeout"}


def stats(rets: list) -> dict:
    n = len(rets)
    if not n:
        return {"trades": 0}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    avg_w = gross_w / len(wins) if wins else 0.0
    avg_l = gross_l / len(losses) if losses else 0.0
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4),
        "avg_win_pct": round(avg_w, 2),
        "avg_loss_pct": round(avg_l, 2),
        "rr": round(avg_w / avg_l, 3) if avg_l else None,
        "expectancy_pct": round(sum(rets) / n, 3),
        "profit_factor": round(gross_w / gross_l, 3) if gross_l else None,
        "total_pct": round(sum(rets), 1),
        "breakeven_win": round(1 / (1 + (avg_w / avg_l)) * 100, 1) if avg_l and avg_w else None,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", type=float, default=None, help="override hard stop (e.g. 0.20)")
    ap.add_argument("--bars", type=int, default=24, help="max bars to hold (default 24)")
    ap.add_argument("--sweep", action="store_true", help="sweep stop width on the fast ladder")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--bt", default=str(ROOT / "backtest_g87e.json"))
    args = ap.parse_args(argv)

    candles = load_candles()
    bt = json.load(open(args.bt, encoding="utf-8"))
    groups = bt.get("trades") or {}

    entries = []
    seen = set()
    for key, variants in groups.items():
        label, tf = key.rsplit(":", 1)
        cs = candles.get((label, tf))
        if not cs:
            continue
        for vname, trades in variants.items():
            for t in trades or []:
                sig = (key, t.get("entry_ts"), t.get("side"))
                if sig in seen:
                    continue
                seen.add(sig)
                entries.append((t, cs))

    def run_one(L):
        rets, reasons = [], {}
        for t, cs in entries:
            r = replay(t, cs, L, max_bars=args.bars)
            if r is None:
                continue
            rets.append(r["ret_pct"])
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        return {**stats(rets), "exits": reasons, "stop_pct": L["stop_pct"]}

    if args.sweep:
        print(f"stop-width sweep on the FAST ladder, {len(entries)} real entries, "
              f"max {args.bars} bars\n")
        hdr = (f"{'stop':>6}{'n':>6}{'win%':>7}{'avgW':>8}{'avgL':>8}"
               f"{'R:R':>7}{'exp%':>8}{'PF':>7}{'need%':>7}  exits")
        print(hdr)
        print("-" * len(hdr))
        for s in (0.015, 0.03, 0.05, 0.10, 0.20, 0.30):
            r = run_one({**LADDERS["fast (live)"], "stop_pct": s})
            if not r.get("trades"):
                continue
            print(f"{s*100:>5.1f}%{r['trades']:>6}{r['win_rate']*100:>6.1f}%"
                  f"{r['avg_win_pct']:>8.2f}{r['avg_loss_pct']:>8.2f}"
                  f"{(r['rr'] or 0):>7.2f}{r['expectancy_pct']:>8.2f}"
                  f"{(r['profit_factor'] or 0):>7.2f}{(r['breakeven_win'] or 0):>6.1f}%"
                  f"  {r['exits']}")
        return 0

    out = {"entries": len(entries), "ladders": {}}
    for name, L in LADDERS.items():
        if args.stop is not None:
            L = {**L, "stop_pct": args.stop}
        out["ladders"][name] = run_one(L)

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"replayed {out['entries']} real entries from {Path(args.bt).name}")
    print(f"stop override: {args.stop if args.stop is not None else 'per-ladder (30%)'}\n")
    hdr = (f"{'ladder':<14}{'n':>5}{'win%':>7}{'avgW':>8}{'avgL':>8}"
           f"{'R:R':>7}{'exp%':>8}{'PF':>7}{'total%':>9}{'need%':>7}")
    print(hdr)
    print("-" * len(hdr))
    for name, s in out["ladders"].items():
        if not s.get("trades"):
            print(f"{name:<14}  no trades")
            continue
        print(f"{name:<14}{s['trades']:>5}{s['win_rate']*100:>6.1f}%"
              f"{s['avg_win_pct']:>8.2f}{s['avg_loss_pct']:>8.2f}"
              f"{(s['rr'] or 0):>7.2f}{s['expectancy_pct']:>8.2f}"
              f"{(s['profit_factor'] or 0):>7.2f}{s['total_pct']:>9.1f}"
              f"{(s['breakeven_win'] or 0):>6.1f}%")
    print("\nexit mix:")
    for name, s in out["ladders"].items():
        print(f"  {name:<14} {s.get('exits')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
