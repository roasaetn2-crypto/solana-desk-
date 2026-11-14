#!/usr/bin/env python3
"""Shadow report: what would the pullback rule have done on *your* real trades?

The pullback package (buy only after a 20% dip from the 20-bar high, then run a
2x/3x ladder) measured PF 1.587 on 145 ungated signals. That is a real lift, but
it was measured on the *ungated* population. The live desk trades a gated
population where the existing ladder already wins ~40% of the time. Nobody knows
whether pullback still helps there.

Shadow mode answers that without risking a single dollar: every BUY the desk
takes is tagged with what pullback *would* have decided (`shadow_block` true or
false), and when the position closes the realised P&L is journaled as a
`shadow_pnl` row. Nothing is blocked. After enough closed trades you have two
groups of real, fully-gated trades:

    blocked  -> trades pullback would have refused (it thought they were chases)
    allowed  -> trades pullback would have taken

If `allowed` beats `blocked` by the bar below, promote the rule to a real gate.
If it does not, delete the rule and stop thinking about it.

Usage:
    python scripts/shadow_report.py                 # reads ./journal.jsonl
    python scripts/shadow_report.py --journal path  # explicit journal
    python scripts/shadow_report.py --out foo.json  # write machine-readable copy

The bar is deliberately strict, matching the repo's gate discipline: a rule has
to earn its place on 20+ closed trades per group, a 10pp win-rate gap, and a
0.30 profit-factor gap. Anything less is noise dressed as a signal.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---- the verdict bar, stated before the data is read ----
MIN_TRADES = 20      # closed trades per group
MIN_WIN_GAP = 0.10   # 10 percentage points of win rate
MIN_PF_GAP = 0.30    # profit-factor gap


def load_rows(path: str | Path) -> list[dict]:
    """Read shadow_pnl rows out of a journal.jsonl. Missing file = no data."""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("kind") == "shadow_pnl":
                rows.append(row)
    return rows


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def agg(rows: list[dict]) -> dict:
    """Aggregate one group of closed shadow trades."""
    n = 0
    wins = 0
    gross_win = 0.0
    gross_loss = 0.0
    total = 0.0
    for r in rows:
        pnl = _num(r.get("pnl_usd"))
        if pnl is None:
            continue
        n += 1
        total += pnl
        if pnl > 0:
            wins += 1
            gross_win += pnl
        elif pnl < 0:
            gross_loss += -pnl
    if gross_loss > 0:
        pf = gross_win / gross_loss
    else:
        pf = float("inf") if gross_win > 0 else 0.0
    return {
        "trades": n,
        "wins": wins,
        "win_rate": (wins / n) if n else 0.0,
        "avg_pnl": (total / n) if n else 0.0,
        "total_pnl": total,
        "profit_factor": pf,
    }


def verdict(blocked: dict, allowed: dict) -> str:
    """The three honest outcomes. Never a fourth, softer one."""
    if blocked["trades"] < MIN_TRADES or allowed["trades"] < MIN_TRADES:
        need = MIN_TRADES
        return (
            f"KEEP SHADOWING (need {need}+ closed trades per group; "
            f"have blocked={blocked['trades']} allowed={allowed['trades']})"
        )
    win_gap = allowed["win_rate"] - blocked["win_rate"]
    pf_gap = allowed["profit_factor"] - blocked["profit_factor"]
    if win_gap >= MIN_WIN_GAP and pf_gap >= MIN_PF_GAP:
        return "PROMOTE TO GATE (set MEME['pullback_entry'] = True)"
    if win_gap <= 0 and pf_gap <= 0:
        return "DELETE THE RULE (blocked trades were no worse than allowed ones)"
    return (
        f"KEEP SHADOWING (gap not decisive: win {win_gap:+.1%}, "
        f"PF {pf_gap:+.2f})"
    )


def report(rows: list[dict], source: str = "journal.jsonl") -> dict:
    blocked_rows = [r for r in rows if r.get("shadow_block") is True]
    allowed_rows = [r for r in rows if r.get("shadow_block") is False]
    blocked = agg(blocked_rows)
    allowed = agg(allowed_rows)
    v = verdict(blocked, allowed)

    print("=" * 68)
    print("PULLBACK SHADOW REPORT")
    print(f"source: {source}")
    print(f"closed shadow trades: {len(blocked_rows) + len(allowed_rows)}")
    print("=" * 68)
    print(f"{'group':<10}{'trades':>8}{'win%':>9}{'avg$':>10}{'total$':>11}{'PF':>8}")
    for name, a in (("blocked", blocked), ("allowed", allowed)):
        pf = "inf" if a["profit_factor"] == float("inf") else f"{a['profit_factor']:.3f}"
        print(f"{name:<10}{a['trades']:>8}{a['win_rate']*100:>8.1f}%"
              f"{a['avg_pnl']:>10.2f}{a['total_pnl']:>11.2f}{pf:>8}")
    print("-" * 68)
    print(f"VERDICT: {v}")
    print("=" * 68)
    if blocked["trades"] + allowed["trades"] == 0:
        print("\nNo shadow rows yet. The desk writes one per closed BUY once")
        print("MEME['pullback_shadow'] is on and a position closes. Let it run.")

    return {
        "source": str(source),
        "bar": {"min_trades": MIN_TRADES, "min_win_gap": MIN_WIN_GAP,
                "min_pf_gap": MIN_PF_GAP},
        "blocked": blocked,
        "allowed": allowed,
        "verdict": v,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Report on pullback shadow trades.")
    ap.add_argument("--journal", default=str(ROOT / "journal.jsonl"),
                    help="path to journal.jsonl (default: repo root)")
    ap.add_argument("--out", default=str(ROOT / "shadow_report.json"),
                    help="where to write the machine-readable summary")
    args = ap.parse_args(argv)

    rows = load_rows(args.journal)
    out = report(rows, source=args.journal)
    try:
        Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    except Exception as e:
        print(f"\n[shadow_report] could not write {args.out}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
