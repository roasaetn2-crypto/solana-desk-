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


def test_configured_bankroll_survives_two_caution_cuts():
    """The actual bug, at the worst case the desk is meant to trade through.

    Two cuts multiply: risk-off regime (0.5) x a security halve-flag (0.5) = 0.25x.
    That is the realistic worst case, so the bankroll must clear the floor there.
    Fails on a $50 book ($0.94-$1.25) and on a $100 book ($1.25-$1.88).
    """
    equity = PAPER["starting_equity_usd"]
    for name, stop in _profiles().items():
        need = risk.min_viable_equity(stop, 0.25)
        assert equity >= need, (
            f"profile {name}: a ${equity:.2f} book needs ${need:.2f} to survive two "
            f"0.25x caution cuts above the ${PAPER['min_ticket_usd']} floor"
        )


def test_risk_engine_approves_and_the_halved_ticket_still_clears_the_floor():
    equity = PAPER["starting_equity_usd"]
    acct = risk.Account(equity=equity, day_pnl=0.0, open_positions=0, realized_equity=equity)
    for name, stop in _profiles().items():
        ok, ticket, reason = risk.check_token_trade(_thesis(), acct, 10_517, stop_pct=stop)
        assert ok, f"{name}: risk engine rejected a clean setup: {reason}"
        assert ticket >= PAPER["min_ticket_usd"]
        for mult in (0.5, 0.25):
            cut = round(ticket * mult, 2)
            assert cut >= PAPER["min_ticket_usd"], (
                f"{name}: ${ticket} at x{mult} is ${cut}, under the ${PAPER['min_ticket_usd']} floor"
            )


# ---------- paper cash cap: the book must never spend money it does not have ----------

def test_paper_cash_cap_blocks_a_ticket_larger_than_available_cash():
    acct = risk.Account(equity=100.0, day_pnl=0.0, open_positions=0,
                        realized_equity=100.0, cash=1.0)
    ok, ticket, reason = risk.check_token_trade(_thesis(), acct, 500_000, stop_pct=0.30)
    assert not ok
    assert "fee floor" in reason, reason


def test_paper_cash_cap_shrinks_the_ticket_to_the_cash_left():
    acct = risk.Account(equity=100.0, day_pnl=0.0, open_positions=5,
                        realized_equity=100.0, cash=3.0)
    ok, ticket, _ = risk.check_token_trade(_thesis(), acct, 500_000, stop_pct=0.30)
    assert ok and ticket == 3.0


def test_negative_paper_cash_is_refused_outright():
    acct = risk.Account(equity=100.0, day_pnl=0.0, open_positions=0,
                        realized_equity=100.0, cash=-50.0)
    ok, ticket, reason = risk.check_token_trade(_thesis(), acct, 500_000, stop_pct=0.30)
    assert not ok and ticket == 0.0
    assert "negative" in reason


def test_six_positions_at_max_size_cannot_drive_cash_negative():
    """The old bug: 6 x 25% of a $100 book = $150 spent on $100 of cash."""
    import tempfile
    from pathlib import Path
    from desk.broker import PaperBroker

    path = Path(tempfile.mkdtemp()) / "paper_ledger.json"
    book = PaperBroker(path)
    for i in range(8):
        acct = risk.Account(equity=book.equity(), day_pnl=book.day_pnl(),
                            open_positions=book.open_count(),
                            realized_equity=book.realized_equity(),
                            cash=book.s["cash"])
        ok, ticket, _ = risk.check_token_trade(_thesis(), acct, 500_000, stop_pct=0.30)
        if not ok:
            break
        book.buy(f"mint{i}", f"T{i}", ticket, 1.0)
    assert book.s["cash"] >= 0, f"paper cash went negative: {book.s['cash']}"


def test_ledger_keeps_its_own_basis_when_config_changes():
    """A Termux ledger written at $50 must not re-base itself on a $100 config."""
    import json, tempfile
    from pathlib import Path
    from desk.broker import PaperBroker

    path = Path(tempfile.mkdtemp()) / "paper_ledger.json"
    path.write_text(json.dumps({"cash": 40.0, "positions": {}, "day": "2026-01-01",
                                "day_start": 50.0, "closed": []}))
    book = PaperBroker(path)
    assert book.s["start_equity"] == 40.0, book.s["start_equity"]
    assert book.realized_equity() == 40.0


