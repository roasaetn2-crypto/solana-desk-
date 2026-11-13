"""Whole-book risk budget: the daily stop must cover every open stop at once.

The bug these lock down (seen live as "AMC: risk engine said no (open risk $2.14
(open $1.89 + new $0.25) would exceed the $1.54 daily stop)") was a deadlock:
once open risk drifted above the budget, EVERY subsequent entry was refused, so
the desk looked alive and never traded again. Three separate faults produced it:

1. The gate refused on a few cents of overrun instead of shrinking the ticket.
2. The budget was measured against live equity, so it shrank as open positions
   went under water -- locking the desk out exactly when it was already losing.
3. The first position was exempt (`open_risk > 0`), so one oversized ticket
   could put the book over budget with nothing to check it.
"""
from __future__ import annotations

import pytest

from desk import risk
from desk.risk import (Account, fit_risk_budget, per_trade_risk_usd,
                       risk_budget_usd)


def _acct(**kw) -> Account:
    base = dict(equity=51.33, day_pnl=-0.40, open_positions=3,
                realized_equity=51.33, cash=20.0, day_start_equity=51.33)
    base.update(kw)
    return Account(**base)


# --------------------------------------------------------------------------
# budget basis: stable, not mark-to-market
# --------------------------------------------------------------------------

def test_budget_uses_day_start_not_live_equity():
    """A book that fell during the day keeps its opening risk budget.

    This is the procyclical lock: with live equity the budget shrinks as marks
    drop, so a losing desk refuses new entries precisely when it should still be
    allowed to work. The budget must be anchored to the day's opening equity.
    """
    a = _acct(equity=40.0, day_start_equity=51.33)   # down ~22% intraday
    assert risk_budget_usd(a) == pytest.approx(0.03 * 51.33, abs=1e-9)
    # the old basis would have been much smaller
    assert risk_budget_usd(a) > risk.daily_loss_limit_usd(a)


def test_budget_falls_back_to_equity_when_day_start_missing():
    a = _acct(equity=50.0, day_start_equity=None)
    assert risk_budget_usd(a) == pytest.approx(0.03 * 50.0, abs=1e-9)


def test_hard_dollar_stop_wins_over_percentage():
    a = _acct()
    old = risk.RISK.get("max_daily_loss_usd")
    try:
        risk.RISK["max_daily_loss_usd"] = 2.50
        assert risk_budget_usd(a) == pytest.approx(2.50)
    finally:
        risk.RISK["max_daily_loss_usd"] = old


# --------------------------------------------------------------------------
# shrink, don't refuse
# --------------------------------------------------------------------------

def test_ticket_is_trimmed_to_fit_the_remaining_budget():
    """The exact shape of the live bug: a small overrun must trim, not brick.

    open $1.40 of a $1.54 budget leaves $0.14 of room. A $4.17 ticket at a 6%
    stop risks $0.25, which does not fit -- so the ticket shrinks to $2.33
    ($0.14 / 0.06) and the desk keeps trading.
    """
    a = _acct()
    ok, ticket, why = fit_risk_budget(1.40, 4.17, 0.06, a, 0.50)
    assert ok is True
    assert ticket == pytest.approx(2.33, abs=0.01)
    assert "trimmed" in why


def test_ticket_that_already_fits_is_untouched():
    a = _acct()
    ok, ticket, why = fit_risk_budget(0.0, 4.17, 0.06, a, 0.50)
    assert ok is True and ticket == 4.17 and why == "approved"


def test_first_position_is_left_to_the_sizing_layer():
    """An empty book is not trimmed by the budget.

    A single ticket is already capped by max_risk_per_trade. Trimming it here
    would also break the caution layer's relative cuts, because the cut happens
    BEFORE this check: a halved ticket could end up larger than a trimmed
    baseline. The budget governs stacking, not the first position.
    """
    a = _acct()
    ticket = 4.17
    ok, fitted, why = fit_risk_budget(0.0, ticket, 0.06, a, 0.50)
    assert ok is True and fitted == ticket and why == "approved"


# --------------------------------------------------------------------------
# the only two legitimate refusals
# --------------------------------------------------------------------------

def test_book_already_over_budget_refuses_with_actionable_message():
    a = _acct()
    ok, ticket, why = fit_risk_budget(1.89, 4.17, 0.06, a, 0.50)
    assert ok is False and ticket == 0.0
    assert "over its risk budget" in why
    assert "1.89" in why and "1.54" in why
    # the operator must be told the way out, not just that it failed
    assert "close" in why.lower() or "tighten" in why.lower()


def test_refuses_when_what_fits_is_below_the_fee_floor():
    a = _acct()
    # $0.03 of room at a 6% stop is a $0.50 ticket; force it under the floor
    ok, ticket, why = fit_risk_budget(1.53, 4.17, 0.06, a, 0.50)
    assert ok is False and ticket == 0.0
    assert "fee floor" in why


def test_zero_budget_disables_the_gate():
    """max_daily_loss_pct = 0 means "no daily stop", not "block everything"."""
    a = _acct()
    old = risk.RISK.get("max_daily_loss_pct")
    try:
        risk.RISK["max_daily_loss_pct"] = 0.0
        risk.RISK["max_daily_loss_usd"] = 0.0
        ok, ticket, why = fit_risk_budget(99.0, 4.17, 0.06, a, 0.50)
        assert ok is True and ticket == 4.17 and why == "approved"
    finally:
        risk.RISK["max_daily_loss_pct"] = old


