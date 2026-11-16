#!/usr/bin/env python
"""Re-base the paper book's TOTAL equity to a number, WITHOUT closing any trade.

The book is double-entry (desk/broker.py enforces this on every boot):

    cash    = start + realized + open_proceeds - open_cost
    equity  = cash + held                      (held = sum(qty * last))
            = start + realized + unrealized    (unrealized = held - cost + proceeds)

Your example: total $10, buy a meme for $6 -> cash $4, held $6, equity $10.
Buying does not change equity; it only moves money from cash into a position.

So to make equity read $46 while keeping every position exactly as it is, the
ONLY thing that has to move is `start_equity` -- and `cash` then follows from the
identity above. Positions, their entry prices, their marks and all closed trades
are left untouched.

    python scripts/set_equity.py 46            # keep positions + marks, equity -> $46
    python scripts/set_equity.py 46 --dry      # show the plan, change nothing
    python scripts/set_equity.py 46 --flat     # also mark every position to entry
                                               # (zeroes unrealized P&L first)

It archives the ledger first (paper_ledger.<stamp>.json), so nothing is lost.
It never edits config.py and never deletes a position or a closed trade.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk.ops import data_file  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    flat = "--flat" in sys.argv

    if not args:
        print("usage: python scripts/set_equity.py <target_total_usd> [--dry] [--flat]")
        return 2
    try:
        target = float(args[0])
    except ValueError:
        print(f"FAIL: target must be a number, got {args[0]!r}")
        return 2
    if target <= 0:
        print("FAIL: target must be > 0")
        return 2

    path = data_file("paper_ledger.json")
    if not path.exists():
        print(f"FAIL: no ledger at {path}")
        return 1

    s = json.loads(path.read_text())
    positions = s.get("positions") or {}
    closed = s.get("closed") or []

    if flat:
        for p in positions.values():
            entry = float(p.get("entry") or 0.0)
            if entry > 0:
                p["last"] = entry

    cash = float(s.get("cash") or 0.0)
    cost = sum(float(p.get("cost") or 0.0) for p in positions.values())
    proceeds = sum(float(p.get("proceeds") or 0.0) for p in positions.values())
    held = sum(float(p.get("qty") or 0.0) * float(p.get("last") or p.get("entry") or 0.0)
               for p in positions.values())
    realized = sum(float(c.get("pnl_usd") or 0.0) for c in closed)
    unrealized = held - cost + proceeds
    total = round(cash + held, 2)
    start_old = float(s.get("start_equity") or 0.0)

    print("=== BEFORE ===")
    print(f"  ledger        {path}")
    print(f"  cash          ${cash:,.2f}")
    print(f"  held (open)   ${held:,.2f}   ({len(positions)} position(s))")
    print(f"  equity        ${total:,.2f}")
    print(f"  realized      ${realized:,.2f}   ({len(closed)} closed trade(s), kept)")
    print(f"  unrealized    ${unrealized:,.2f}")
    print(f"  start_equity  ${start_old:,.2f}")

    if abs(total - target) < 0.005:
        print(f"\nNothing to do: equity is already ${target:,.2f}")
        return 0

    # equity = start + realized + unrealized  =>  solve for start.
    new_start = round(target - realized - unrealized, 2)
    new_cash = round(new_start + realized + proceeds - cost, 2)
    new_total = round(new_cash + held, 2)

    print("\n=== AFTER ===")
    print(f"  cash          ${new_cash:,.2f}")
    print(f"  held (open)   ${held:,.2f}   ({len(positions)} position(s), untouched)")
    print(f"  equity        ${new_total:,.2f}   <- target ${target:,.2f}")
    print(f"  start_equity  ${new_start:,.2f}   (the only value that moves)")
    print(f"  realized      ${realized:,.2f}   (kept)")
    print(f"  unrealized    ${unrealized:,.2f}   (kept)")

    if new_cash < 0:
        print(f"\n  WARNING: cash would be NEGATIVE (${new_cash:,.2f}).")
        print("  That means the open positions are worth more than the whole book.")
        print("  Re-run with --flat to mark them to entry (zero unrealized) and try again.")
        if not dry:
            print("  Nothing written.")
            return 1
    if new_start <= 0:
        print(f"\n  WARNING: start_equity would be ${new_start:,.2f} (<= 0).")
        print("  The P&L% display divides by this. Use --flat, or pick a larger target.")

    if dry:
        print("\n--dry: nothing written.")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"paper_ledger.{stamp}.json")
    shutil.copy2(path, backup)
    print(f"\n  archived old ledger -> {backup}")

    s["cash"] = new_cash
    s["positions"] = positions
    s["start_equity"] = new_start
    s["day_start"] = new_start
    path.write_text(json.dumps(s, indent=2))
    print(f"  wrote {path}")
    print(f"\nDONE: equity is now ${new_total:,.2f} "
          f"({len(positions)} open position(s) and {len(closed)} closed trade(s) preserved).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