def test_snapshot_reports_ledger_basis_not_config_basis():
    """Termux: a persisted $50 book must report $50 to the dashboard, not config's $160.

    The broker already keeps its own `start_equity`, but the agent snapshot used
    to send config.PAPER["starting_equity_usd"] to the UI. On a flat $50 book that
    made the dashboard show the right equity next to a 0.31x multiple and a wrong
    day P&L percentage. The snapshot must carry the ledger's basis, and the broker
    snapshot must expose `day_start` so the daily % divides by the right number.
    """
    import json, os, tempfile
    from pathlib import Path
    from unittest import mock

    tmp = tempfile.TemporaryDirectory()
    d = Path(tmp.name)
    ledger = d / "paper_ledger.json"
    ledger.write_text(json.dumps({"cash": 50.0, "positions": {}, "day": "2026-01-01",
                                  "day_start": 50.0, "closed": []}))
    with mock.patch.dict(os.environ, {"DESK_DATA_DIR": str(d)}):
        from desk.solana_agent import SolanaAgent
        agent = SolanaAgent(ledger=ledger, bench_path=d / "b.db",
                            live_ledger=d / "l.json")
        snap = agent.status()
    try:
        assert snap["starting_equity"] == 50.0, snap["starting_equity"]
        assert snap["broker"]["day_start"] == 50.0, snap["broker"].get("day_start")
        # the multiple the dashboard draws is equity / starting_equity -> 1.00x, not 0.31x
        assert round(snap["broker"]["equity"] / snap["starting_equity"], 2) == 1.0
    finally:
        tmp.cleanup()


def test_position_card_never_shows_an_impossible_mark():
    """The card must agree with the equity: no 32-billion-percent positions.

    Reproduces the 2026-11-14 phone screenshot: MAYA bought at 8.85e-7, marked
    at 284.67 (a decimals/pool mismatch). `_value()` already kept equity sane,
    but the position row handed the raw `last` to the UI and computed pnl_pct
    from it, so the card read +32,156,727,091.40%. The row must report the
    clamped mark, and a long-running process must heal the ledger each cycle.
    """
    import json, tempfile
    from pathlib import Path
    from desk.broker import PaperBroker

    d = Path(tempfile.mkdtemp())
    path = d / "paper_ledger.json"
    path.write_text(json.dumps({"cash": 55.0, "positions": {}, "day": "2026-11-14",
                                "day_start": 55.0, "start_equity": 55.0, "closed": []}))
    book = PaperBroker(path)
    book.buy("ECguXxx", "MAYA", 1.76, 8.85e-7, "scalp")
    # a corrupt print lands mid-cycle (after boot repair already ran)
    book.s["positions"]["ECguXxx"]["last"] = 284.67
    book.s["positions"]["ECguXxx"]["peak"] = 284.67

    row = book.snapshot()["positions"][0]
    assert row["mark_clamped"] is True
    assert row["raw_last"] == 284.67
    # the card shows the sane mark, not the corrupt print
    assert row["last"] < 1e-5, row["last"]
    assert abs(row["pnl_pct"]) < 100, row["pnl_pct"]
    # and equity is unchanged by the bad print
    assert round(book.equity(), 2) == 55.0


def test_repair_heals_a_corrupt_mark_on_a_running_process():
    """_repair() is idempotent and fixes an impossible mark written after boot."""
    import json, tempfile
    from pathlib import Path
    from desk.broker import PaperBroker

    d = Path(tempfile.mkdtemp())
    path = d / "paper_ledger.json"
    path.write_text(json.dumps({"cash": 55.0, "positions": {}, "day": "2026-11-14",
                                "day_start": 55.0, "start_equity": 55.0, "closed": []}))
    book = PaperBroker(path)
    book.buy("ECguXxx", "MAYA", 1.76, 8.85e-7, "scalp")
    book.s["positions"]["ECguXxx"]["last"] = 284.67

    fixes = book._repair()
    assert any(f["kind"] == "mark" for f in fixes), fixes
    pos = book.s["positions"]["ECguXxx"]
    assert pos["last"] == pos["entry"], (pos["last"], pos["entry"])
    # idempotent: a clean book reports nothing to fix
    assert book._repair() == []