def test_zero_stop_does_not_divide_by_zero():
    a = _acct()
    ok, ticket, why = fit_risk_budget(0.0, 4.17, 0.0, a, 0.50)
    assert ok is True and ticket == 4.17


# --------------------------------------------------------------------------
# optional budget split across slots
# --------------------------------------------------------------------------

def test_per_trade_risk_defaults_to_the_configured_cap():
    a = _acct()
    old = risk.RISK.get("risk_budget_split_across_slots")
    try:
        risk.RISK["risk_budget_split_across_slots"] = False
        assert per_trade_risk_usd(a) == pytest.approx(
            risk.RISK["max_risk_per_trade"] * risk.sizing_base(a))
    finally:
        risk.RISK["risk_budget_split_across_slots"] = old


def test_per_trade_risk_can_be_split_across_slots():
    """Opt-in: budget / max_open_positions, so 6 slots fit a 3% day.

    Needs a book big enough that the split is the binding term rather than the
    viability floor (on a $20 book with a 30% stop the floor wins).
    """
    a = _acct(equity=200.0, day_start_equity=200.0, realized_equity=200.0, cash=200.0)
    old = risk.RISK.get("risk_budget_split_across_slots")
    try:
        risk.RISK["risk_budget_split_across_slots"] = True
        slots = risk.RISK["max_open_positions"]
        assert per_trade_risk_usd(a) == pytest.approx(
            risk_budget_usd(a) / slots, abs=1e-9)
    finally:
        risk.RISK["risk_budget_split_across_slots"] = old


def test_split_never_starves_the_desk_below_the_fee_floor():
    """Splitting must not recreate the sizing deadlock on a small book.

    The invariant is that the split can only ever *lower* the per-trade cap, and
    never below the amount that keeps a caution-cut ticket above the fee floor.
    On a $20 book with a 30% stop the viability floor wins outright, so the split
    changes nothing -- which is the point.
    """
    a = _acct(equity=20.0, day_start_equity=20.0, realized_equity=20.0, cash=20.0)
    old = risk.RISK.get("risk_budget_split_across_slots")
    try:
        risk.RISK["risk_budget_split_across_slots"] = False
        unsplit = per_trade_risk_usd(a)
        risk.RISK["risk_budget_split_across_slots"] = True
        split = per_trade_risk_usd(a)
        assert split <= unsplit + 1e-9

        stop = risk.EXITS["stop_pct"]
        floor = risk.PAPER["min_ticket_usd"]
        viable = floor * stop / 0.25
        assert split >= min(unsplit, viable) - 1e-9
    finally:
        risk.RISK["risk_budget_split_across_slots"] = old


# --------------------------------------------------------------------------
# integration: the live agent path and the dashboard
# --------------------------------------------------------------------------

def _agent(tmpdir, positions=None, day_start=50.0, cash=50.0):
    import json
    import os
    from pathlib import Path
    from unittest import mock

    d = Path(tmpdir)
    ledger = d / "paper_ledger.json"
    ledger.write_text(json.dumps({"cash": cash, "positions": positions or {},
                                  "day": "2026-11-14", "day_start": day_start,
                                  "start_equity": day_start, "closed": []}))
    with mock.patch.dict(os.environ, {"DESK_DATA_DIR": str(d)}):
        from desk.solana_agent import SolanaAgent
        return SolanaAgent(ledger=ledger, bench_path=d / "b.db", live_ledger=d / "l.json")


def test_risk_status_reports_the_budget_and_open_risk(tmp_path):
    a = _agent(tmp_path)
    a.paper.buy("M1", "RUG", 3.0, 1.0, "scalp")
    a.paper.s["positions"]["M1"]["last"] = 1.0
    a.paper.s["positions"]["M1"]["stop_price"] = 0.9
    a.paper._save()

    pos = a.paper.s["positions"]["M1"]
    expected = pos["qty"] * (pos["last"] - pos["stop_price"])

    st = a.risk_status()
    assert st["risk_budget_usd"] == pytest.approx(0.03 * 50.0, abs=0.01)
    assert st["open_risk_usd"] == pytest.approx(expected, abs=0.01)
    assert st["over_risk_budget"] is False


def test_risk_status_flags_an_over_budget_book(tmp_path):
    """The desk must say out loud that it is over budget, not just go quiet."""
    a = _agent(tmp_path)
    a.paper.buy("M1", "RUG", 3.0, 1.0, "scalp")
    # qty 3 at last 1.00 with a stop at 0.00 risks $3.00 against a $1.50 budget
    a.paper.s["positions"]["M1"]["last"] = 1.0
    a.paper.s["positions"]["M1"]["stop_price"] = 0.01
    a.paper._save()

    pos = a.paper.s["positions"]["M1"]
    expected = pos["qty"] * (pos["last"] - pos["stop_price"])

    st = a.risk_status()
    assert st["open_risk_usd"] == pytest.approx(expected, abs=0.01)
    assert st["open_risk_usd"] > st["risk_budget_usd"]
    assert st["over_risk_budget"] is True


def test_paper_account_carries_the_day_start_equity(tmp_path):
    a = _agent(tmp_path, day_start=50.0)
    acct = a._paper_account()
    assert acct.day_start_equity == pytest.approx(50.0)
    assert risk_budget_usd(acct) == pytest.approx(1.50, abs=0.01)
