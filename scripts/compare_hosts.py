#!/usr/bin/env python3
"""Why the same settings made $98.89 on Termux and $52.94 on Render.

Same preset, same start ($50), same exit ladder -- and a 16.6x difference in
profit. This script separates the two things people conflate:

  1. WIN RATE -- statistically indistinguishable between the two books.
  2. MONEY    -- driven by average winner size and by compounding.

Run:  python scripts/compare_hosts.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# From the two screenshots, 2026-11-16 18:09.
BOOKS = {
    "termux": {"equity": 98.89, "pnl": 48.89, "entries": 39, "closed": 30,
               "open": 9, "win": 0.47, "label": "Termux (24/7, persistent disk)"},
    "render": {"equity": 52.94, "pnl": 2.94, "entries": 35, "closed": 25,
               "open": 10, "win": 0.64, "label": "Render free (ephemeral disk)"},
}
START = 50.0


def two_proportion_z(p1: float, n1: int, p2: float, n2: int) -> tuple[float, float]:
    """z and two-sided p for a difference in win rate."""
    pooled = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    # normal CDF via erf
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return z, p


def wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a win rate. The honest error bar."""
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def main() -> int:
    print("SAME SETTINGS, TWO HOSTS")
    print("=" * 72)
    print(f"{'host':<10}{'equity':>9}{'P&L':>9}{'%':>8}{'closed':>8}"
          f"{'win':>7}{'$/trade':>10}")
    print("-" * 72)
    for k, b in BOOKS.items():
        print(f"{k:<10}{b['equity']:>9.2f}{b['pnl']:>+9.2f}"
              f"{b['pnl'] / START * 100:>+7.2f}%{b['closed']:>8}"
              f"{b['win'] * 100:>6.0f}%{b['pnl'] / b['closed']:>+10.3f}")

    print()
    print("1. IS THE WIN RATE ACTUALLY DIFFERENT?")
    print("-" * 72)
    t, r = BOOKS["termux"], BOOKS["render"]
    for k, b in BOOKS.items():
        lo, hi = wilson(b["win"], b["closed"])
        print(f"  {k:<8} {b['win'] * 100:>3.0f}% on {b['closed']} trades  "
              f"95% CI {lo * 100:>4.1f}% - {hi * 100:>4.1f}%")
    z, p = two_proportion_z(t["win"], t["closed"], r["win"], r["closed"])
    print(f"  difference {abs(t['win'] - r['win']) * 100:.0f} pts, z = {z:+.2f}, p = {p:.2f}")
    print(f"  -> {'NOT significant' if p > 0.05 else 'significant'}. "
          "The confidence intervals overlap almost completely.")
    print("  A 47% and a 64% on 25-30 trades are the same coin, flipped twice.")

    print()
    print("2. THEN WHERE DID THE MONEY COME FROM?")
    print("-" * 72)
    print(f"  Termux made ${t['pnl'] / t['closed']:.3f} per closed trade.")
    print(f"  Render made ${r['pnl'] / r['closed']:.3f} per closed trade.")
    print(f"  That is a {(t['pnl'] / t['closed']) / (r['pnl'] / r['closed']):.1f}x "
          "difference with a LOWER win rate.")
    print("  So the average winner was far bigger, not more frequent. That is")
    print("  the trailing stop letting one or two runners run -- variance in")
    print("  which tokens each book happened to catch, not a settings difference.")

    print()
    print("3. AND THE COMPOUNDING LOOP")
    print("-" * 72)
    print("  ticket = max_risk_per_trade * equity / stop_pct")
    print(f"  Termux ticket is sized off ${t['equity']:.2f}; "
          f"Render off ${r['equity']:.2f}.")
    print(f"  Termux is therefore trading {t['equity'] / r['equity']:.2f}x the "
          "ticket size on every new entry.")
    print("  A book that wins compounds into bigger tickets, which win bigger.")
    print("  A book that resets never gets to compound at all.")

    print()
    print("4. THE PART THAT IS NOT LUCK")
    print("-" * 72)
    print("  Render free plan has NO DESK_DATA_DIR, NO Turso, NO disk:")
    print("    - paper_ledger.json sits on ephemeral disk -> wiped on redeploy")
    print("    - equity_curve.json is wiped too -> the chart resets every deploy")
    print("    - the free plan spins down after 15 min idle -> missed entries")
    print("      and missed exits, which is exactly how a 64% win rate still")
    print("      only makes 5.88%")
    print("  Termux keeps the ledger, the curve and the process alive 24/7.")
    print("  Same settings, but only one of them is actually running the desk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
