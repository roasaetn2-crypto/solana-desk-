"""Experiment 2: entry-quality refinements on top of pullback.

Adds filters the repo never tested:
  reclaim  - only enter when the bar closes UP vs the prior bar (dip turning)
  vol      - only enter when bar volume >= k * 20-bar average volume
  trend    - only enter when close > EMA50 (higher-timeframe-ish uptrend)
  full_tp  - sell 100% at TP1 (pure scalp, max win rate)
"""
import json, glob, statistics as st
from collections import defaultdict

FEE, SLIP = 0.01, 0.015

def load():
    pools = {}
    for f in sorted(glob.glob("data/g87e/*_5m.json")):
        d = json.load(open(f)); bars = []
        for c in (d.get("candles") or []):
            try:
                bars.append((float(c["high"]), float(c["low"]), float(c["close"]),
                             float(c.get("vol") or 0)))
            except Exception:
                pass
        if len(bars) >= 60:
            pools[d.get("label") or f.split("/")[-1]] = bars
    return pools

def ema(vals, n):
    k = 2 / (n + 1); e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k); out.append(e)
    return out

def simulate(bars, i, stop_pct, tp1_at, tp1_frac, tp2_at, trail_pct, max_hold):
    entry = bars[i][2] * (1 + SLIP)
    stop = entry * (1 - stop_pct); peak = entry
    remaining, realized = 1.0, 0.0
    tp1_done = tp2_done = False
    for j in range(i + 1, min(len(bars), i + 1 + max_hold)):
        hi, lo, cl, _ = bars[j]
        if lo <= stop:
            realized += remaining * (stop * (1 - SLIP) / entry - 1)
            return realized - FEE
        if not tp1_done and hi >= entry * tp1_at:
            realized += tp1_frac * (entry * tp1_at * (1 - SLIP) / entry - 1)
            remaining -= tp1_frac; tp1_done = True
            stop = max(stop, entry)
            if remaining <= 1e-9:
                return realized - FEE
        if tp1_done and not tp2_done and hi >= entry * tp2_at:
            realized += remaining * (entry * tp2_at * (1 - SLIP) / entry - 1)
            return realized - FEE
        peak = max(peak, hi)
        if tp1_done and remaining > 0 and cl <= peak * (1 - trail_pct):
            realized += remaining * (cl * (1 - SLIP) / entry - 1)
            return realized - FEE
    realized += remaining * (bars[min(len(bars) - 1, i + max_hold)][2] * (1 - SLIP) / entry - 1)
    return realized - FEE

def run(pools, pb, stop, ladder, reclaim=False, vol_k=0.0, trend=False, max_hold=24):
    tp1, f1, tp2, tr = ladder
    rets, per_pool = [], defaultdict(list)
    for name, bars in pools.items():
        highs = [b[0] for b in bars]; closes = [b[2] for b in bars]
        vols = [b[3] for b in bars]; e50 = ema(closes, 50)
        for i in range(50, len(bars) - 1):
            hi20 = max(highs[i - 20:i])
            if hi20 <= 0 or closes[i] > hi20 * (1 - pb):
                continue
            if reclaim and not (closes[i] > closes[i - 1]):
                continue
            if vol_k > 0:
                av = st.mean(vols[i - 20:i]) or 0
                if av <= 0 or vols[i] < vol_k * av:
                    continue
            if trend and closes[i] <= e50[i]:
                continue
            r = simulate(bars, i, stop, tp1, f1, tp2, tr, max_hold)
            rets.append(r); per_pool[name].append(r)
    if not rets:
        return None
    wins = [r for r in rets if r > 0]
    g = sum(r for r in rets if r > 0); l = -sum(r for r in rets if r <= 0)
    return dict(trades=len(rets), pools=sum(1 for v in per_pool.values() if v),
                win=len(wins) / len(rets), pf=(g / l if l > 0 else 99),
                avg=st.mean(rets) * 100)

LAD = {
    "quick 1.20/1.5 t15": (1.20, 0.50, 1.50, 0.15),
    "fast 1.25/1.6 t15":  (1.25, 0.50, 1.60, 0.15),
    "scalp 1.15 all":     (1.15, 1.00, 9.99, 0.10),
    "scalp 1.20 all":     (1.20, 1.00, 9.99, 0.10),
    "scalp 1.10 all":     (1.10, 1.00, 9.99, 0.08),
}

if __name__ == "__main__":
    pools = load()
    print(f"pools: {len(pools)} bars: {sum(len(b) for b in pools.values())}")
    rows = []
    combos = [
        ("baseline", dict()),
        ("+reclaim", dict(reclaim=True)),
        ("+vol1.5", dict(vol_k=1.5)),
        ("+trend", dict(trend=True)),
        ("+reclaim+trend", dict(reclaim=True, trend=True)),
        ("+reclaim+vol1.5", dict(reclaim=True, vol_k=1.5)),
    ]
    for pb in (0.15, 0.20, 0.25):
        for stop in (0.25, 0.30, 0.35):
            for lname, lad in LAD.items():
                for cname, kw in combos:
                    r = run(pools, pb, stop, lad, **kw)
                    if r and r["trades"] >= 25:
                        r.update(pb=pb, stop=stop, ladder=lname, filt=cname)
                        rows.append(r)
    rows.sort(key=lambda r: (-r["pf"], -r["win"]))
    print(f"\n=== TOP 20 by PF (n>=25) ===")
    print(f"{'PF':>5} {'win%':>6} {'n':>5} {'pools':>5} {'avg%':>7}  pb  stop  ladder                filter")
    for r in rows[:20]:
        print(f"{r['pf']:5.2f} {r['win']*100:6.1f} {r['trades']:5d} {r['pools']:5d} {r['avg']:7.2f}  "
              f"{int(r['pb']*100):>2}% {int(r['stop']*100):>2}%  {r['ladder']:<21} {r['filt']}")
    print(f"\n=== TOP 15 by WIN (PF>=1.0, n>=25) ===")
    c = [r for r in rows if r["pf"] >= 1.0]; c.sort(key=lambda r: -r["win"])
    for r in c[:15]:
        print(f"{r['pf']:5.2f} {r['win']*100:6.1f} {r['trades']:5d} {r['pools']:5d} {r['avg']:7.2f}  "
              f"{int(r['pb']*100):>2}% {int(r['stop']*100):>2}%  {r['ladder']:<21} {r['filt']}")
    json.dump(rows, open("research/refine_sweep.json", "w"), indent=1)
    print("\nwrote research/refine_sweep.json")
