"""Where should TP1 / TP2 sit? Measured, not guessed.

For every entry the desk's own signal would take, walk forward up to `horizon`
bars and record the maximum favourable excursion (MFE). The distribution of MFE
is the honest answer to "how far does this actually run".

If 70% of trades never reach +30%, a TP1 at 1.30x is a lottery ticket, not a
plan. If the median MFE is +8%, then TP1 at 1.30x means most trades die at the
stop before the first rung ever fires.

Pure offline: reads the cached candles in data/g87e_surv/, no network.
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desk import g87e  # noqa: E402

CFG = g87e.Config(
    atr_len=14, risk_pct=1.0, stop_mult=2.5, max_bars=24,
    initial_capital=50.0, fee_pct=0.05, slippage_pct=0.30, cooldown_bars=3,
    notional_cap=0.90, atr_floor_pct=0.05, require_trend=True, ema_len=50,
    compounding=True, variant="fixed", trail_arm_mult=2.0,
    trail_offset_mult=1.0, min_notional_pct=0.25,
)

HORIZONS = (12, 24, 48)
RUNGS = (1.10, 1.15, 1.20, 1.30, 1.50, 1.80, 2.00, 2.50)


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def main():
    files = sorted(glob.glob("data/g87e_surv/*.json"))
    if not files:
        print("no cached candles in data/g87e_surv/ - run scripts/guard_g87e.py first")
        return 1

    rows = []
    per_pool = {}
    for path in files:
        pool = os.path.basename(path)[:-5]
        candles = json.load(open(path))
        if len(candles) < 120:
            continue
        sig = g87e.signals(candles, CFG)
        hits = [i for i, r in enumerate(sig) if r.get("long") or r.get("short")]
        # respect the desk's cooldown so we count independent setups, not clusters
        picked, last = [], -10_000
        for i in hits:
            if i - last >= CFG.cooldown_bars:
                picked.append(i)
                last = i
        pool_rows = []
        for i in picked:
            side = "long" if sig[i].get("long") else "short"
            entry = float(candles[i]["close"])
            stop_mult = CFG.stop_mult
            atr = sig[i].get("atr") or 0.0
            if not atr or not entry:
                continue
            stop_px = entry - atr * stop_mult if side == "long" else entry + atr * stop_mult
            rec = {"pool": pool, "side": side, "i": i, "entry": entry,
                   "stop_pct": abs(entry - stop_px) / entry * 100.0}
            for h in HORIZONS:
                fut = candles[i + 1:i + 1 + h]
                if not fut:
                    continue
                if side == "long":
                    best = max(float(c["high"]) for c in fut)
                    worst = min(float(c["low"]) for c in fut)
                    mfe = (best / entry - 1.0) * 100.0
                    mae = (worst / entry - 1.0) * 100.0
                else:
                    best = min(float(c["low"]) for c in fut)
                    worst = max(float(c["high"]) for c in fut)
                    mfe = (1.0 - best / entry) * 100.0
                    mae = (1.0 - worst / entry) * 100.0
                # did the stop get hit BEFORE the rung? pessimistic: stop first
                hit_stop = (mae <= -rec["stop_pct"])
                rec[f"mfe{h}"] = mfe
                rec[f"mae{h}"] = mae
                rec[f"stopped{h}"] = hit_stop
            pool_rows.append(rec)
        rows.extend(pool_rows)
        if pool_rows:
            per_pool[pool] = len(pool_rows)

    if not rows:
        print("no entries found")
        return 1

    print(f"pools={len(per_pool)}  independent setups={len(rows)}  "
          f"long={sum(1 for r in rows if r['side']=='long')}  "
          f"short={sum(1 for r in rows if r['side']=='short')}")
    print(f"median stop distance: {st.median([r['stop_pct'] for r in rows]):.2f}%\n")

    print("=== how far price actually runs (MFE, % above entry) ===")
    print(f"{'horizon':>8} {'p25':>8} {'p50':>8} {'p75':>8} {'p90':>8} {'mean':>8} {'%neg':>7}")
    for h in HORIZONS:
        v = [r[f"mfe{h}"] for r in rows if f"mfe{h}" in r]
        neg = sum(1 for x in v if x <= 0) / len(v) * 100
        print(f"{h:>7}b {pct(v,25):>8.2f} {pct(v,50):>8.2f} {pct(v,75):>8.2f} "
              f"{pct(v,90):>8.2f} {st.mean(v):>8.2f} {neg:>6.1f}%")

    print("\n=== would a TP rung at Xx ever fire within 24 bars? ===")
    print(f"{'rung':>7} {'reach%':>8} {'reach+survive%':>16} {'note'}")
    for mult in RUNGS:
        need = (mult - 1.0) * 100.0
        reach = sum(1 for r in rows if r.get("mfe24", -999) >= need)
        survive = sum(1 for r in rows if r.get("mfe24", -999) >= need and not r.get("stopped24"))
        note = ""
        if reach / len(rows) < 0.25:
            note = "fires on <1 in 4 setups - dead rung"
        elif survive / len(rows) > 0.35:
            note = "fires often and survives - plausible TP1"
        print(f"{mult:>6.2f}x {reach/len(rows)*100:>7.1f}% {survive/len(rows)*100:>15.1f}%  {note}")

    print("\n=== the trap: rung fires but stop already took you out ===")
    for mult in (1.20, 1.30, 1.50):
        need = (mult - 1.0) * 100.0
        reach = [r for r in rows if r.get("mfe24", -999) >= need]
        stopped = [r for r in reach if r.get("stopped24")]
        if reach:
            print(f"  {mult:.2f}x reached by {len(reach)} setups, but {len(stopped)} "
                  f"({len(stopped)/len(reach)*100:.0f}%) had already hit the stop in the same window")

    out = {"setups": len(rows), "pools": len(per_pool),
           "median_stop_pct": round(st.median([r["stop_pct"] for r in rows]), 3),
           "mfe": {f"h{h}": {"p25": round(pct([r[f'mfe{h}'] for r in rows if f'mfe{h}' in r], 25), 3),
                             "p50": round(pct([r[f'mfe{h}'] for r in rows if f'mfe{h}' in r], 50), 3),
                             "p75": round(pct([r[f'mfe{h}'] for r in rows if f'mfe{h}' in r], 75), 3),
                             "p90": round(pct([r[f'mfe{h}'] for r in rows if f'mfe{h}' in r], 90), 3)}
                   for h in HORIZONS},
           "rungs": {f"{m}x": {"reach_pct": round(sum(1 for r in rows if r.get("mfe24", -999) >= (m-1)*100)/len(rows)*100, 2),
                               "survive_pct": round(sum(1 for r in rows if r.get("mfe24", -999) >= (m-1)*100 and not r.get("stopped24"))/len(rows)*100, 2)}
                     for m in RUNGS}}
    json.dump(out, open("tp_placement.json", "w"), indent=1)
    print("\nwrote tp_placement.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
