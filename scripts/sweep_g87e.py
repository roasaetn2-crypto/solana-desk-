"""Parameter sweep for the fixed G87E, scored on median return across runs.

Median, not mean: one 1700% pump.fun run should not be allowed to make a bad
parameter set look good. A parameter set has to work on most pools, not on the
luckiest one.

    python3 scripts/sweep_g87e.py
"""
from __future__ import annotations

import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desk import g87e
from scripts import fetch_g87e_data as feed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "backtest_g87e_sweep.json")

GRID = dict(
    trail_arm_mult=[1.0, 1.5, 2.0],
    trail_offset_mult=[1.0, 2.0, 3.0],
    stop_mult=[1.5, 2.5],
    max_bars=[12, 24],
    long_only=[False, True],
    require_trend=[True, False],
)

FIXED = dict(
    atr_len=14, risk_pct=1.0, initial_capital=50.0, fee_pct=0.05,
    slippage_pct=0.30, cooldown_bars=3, notional_cap=0.90, atr_floor_pct=0.05,
    ema_len=50, compounding=True, variant="fixed",
)


def main() -> int:
    data = feed.load_all()
    runs = [(label, tf, c) for label, tfs in data.items() for tf, c in tfs.items()]
    print(f"{len(runs)} pool/timeframe runs")

    keys = list(GRID)
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    print(f"{len(combos)} parameter sets\n")

    results = []
    for n, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        cfg = g87e.Config(**{**FIXED, **params})
        rets, trades, dds = [], 0, []
        for label, tf, candles in runs:
            r = g87e.backtest(candles, cfg)
            s = r.stats()
            rets.append(s["return_pct"])
            trades += s["trades"]
            dds.append(s["max_dd_pct"])
        rets_sorted = sorted(rets)
        med = rets_sorted[len(rets_sorted) // 2]
        results.append({
            "params": params,
            "median_return_pct": round(med, 2),
            "mean_return_pct": round(sum(rets) / len(rets), 2),
            "worst_return_pct": round(min(rets), 2),
            "best_return_pct": round(max(rets), 2),
            "profitable_runs": sum(1 for r in rets if r > 0),
            "trades": trades,
            "avg_max_dd_pct": round(sum(dds) / len(dds), 2),
        })
        if n % 12 == 0:
            print(f"  {n}/{len(combos)} ...")

    results.sort(key=lambda r: (-r["median_return_pct"], -r["mean_return_pct"]))
    with open(OUT, "w") as f:
        json.dump(results, f, indent=1)

    print("\nTop 10 by median return:")
    print(f"{'med%':>8} {'mean%':>8} {'worst%':>8} {'prof':>5} {'tr':>6} {'dd%':>6}  params")
    for r in results[:10]:
        p = r["params"]
        print(f"{r['median_return_pct']:8.2f} {r['mean_return_pct']:8.2f} "
              f"{r['worst_return_pct']:8.2f} {r['profitable_runs']:5d} {r['trades']:6d} "
              f"{r['avg_max_dd_pct']:6.2f}  arm={p['trail_arm_mult']} off={p['trail_offset_mult']} "
              f"stop={p['stop_mult']} bars={p['max_bars']} longonly={p['long_only']} trend={p['require_trend']}")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
