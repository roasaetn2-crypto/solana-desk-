"""Is the risk-budget fix live? One command, no guesswork.

Run from the repo root:

    python scripts/verify_risk_fix.py

It prints PASS/FAIL for the things that were broken, using the SAME functions
the running desk uses (no re-implementation, no mocking):

  1. the budget is at least what the slots are allowed to deploy
  2. the split is on, so six slots fit the budget by construction
  3. the paper fee floor is low enough that a split ticket still clears it
  4. the exact screenshot state (open risk $2.62) now approves an entry
  5. six slots fill without a deadlock

Exit code 0 = fixed, 1 = still broken.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from desk.analyst import TokenThesis
from desk.risk import (Account, check_token_trade, fit_risk_budget,
                       per_trade_risk_usd, risk_budget_usd)

GREEN, RED, DIM, END = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
_fails = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _fails
    if not ok:
        _fails += 1
    print(f"  {GREEN}PASS{END}  {label}" if ok else f"  {RED}FAIL{END}  {label}")
    if detail:
        print(f"        {DIM}{detail}{END}")


def thesis() -> TokenThesis:
    return TokenThesis(verdict="valid_setup", score=8, red_flags=[], green_flags=[],
                       shape="crowd", invalidation="n/a", sources=[])


def main() -> int:
    stop = config.EXITS["stop_pct"]
    slots = config.RISK["max_open_positions"]
    acct = Account(equity=50.90, day_pnl=0.90, open_positions=3,
                   realized_equity=50.90, cash=20.0, day_start_equity=50.00)

    print("\nSolana Desk \u2014 risk-budget fix verifier")
    print("=" * 52)
    print(f"  book ${acct.day_start_equity:.2f} | stop {stop:.0%} | slots {slots} | "
          f"risk/trade {config.RISK['max_risk_per_trade']:.1%}")

    budget = risk_budget_usd(acct)
    per_trade = per_trade_risk_usd(acct)

    print("\n1. Budget covers every slot")
    check(f"budget ${budget:.2f} >= {slots} x per-trade risk ${per_trade:.2f}",
          budget + 1e-9 >= per_trade * slots,
          f"max(daily stop, risk/trade x book x slots) = ${budget:.2f}")

    print("\n2. Split across slots is on")
    check("risk_budget_split_across_slots is True",
          config.RISK["risk_budget_split_across_slots"] is True,
          "per-trade risk = budget / slots")

    print("\n3. Paper fee floor lets a split ticket through")
    check(f"paper floor ${config.PAPER['min_ticket_usd']:.2f} <= $0.20",
          config.PAPER["min_ticket_usd"] <= 0.20,
          "worst caution cut (x0.25) must stay above the floor")

    print("\n4. The screenshot state now trades")
    ok, ticket, why = check_token_trade(thesis(), acct, 60_000, stop_pct=stop)
    check(f"open risk $2.62 vs ${budget:.2f} budget -> entry approved",
          ok and ticket > 0, f"ticket ${ticket:.2f} ({why})")
    ok2, fitted, why2 = fit_risk_budget(2.62, ticket, stop, acct,
                                        config.PAPER["min_ticket_usd"])
    check("whole-book gate accepts it too", ok2, f"${fitted:.2f} ({why2})")

    print("\n5. Six slots fill without a deadlock")
    a2 = Account(equity=50.0, day_pnl=0.0, open_positions=0, realized_equity=50.0,
                 cash=50.0, day_start_equity=50.0)
    risk, filled = 0.0, 0
    for _ in range(slots):
        o, t, _w = check_token_trade(thesis(), a2, 100_000, stop_pct=stop)
        o2, t2, _w2 = fit_risk_budget(risk, t, stop, a2, config.PAPER["min_ticket_usd"])
        if not (o and o2):
            break
        risk += t2 * stop
        filled += 1
    check(f"{filled}/{slots} slots opened, open risk ${risk:.2f} <= ${risk_budget_usd(a2):.2f}",
          filled == slots and risk <= risk_budget_usd(a2) + 1e-6)

    print("\n6. Off switch works, daily stop stays armed")
    old_enforce = config.RISK.get("enforce_risk_budget")
    try:
        config.RISK["enforce_risk_budget"] = False
        o, t, w = fit_risk_budget(99.0, 2.5, stop, acct, config.PAPER["min_ticket_usd"])
        check("gate off -> over-budget entry approved", o and t > 0, w)
        check("daily loss stop still configured",
              config.RISK["max_daily_loss_pct"] > 0 and config.RISK["flatten_on_daily_stop"])
    finally:
        config.RISK["enforce_risk_budget"] = old_enforce

    print("=" * 52)
    if _fails:
        print(f"{RED}{_fails} check(s) failed \u2014 the fix is NOT live.{END}\n")
        return 1
    print(f"{GREEN}All checks passed \u2014 the desk can trade.{END}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
