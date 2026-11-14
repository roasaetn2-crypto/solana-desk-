"""Why the desk says "book is over its risk budget" while it is winning.

Run on the phone:  python scripts/risk_budget_paradox.py

Reads the live paper ledger if it can, otherwise demonstrates on a synthetic
book. Prints, per position, the risk the gate THINKS is open and the loss the
position would actually take if its stop filled.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk.risk import Account, open_risk_usd, risk_budget_usd  # noqa: E402
from config import EXITS, RISK  # noqa: E402


def entry_risk(positions: dict) -> float:
    """What the book actually loses if every stop fills: qty x (entry - stop)."""
    total = 0.0
    for p in (positions or {}).values():
        qty, entry, stop = (float(p.get(k) or 0.0) for k in ("qty", "entry", "stop_price"))
        if qty > 0 and entry > 0 and stop > 0:
            total += qty * max(0.0, entry - stop)
    return round(total, 4)


def load_positions() -> tuple[dict, float]:
    from desk.ops import data_file
    p = data_file("paper_ledger.json")
    try:
        s = json.loads(p.read_text())
        return (s.get("positions") or {}), float(s.get("day_start") or 0.0)
    except Exception:
        return {}, 0.0


def show(positions: dict, day_start: float) -> None:
    acct = Account(equity=day_start, day_pnl=0.0, open_positions=len(positions),
                   realized_equity=day_start, day_start_equity=day_start)
    budget = risk_budget_usd(acct)
    mark = open_risk_usd(positions)
    ent = entry_risk(positions)
    print(f"budget  ${budget:.2f}   ({RISK['max_daily_loss_pct']:.0%} of day-start ${day_start:.2f})")
    print(f"gate    ${mark:.2f}   <- qty x (current price - stop)")
    print(f"actual  ${ent:.2f}   <- qty x (entry - stop), what a stop-out really costs")
    print()
    print(f"{'sym':10} {'cost':>6} {'entry':>11} {'last':>11} {'stop':>11} {'move':>7} "
          f"{'gate':>7} {'actual':>7}")
    for p in positions.values():
        qty = float(p.get("qty") or 0.0)
        e, l, s = (float(p.get(k) or 0.0) for k in ("entry", "last", "stop_price"))
        gate = qty * (l - s) if (qty > 0 and l > 0 and 0 < s < l) else 0.0
        actual = qty * max(0.0, e - s) if (qty > 0 and e > 0 and s > 0) else 0.0
        move = (l / e - 1) * 100 if e else 0.0
        print(f"{str(p.get('symbol'))[:10]:10} {float(p.get('cost') or 0):6.2f} {e:11.6g} {l:11.6g} "
              f"{s:11.6g} {move:6.1f}% {gate:7.3f} {actual:7.3f}")
    print()
    if mark > budget:
        print(f"VERDICT: the gate refuses new entries because ${mark:.2f} > ${budget:.2f}.")
        if ent <= budget:
            print(f"         ...but the book's real stop-out risk is ${ent:.2f}, which FITS.")
            print("         The overrun is unrealised profit being counted as risk.")
        else:
            print(f"         The book really is over budget (${ent:.2f} of stop-out risk).")
    else:
        print("VERDICT: inside budget; new entries are allowed.")


def synthetic() -> None:
    def pos(entry, last, stop_pct=EXITS["stop_pct"], ticket=1.25):
        return {"qty": ticket / entry, "entry": entry, "last": last,
                "stop_price": entry * (1 - stop_pct), "cost": ticket}

    print("No ledger found; demonstrating on a synthetic book.\n")
    for label, book in [
        ("flat", {"A": pos(1.0, 1.0), "B": pos(1.0, 1.0)}),
        ("one winner +27%", {"A": pos(1.0, 1.27), "B": pos(1.0, 1.0)}),
        ("one winner +100%", {"A": pos(1.0, 2.00), "B": pos(1.0, 1.0)}),
        ("one winner +300%", {"A": pos(1.0, 4.00), "B": pos(1.0, 1.0)}),
    ]:
        print(f"--- {label} ---")
        show(book, 50.33)
        print()


if __name__ == "__main__":
    positions, day_start = load_positions()
    if positions and day_start > 0:
        show(positions, day_start)
    else:
        synthetic()
