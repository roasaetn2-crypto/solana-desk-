#!/usr/bin/env python3
"""Explain (and optionally repair) a paper ledger whose equity looks impossible.

    python scripts/check_ledger.py                 # report only
    python scripts/check_ledger.py --fix           # repair in place, keep history
    python scripts/check_ledger.py --file live_ledger.json

Why this exists: on 2026-11-14 the dashboard showed

    equity  $549,195,436.20
    day P&L +$549,195,369.45   (+343,247,105.91%)
    multiple 3,432,471.477x

on a $160 paper bankroll. That is not a trade result. It is one position marked
from a quote that was not a price for that token (a decimals mismatch, a
different pool for the same mint, or a duplicated pair). The desk now refuses to
mark from such a print (desk/exits.py: plausible) and refuses to value a
position outside a sane band around its cost (desk/broker.py: _value), and
repairs an already-broken ledger on load (_repair). This script is the manual
view of the same thing: it names the offending mint so the fix is auditable
instead of a mystery.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def rows(s: dict) -> list[dict]:
    hi = float(config.PAPER.get("max_mark_mult", 1000.0))
    lo = float(config.PAPER.get("min_mark_mult", 0.001))
    out = []
    for mint, p in (s.get("positions") or {}).items():
        cost = float(p.get("cost") or 0.0)
        qty = float(p.get("qty") or 0.0)
        last = float(p.get("last") or 0.0)
        entry = float(p.get("entry") or 0.0)
        value = qty * last
        mult = (last / entry) if entry else 0.0
        bad = (not entry or not qty or last <= 0 or not (lo <= mult <= hi)
               or (cost and not (cost * lo <= value <= cost * hi)))
        out.append({"mint": mint, "symbol": p.get("symbol"), "cost": cost, "qty": qty,
                    "entry": entry, "last": last, "value": value, "mult": mult,
                    "bad": bad})
    return out


def report(path: Path, s: dict) -> int:
    start = s.get("start_equity") or config.PAPER["starting_equity_usd"]
    cash = float(s.get("cash") or 0.0)
    rs = rows(s)
    held = sum(r["value"] for r in rs)
    equity = cash + held
    print(f"file           {path}")
    print(f"start equity   ${start:,.2f}")
    print(f"cash           ${cash:,.2f}")
    print(f"open value     ${held:,.2f}")
    print(f"equity         ${equity:,.2f}")
    if start:
        print(f"multiple       {equity / start:,.3f}x")
        print(f"return         {(equity / start - 1) * 100:,.2f}%")
    day_start = s.get("day_start")
    if day_start is not None:
        print(f"day start      ${float(day_start):,.2f}   (equity when the day rolled)")
        print(f"day P&L        ${equity - float(day_start):,.2f}")
    print(f"open / closed  {len(s.get('positions') or {})} / {len(s.get('closed') or [])}")
    print()
    if not rs:
        print("no open positions")
    for r in sorted(rs, key=lambda x: -x["value"]):
        flag = "  <-- IMPOSSIBLE MARK" if r["bad"] else ""
        print(f"{str(r['symbol'])[:12]:12s} {r['mint'][:8]}  cost ${r['cost']:>8,.2f}  "
              f"qty {r['qty']:>18,.4f}  entry {r['entry']:.6g}  last {r['last']:.6g}  "
              f"value ${r['value']:>16,.2f}  {r['mult']:,.3f}x{flag}")
    bad = [r for r in rs if r["bad"]]
    print()
    if bad:
        print(f"{len(bad)} position(s) cannot be priced. Run with --fix to reset their mark to entry.")
    else:
        print("every mark is inside the sane band.")

    realized = sum(float(c.get("pnl_usd") or 0.0) for c in (s.get("closed") or []))
    open_cost = sum(float(p.get("cost") or 0.0) for p in (s.get("positions") or {}).values())
    open_proc = sum(float(p.get("proceeds") or 0.0) for p in (s.get("positions") or {}).values())
    expected = float(start) + realized + open_proc - open_cost
    tol = 0.02 + 0.01 * len(s.get("closed") or [])
    if abs(cash - expected) > tol:
        print(f"cash breaks double entry: ${cash:,.2f} vs expected ${expected:,.2f} "
              f"(closed P&L ${realized:,.2f}). Run with --fix.")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Report or repair an impossible paper ledger.")
    ap.add_argument("--file", default=None, help="ledger json (default: the desk's paper ledger)")
    ap.add_argument("--fix", action="store_true", help="repair the ledger in place")
    a = ap.parse_args()

    if a.file:
        path = Path(a.file)
    else:
        from desk.ops import data_file
        path = data_file("paper_ledger.json")
    if not path.exists():
        # A fresh install has no ledger yet: that is not an error, and with
        # --fix the right move is to create one at the configured bankroll.
        if a.fix:
            from desk.broker import PaperBroker
            b = PaperBroker(path)
            # PaperBroker builds the fresh state in memory but only writes when
            # something needs repairing, so persist it explicitly. Otherwise the
            # next boot still sees "no ledger" and the reset looks ignored.
            b._save()
            print(f"no ledger at {path} -- created a fresh book at ${b.equity():,.2f}")
            return 0
        print(f"no ledger at {path} (nothing to check yet)")
        return 0

    s = load(path)
    code = report(path, s)
    if not a.fix:
        return code

    from desk.broker import PaperBroker
    b = PaperBroker(path)
    # The constructor already repairs and saves, so its fixes are the real ones.
    # Calling _repair() again would return [] and print a misleading
    # "nothing to repair" even though the book was just healed.
    fixes = list(getattr(b, "last_fixes", []) or []) or b._repair()
    print()
    if fixes:
        for f in fixes:
            print(f"fixed {f['kind']:10s} {str(f.get('symbol') or f.get('mint') or '')[:12]:12s} "
                  f"{f.get('was')!r} -> {f.get('now')!r}")
        print(f"equity is now ${b.equity():,.2f}")
    else:
        print("nothing to repair")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
