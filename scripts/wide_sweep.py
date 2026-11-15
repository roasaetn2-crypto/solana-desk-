"""Caveat A, part 2: what actually works on the WIDE memecoin set?

The shipped package (15% pullback, +15% TP, 30% stop) needs a 72.3% win rate to
break even after costs. On 70k bars of real memecoin 5m data it wins 46.5%, so
it loses. This sweep asks the obvious follow-up: is there a pullback package
whose reward:risk is not structurally doomed?

Sweeps pullback depth x stop x ladder x volume gate on the wide set and reports
everything, ranked by PF, with a minimum trade count so a 3-trade fluke cannot
top the table.
"""
from __future__ import annotations

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refine_sweep import run  # noqa: E402

WIDE = "data/g87e_wide/*_5m.json"
MIN_TRADES = 150


def load_wide():
    pools = {}
    for f in sorted(glob.glob(WIDE)):
        d = json.load(open(f))
        bars = []
        for c in (d.get("candles") or []):
            try:
                bars.append((float(c["high"]), float(c["low"]), float(c["close"]),
                             float(c.get("vol") or 0)))
            except (KeyError, TypeError, ValueError):
                pass
        if len(bars) >= 400:
            pools[d.get("label") or os.path.basename(f)] = bars
    return pools


# ladder: (tp1_at, tp1_frac, tp2_at, trail_pct)
LADDERS = {
    "single 1.15 all": (1.15, 1.00, 9.99, 0.10),   # the shipped one
    "single 1.25 all": (1.25, 1.00, 9.99, 0.12),
    "single 1.50 all": (1.50, 1.00, 9.99, 0.15),
    "single 2.00 all": (2.00, 1.00, 9.99, 0.20),
    "half 1.25 / 1.6": (1.25, 0.50, 1.60, 0.15),
    "half 1.5 / 2.0":  (1.50, 0.50, 2.00, 0.18),
    "half 2.0 / 3.0":  (2.00, 0.50, 3.00, 0.22),
    "half 2.0 / 4.0":  (2.00, 0.50, 4.00, 0.25),
}

STOPS = (0.08, 0.10, 0.12, 0.15, 0.20, 0.30)
PBS = (0.10, 0.15, 0.20, 0.25)
VOLS = (0.0, 1.5, 2.0)


def main():
    pools = load_wide()
    print(f"WIDE SET: {len(pools)} pools, {sum(len(b) for b in pools.values()):,} bars")
    print(f"minimum trades to rank: {MIN_TRADES}\n")

    rows = []
    for pb in PBS:
        for stop in STOPS:
            for lname, lad in LADDERS.items():
                for vk in VOLS:
                    r = run(pools, pb, stop, lad, vol_k=vk)
                    if r and r["trades"] >= MIN_TRADES:
                        r.update(pb=pb, stop=stop, ladder=lname, vol_k=vk)
                        rows.append(r)

    rows.sort(key=lambda r: -r["pf"])
    print(f"{'PF':>5} {'win%':>6} {'n':>6} {'pools':>5} {'avg%':>7}  pb   stop  vol   ladder")
    print("-" * 84)
    for r in rows[:25]:
        print(f"{r['pf']:5.2f} {r['win']*100:6.1f} {r['trades']:6d} {r['pools']:5d} "
              f"{r['avg']:7.2f}  {int(r['pb']*100):>2}%  {int(r['stop']*100):>2}%  "
              f"{r['vol_k']:.1f}  {r['ladder']}")

    print("\n=== best per stop level (PF) ===")
    for stop in STOPS:
        sub = [r for r in rows if r["stop"] == stop]
        if sub:
            b = max(sub, key=lambda r: r["pf"])
            print(f"stop {int(stop*100):>2}%: PF {b['pf']:5.2f}  win {b['win']*100:5.1f}%  "
                  f"n={b['trades']:5d}  pb {int(b['pb']*100)}%  vol {b['vol_k']:.1f}  {b['ladder']}")

    json.dump(rows, open("research/wide_sweep.json", "w"), indent=1)
    print("\nwrote research/wide_sweep.json")


if __name__ == "__main__":
    main()
