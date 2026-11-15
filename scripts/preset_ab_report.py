#!/usr/bin/env python3
"""Preset A/B report: does WR-65 actually beat WR-60 on YOUR paper fills?

WR-60 and WR-65 were both measured on historical candles. A backtest is a
hypothesis; this script reads the realised P&L of real, fully-gated, fully-costed
paper trades and tells you whether the hypothesis survived contact.

Every BUY is tagged with the preset active at entry (desk/preset_ab.py), and
every close journals a `preset_pnl` row. This script groups those rows and
applies the same strict bar the repo already uses for the pullback shadow:

    20+ closed trades per group, a 10pp win-rate gap, and a 0.30 PF gap.

Anything less is noise dressed as a signal. The honest default is "keep
measuring", and WR-60 stays the baseline until WR-65 clears the bar.

Usage:
    python scripts/preset_ab_report.py
    python scripts/preset_ab_report.py --journal path/to/journal.jsonl
    python scripts/preset_ab_report.py --out research/preset_ab.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MIN_TRADES = 20      # closed trades per group
MIN_WIN_GAP = 0.10   # 10 percentage points
MIN_PF_GAP = 0.30    # profit-factor gap

LABEL = {"wr60": "WR-60 (no volume gate)", "wr65": "WR-65 (volume >= 1.5x)",
         "pf11": "PF-1.1 (deep dip, 2x/3x)",
         "other": "other / hand-tuned"}


def load_rows(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "preset_pnl" or r.get("event") == "preset_pnl":
                rows.append(r)
    return rows


def stats(rows: list[dict]) -> dict | None:
    pnls = []
    for r in rows:
        try:
            pnls.append(float(r.get("pnl_usd")))
        except (TypeError, ValueError):
            continue
    if not pnls:
        return None
    wins = [x for x in pnls if x > 0]
    gross_win = sum(wins)
    gross_loss = -sum(x for x in pnls if x <= 0)
    return {"trades": len(pnls), "wins": len(wins),
            "win": len(wins) / len(pnls),
            "pf": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "avg": sum(pnls) / len(pnls),
            "net": sum(pnls)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=str(ROOT / "journal.jsonl"))
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    rows = load_rows(a.journal)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r.get("preset") or "other"), []).append(r)

    print(f"journal: {a.journal}")
    print(f"preset_pnl rows: {len(rows)}\n")

    if not rows:
        print("No tagged closes yet. Apply WR-60 or WR-65, let the desk trade, and")
        print("re-run this once positions start closing. Nothing to compare today.")
        return 0

    res = {}
    print(f"{'group':26s} {'n':>5} {'win%':>6} {'PF':>6} {'avg$':>8} {'net$':>10}")
    for key in ("wr60", "wr65", "pf11", "other"):
        s = stats(groups.get(key, []))
        if not s:
            continue
        res[key] = s
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"{LABEL[key]:26s} {s['trades']:5d} {s['win']*100:6.1f} {pf:>6} "
              f"{s['avg']:8.2f} {s['net']:10.2f}")

    a60, a65 = res.get("wr60"), res.get("wr65")
    print()
    if not (a60 and a65):
        print("VERDICT: need closed trades in BOTH groups before comparing.")
        print("         Run WR-60 for a stretch, then WR-65 for a stretch.")
    else:
        win_gap = a65["win"] - a60["win"]
        pf_gap = (a65["pf"] - a60["pf"]) if a65["pf"] != float("inf") else float("inf")
        print(f"WR-65 minus WR-60: win {win_gap*100:+.1f}pp, PF {pf_gap:+.2f}")
        thin = a60["trades"] < MIN_TRADES or a65["trades"] < MIN_TRADES
        if thin:
            print(f"VERDICT: keep measuring. Need {MIN_TRADES}+ closes per group; "
                  f"have {a60['trades']} and {a65['trades']}.")
        elif win_gap >= MIN_WIN_GAP and pf_gap >= MIN_PF_GAP:
            print("VERDICT: WR-65 WINS. It cleared 10pp win and 0.30 PF on real fills.")
            print("         Promote WR-65 to the default and retire WR-60 to a button.")
        elif win_gap <= -MIN_WIN_GAP and pf_gap <= -MIN_PF_GAP:
            print("VERDICT: WR-60 WINS. The volume gate HURT on real fills.")
            print("         Demote WR-65: the backtest edge did not survive contact.")
        else:
            print("VERDICT: no meaningful difference yet. Keep both, keep measuring.")

    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        json.dump({"journal": a.journal, "groups": res,
                   "bar": {"min_trades": MIN_TRADES, "min_win_gap": MIN_WIN_GAP,
                           "min_pf_gap": MIN_PF_GAP}},
                  open(a.out, "w"), indent=1)
        print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
