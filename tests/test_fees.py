"""The fee model: real Jupiter/Solana costs on both buy and sell.

Jupiter charges 0%. Every dollar is Solana's: base signature + capped priority,
priced off the live SOL spot. Paper must charge the same dollars, not a flat 1%.
"""
import tempfile
from pathlib import Path

import pytest

import config
from desk import fees
from desk.broker import PaperBroker, paper_side_fee


@pytest.fixture()
def pinned_sol(monkeypatch):
    monkeypatch.setitem(config.EXECUTION, "live_sol_price_usd", 100.0)
    return 100.0


def test_base_and_priority_are_lamport_priced(pinned_sol):
    # 5,000 lamports at $100/SOL
    assert fees.base_fee_usd() == pytest.approx(5000 / 1e9 * 100.0)
    # 200,000 lamports at $100/SOL
    assert fees.priority_fee_usd() == pytest.approx(200_000 / 1e9 * 100.0)


def test_rent_is_the_ata_deposit(pinned_sol):
    assert fees.ata_rent_usd() == pytest.approx(2_039_280 / 1e9 * 100.0)


def test_round_trip_is_base_plus_priority_both_sides_plus_spread(pinned_sol):
    rt = fees.round_trip_usd(10.0)
    # two signatures: (5,000 + 200,000) x 2 = 410,000 lamports = $0.041
    # one ATA deposit: 2,039,280 lamports = $0.203928
    # spread: 10 x (300bps / 2) x 2 sides = $0.30
    expected = (410_000 + 2_039_280) / 1e9 * 100.0 + 0.30
    assert rt["total_usd"] == pytest.approx(expected, abs=1e-4)


def test_paper_side_fee_is_flat_and_charges_no_rent(pinned_sol):
    """Paper charges a flat $0.10 per side. Rent is refundable on a full exit and
    network/priority are folded into the flat fee, so none is added on top."""
    for first in (True, False):
        fm = paper_side_fee(2.0, first_buy=first)
        assert fm["rent_usd"] == 0.0
        assert fm["total_usd"] == pytest.approx(0.10)
    assert config.PAPER["fee_per_side_usd"] == 0.10


def test_paper_side_fee_never_exceeds_half_the_ticket(pinned_sol):
    fm = paper_side_fee(0.12, first_buy=True)
    assert fm["total_usd"] == pytest.approx(0.06)


def test_legacy_flat_fee_still_works(monkeypatch):
    monkeypatch.setitem(config.PAPER, "use_live_fee_model", False)
    monkeypatch.setitem(config.PAPER, "fee_pct", 0.01)
    fm = paper_side_fee(4.0)
    assert fm["legacy"] is True
    assert fm["total_usd"] == pytest.approx(0.04)


def test_paper_fee_ignores_sol_price_and_lamport_inputs(monkeypatch):
    monkeypatch.setitem(config.EXECUTION, "live_sol_price_usd", 100.0)
    a = paper_side_fee(4.0)["total_usd"]
    monkeypatch.setitem(config.EXECUTION, "live_sol_price_usd", 400.0)
    monkeypatch.setitem(config.EXECUTION, "live_priority_fee_max_lamports", 20_000_000)
    assert paper_side_fee(4.0)["total_usd"] == a == pytest.approx(0.10)


def test_paper_fee_is_one_config_constant(monkeypatch):
    monkeypatch.setitem(config.PAPER, "fee_per_side_usd", 0.25)
    assert paper_side_fee(4.0)["total_usd"] == pytest.approx(0.25)


def test_a_two_dollar_buy_puts_1_90_in_the_token_and_exit_deducts_0_10(pinned_sol):
    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(Path(d) / "paper_ledger.json")
        b.s["cash"] = 50.0
        buy = b.buy("MINT1", "SYM", 2.0, 1.0)
        assert buy["fee"] == pytest.approx(0.10)
        pos = b.s["positions"]["MINT1"]
        # qty * fill is what reached the token: ticket - fee
        assert pos["qty"] * buy["fill"] == pytest.approx(1.90)
        assert b.s["cash"] == pytest.approx(48.0)
        gross_before = pos["qty"] * pos["entry"] * (1 - config.PAPER["slippage_pct"])
        cash_before = b.s["cash"]
        sell = b.sell("MINT1", 1.0, 1.0, "flat")
        assert sell["fee"] == pytest.approx(0.10)
        assert b.s["cash"] - cash_before == pytest.approx(gross_before - 0.10, abs=0.2)
        assert sell["fee_detail"]["rent_usd"] == 0.0


def test_snapshot_exposes_the_fee_model(pinned_sol):
    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(Path(d) / "paper_ledger.json")
        b.buy("MINT1", "SYM", 4.0, 1e-6)
        snap = b.snapshot()
        assert snap["fees"]["ticket_usd"] == pytest.approx(4.0)
        assert snap["fees"]["buy_fee_usd"] == pytest.approx(0.10)
        assert snap["fees"]["sell_fee_usd"] == pytest.approx(0.10)
        assert snap["fees"]["total_usd"] > 0


def test_fee_model_breakeven_at_1_and_6_dollar_tickets():
    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(Path(d) / "paper_ledger.json")
        for ticket in (1.0, 6.0):
            fm = b.fee_model(ticket)
            spread = ticket * 300 / 2 / 10_000
            assert fm["buy_fee_usd"] == fm["sell_fee_usd"] == pytest.approx(0.10)
            assert fm["fees_usd"] == pytest.approx(0.20)
            assert fm["spread_usd"] == pytest.approx(spread, abs=1e-4)
            assert fm["total_usd"] == pytest.approx(0.20 + spread, abs=1e-4)
            assert fm["break_even_pct"] == pytest.approx((0.20 + spread) / ticket * 100, abs=0.01)
            assert fm["rent_usd"] == 0.0
        assert b.fee_model(1.0)["break_even_pct"] == pytest.approx(21.5, abs=0.01)
        assert b.fee_model(6.0)["break_even_pct"] == pytest.approx(4.83, abs=0.01)


def test_paper_min_ticket_setting_allows_one_dollar():
    from desk import settings
    item = settings.REGISTRY["paper.min_ticket_usd"]
    assert item.lo <= 1.0 <= item.hi


def test_live_fee_lookup_falls_back_to_the_model(monkeypatch):
    """A getTransaction miss must not report a decorative zero."""
    from desk import live as live_mod
    monkeypatch.setitem(config.EXECUTION, "live_sol_price_usd", 100.0)
    b = live_mod.LiveBroker.__new__(live_mod.LiveBroker)
    monkeypatch.setattr(b, "_rpc", lambda m, p: {"meta": {}})
    expected = fees.base_fee_usd() + fees.priority_fee_usd()
    assert b._fee_usd("sig") == pytest.approx(expected, abs=1e-6)
