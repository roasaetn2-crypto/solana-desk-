"""Caveat A: validate the WR-60 vs WR-65 package on a WIDE 5m candle set.

Reuses the exact simulate()/run() from refine_sweep.py so the numbers are
directly comparable to the earlier 10-pool result. Adds:
  - A/B of the two shipped presets on identical entries/exits
  - leave-one-pool-out band for each
  - volume-gate k sensitivity (1.0 .. 3.0)
  - per-pool breakdown so one lucky pool can't carry the result
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refine_sweep import run  # noqa: E402

WIDE = "data/g87e_wide/*_5m.json"

# the exact shipped packages
WR60 = dict(pb=0.15, stop=0.30, ladder=(1.15, 1.00, 9.99, 0.10), vol_k=0.0)
WR65 = dict(pb=0.15, stop=0.30, ladder=(1.15, 1.00, 9.99, 0.10), vol_k=1.5)


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


def lopo(pools, cfg):
    """Leave-one-pool-out: drop each pool and re-measure."""
    out = []
    for drop in list(pools):
        sub = {k: v for k, v in pools.items() if k != drop}
        r = run(sub, cfg["pb"], cfg["stop"], cfg["ladder"], vol_k=cfg["vol_k"])
        if r and r["trades"] >= 20:
            out.append(r)
    if not out:
        return None
    return dict(win_min=min(r["win"] for r in out), win_med=st.median([r["win"] for r in out]),
                pf_min=min(r["pf"] for r in out), pf_med=st.median([r["pf"] for r in out]),
                n=len(out))


def per_pool(pools, cfg):
    rows = []
    for name, bars in pools.items():
        r = run({name: bars}, cfg["pb"], cfg["stop"], cfg["ladder"], vol_k=cfg["vol_k"])
        if r and r["trades"] >= 3:
            rows.append((name, r))
    rows.sort(key=lambda x: -x[1]["win"])
    return rows


def show(tag, r):
    if not r:
        print(f"{tag:26s} no trades")
        return
    print(f"{tag:26s} n={r['trades']:5d} pools={r['pools']:3d} "
          f"win={r['win']*100:5.1f}%  PF={r['pf']:5.2f}  avg={r['avg']:+6.2f}%")


def main():
    pools = load_wide()
    bars = sum(len(b) for b in pools.values())
    print(f"WIDE SET: {len(pools)} pools, {bars:,} bars\n")

    print("=== A/B: identical entries/exits, volume gate on vs off ===")
    a = run(pools, 0.15, 0.30, (1.15, 1.00, 9.99, 0.10), vol_k=0.0)
    b = run(pools, 0.15, 0.30, (1.15, 1.00, 9.99, 0.10), vol_k=1.5)
    show("WR-60 (no vol gate)", a)
    show("WR-65 (vol >= 1.5x)", b)
    if a and b:
        print(f"{'delta':26s} win {(b['win']-a['win'])*100:+.1f}pp   PF {b['pf']-a['pf']:+.2f}   "
              f"trades {b['trades']-a['trades']:+d}")

    print("\n=== leave-one-pool-out ===")
    l60 = lopo(pools, WR60)
    l65 = lopo(pools, WR65)
    for tag, l in (("WR-60", l60), ("WR-65", l65)):
        if l:
            print(f"{tag:8s} worst win={l['win_min']*100:5.1f}%  median={l['win_med']*100:5.1f}%  "
                  f"| worst PF={l['pf_min']:4.2f} median={l['pf_med']:4.2f}  ({l['n']} folds)")

    print("\n=== volume-gate k sensitivity (same entries/exits) ===")
    ks = {}
    for k in (0.0, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0):
        r = run(pools, 0.15, 0.30, (1.15, 1.00, 9.99, 0.10), vol_k=k)
        show(f"k={k:.1f}", r)
        ks[str(k)] = r

    print("\n=== per-pool, WR-65 (top 10 / bottom 5) ===")
    rows = per_pool(pools, WR65)
    for name, r in rows[:10]:
        print(f"  {name[:22]:22s} n={r['trades']:3d} win={r['win']*100:5.1f}% PF={r['pf']:5.2f}")
    if len(rows) > 15:
        print("  ...")
        for name, r in rows[-5:]:
            print(f"  {name[:22]:22s} n={r['trades']:3d} win={r['win']*100:5.1f}% PF={r['pf']:5.2f}")
    print(f"\n  pools with >=3 trades: {len(rows)}")

    json.dump({"pools": len(pools), "bars": bars, "wr60": a, "wr65": b,
               "wr60_lopo": l60, "wr65_lopo": l65, "k_sens": ks,
               "per_pool_wr65": [{"pool": n, **r} for n, r in rows]},
              open("research/wide_validation.json", "w"), indent=1)
    print("\nwrote research/wide_validation.json")


if __name__ == "__main__":
    main()
