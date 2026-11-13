"""Regression tests for the paper "sizing deadlock".

The bug: the risk cap sizes a ticket as ``max_risk_per_trade * equity / stop_pct``.
The caution layer then halves it for a single flag (risk-off regime, security
flag, drawdown breaker, high Jupiter impact). On a $50 book the halved ticket
was $1.07-$1.88 -- under the $2 paper fee floor -- so *every* setup was rejected
while the desk still looked healthy and kept scanning.

These tests pin the invariant: the configured paper bankroll must be able to
produce a tradeable ticket after one caution cut, for every exit profile.
"""
from __future__ import annotations

import config
from config import EXITS, PAPER, RISK
from desk import risk
from desk.analyst import TokenThesis


def _thesis() -> TokenThesis:
    return TokenThesis(
        verdict="valid_setup", score=10, red_flags=[], green_flags=[],
        shape="crowd", invalidation="n/a", sources=[],
    )


def _profiles() -> dict[str, float]:
    prof = {k: v["stop_pct"] for k, v in EXITS.get("profiles", {}).items()}
    prof.setdefault("default", EXITS["stop_pct"])
    return prof


def test_min_viable_equity_matches_the_sizing_formula():
    for stop in _profiles().values():
        for mult in (1.0, 0.5, 0.25):
            need = risk.min_viable_equity(stop, mult)
            # at exactly `need`, the post-cut ticket is exactly the floor
            ticket = RISK["max_risk_per_trade"] * need / stop * mult
            assert abs(ticket - PAPER["min_ticket_usd"]) < 1e-6, (stop, mult, ticket)


def test_configured_bankroll_survives_one_caution_cut():
    """The actual bug. Fails on a $50 book, passes on $100."""
    equity = PAPER["starting_equity_usd"]
    for name, stop in _profiles().items():
        need = risk.min_viable_equity(stop, 0.5)
        assert equity >= need, (
            f"profile {name}: a ${equity:.2f} book needs ${need:.2f} to survive a "
            f"0.5x caution cut above the ${PAPER['min_ticket_usd']} floor"
        )


def test_risk_engine_approves_and_the_halved_ticket_still_clears_the_floor():
    equity = PAPER["starting_equity_usd"]
    acct = risk.Account(equity=equity, day_pnl=0.0, open_positions=0, realized_equity=equity)
    for name, stop in _profiles().items():
        ok, ticket, reason = risk.check_token_trade(_thesis(), acct, 10_517, stop_pct=stop)
        assert ok, f"{name}: risk engine rejected a clean setup: {reason}"
        assert ticket >= PAPER["min_ticket_usd"]
        halved = round(ticket * 0.5, 2)
        assert halved >= PAPER["min_ticket_usd"], (
            f"{name}: ${ticket} halved to ${halved}, under the ${PAPER['min_ticket_usd']} floor"
        )
