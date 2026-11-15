"""Finer win-rate sweep on the repo's real 5m candles.

Tests what the repo did NOT test: pullback depths between 10-35%, stop widths
15-35%, and finer take-profit rungs. Bar-by-bar with intrabar high/low,
conservative fill order (stop before TP inside a bar), 3% round-trip cost.
"""
import json, glob, itertools, statistics as st
from collections import defaultdict

FEE = 0.01      # pump.fun swap fee
SLIP = 0.02     # slippage each way-ish (applied to entry and exit)

def load():
    pools = {}
    for f in sorted(glob.glob("data/g87e/*_5m.json")):
        d = json.load(open(f))
        arr = d.get("candles") or []
        bars = []
        for c in arr:
            try:
                bars.append((float(c["high"]), float(c["low"]), float(c["close"]),
                             float(c.get("vol") or 0)))
            except Exception:
                pass
        if len(bars) >= 40:
            pools[d.get("label") or f.split("/")[-1]] = bars
    return pools

def simulate(bars, i, pb, stop_pct, tp1_at, tp1_frac, tp2_at, trail_pct, max_hold):
    """Enter at close of bar i. Return net return fraction (after costs)."""
    entry = bars[i][2] * (1 + SLIP)
    stop = entry * (1 - stop_pct)
    peak = entry
    remaining = 1.0
    realized = 0.0
    tp1_done = tp2_done = False
    for j in range(i + 1, min(len(bars), i + 1 + max_hold)):
        hi, lo, cl, _ = bars[j]
        # conservative: stop first
        if lo <= stop:
            px = stop * (1 - SLIP)
            realized += remaining * (px / entry - 1)
            return realized - FEE
        # TP1
        if not tp1_done and hi >= entry * tp1_at:
            px = entry * tp1_at * (1 - SLIP)
            realized += tp1_frac * (px / entry - 1)
            remaining -= tp1_frac
            tp1_done = True
            stop = max(stop, entry)          # breakeven after TP1
        # TP2
        if tp1_done and not tp2_done and hi >= entry * tp2_at:
            px = entry * tp2_at * (1 - SLIP)
            realized += remaining * (px / entry - 1)
            remaining = 0.0
            tp2_done = True
            break
        # trail on the runner
        peak = max(peak, hi)
        if tp1_done and remaining > 0 and cl <= peak * (1 - trail_pct):
            px = cl * (1 - SLIP)
            realized += remaining * (px / entry - 1)
            remaining = 0.0
            break
    if remaining > 0:                        # timeout: close at last close
        px = bars[min(len(bars) - 1, i + max_hold)][2] * (1 - SLIP)
        realized += remaining * (px / entry - 1)
    return realized - FEE

LADDERS = {
    "fast 1.25/1.6 t15":  (1.25, 0.50, 1.60, 0.15),
    "quick 1.20/1.5 t15": (1.20, 0.50, 1.50, 0.15),
    "quick 1.15/1.4 t12": (1.15, 0.50, 1.40, 0.12),
    "single 1.35 t18":    (1.35, 0.50, 9.99, 0.18),
    "patient 2/3 t20":    (2.00, 0.50, 3.00, 0.20),
    "profile 1.5/2 t20":  (1.50, 0.33, 2.00, 0.20),
}

def run(pools, pb, stop_pct, ladder, max_hold=24, min_trades_pool=1):
    tp1, f1, tp2, tr = LADDERS[ladder]
    rets, per_pool = [], defaultdict(list)
    for name, bars in pools.items():
        highs = [b[0] for b in bars]
        for i in range(20, len(bars) - 1):
            hi20 = max(highs[i - 20:i])
            if hi20 <= 0:
                continue
            if bars[i][2] <= hi20 * (1 - pb):          # pullback entry
                r = simulate(bars, i, pb, stop_pct, tp1, f1, tp2, tr, max_hold)
                rets.append(r); per_pool[name].append(r)
    if not rets:
        return None
    wins = [r for r in rets if r > 0]
    gains = sum(r for r in rets if r > 0)
    losses = -sum(r for r in rets if r <= 0)
    pf = gains / losses if losses > 0 else 99
    active = sum(1 for v in per_pool.values() if v)
    return dict(trades=len(rets), pools=active, win=len(wins)/len(rets),
                pf=pf, avg=st.mean(rets)*100)

if __name__ == "__main__":
    pools = load()
    print(f"pools loaded: {len(pools)}  bars: {sum(len(b) for b in pools.values())}")
    rows = []
    for pb in (0.10, 0.15, 0.20, 0.25, 0.30, 0.35):
        for stop in (0.15, 0.20, 0.25, 0.30, 0.35):
            for lad in LADDERS:
                r = run(pools, pb, stop, lad)
                if r and r["trades"] >= 20:
                    r.update(pb=pb, stop=stop, ladder=lad)
                    rows.append(r)
    rows.sort(key=lambda r: (-r["win"], -r["pf"]))
    print(f"\n{'win%':>6} {'PF':>5} {'n':>5} {'pools':>5} {'avg%':>7}  pb   stop  ladder")
    for r in rows[:25]:
        print(f"{r['win']*100:6.1f} {r['pf']:5.2f} {r['trades']:5d} {r['pools']:5d} "
              f"{r['avg']:7.2f}  {int(r['pb']*100):>2}%  {int(r['stop']*100):>2}%  {r['ladder']}")
    json.dump(rows, open("research/fine_sweep.json", "w"), indent=1)
    print("\nwrote research/fine_sweep.json")
