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


def test_paper_side_fee_charges_no_rent(pinned_sol):
    """Rent is a refundable deposit, not a fee. Charging it to a $2 paper ticket
    would book a ~$0.20 loss on open and trip the daily stop."""
    fm = paper_side_fee(2.0, first_buy=True)
    assert fm["rent_usd"] == 0.0
    assert fm["total_usd"] == pytest.approx(fees.base_fee_usd() + fees.priority_fee_usd())


def test_paper_side_fee_never_exceeds_half_the_ticket(pinned_sol, monkeypatch):
    monkeypatch.setitem(config.EXECUTION, "live_priority_fee_max_lamports", 20_000_000)
    fm = paper_side_fee(0.20, first_buy=True)
    assert fm["total_usd"] <= 0.20 * 0.5 + 1e-9


def test_legacy_flat_fee_still_works(monkeypatch):
    monkeypatch.setitem(config.PAPER, "use_live_fee_model", False)
    monkeypatch.setitem(config.PAPER, "fee_pct", 0.01)
    fm = paper_side_fee(4.0)
    assert fm["legacy"] is True
    assert fm["total_usd"] == pytest.approx(0.04)


def test_paper_buy_and_sell_charge_the_real_fee(pinned_sol):
    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(Path(d) / "paper_ledger.json")
        buy = b.buy("MINT1", "SYM", 4.0, 1e-6)
        sell = b.sell("MINT1", 1.0, 1e-6, "flat")
        per_side = fees.base_fee_usd() + fees.priority_fee_usd()
        assert buy["fee"] == pytest.approx(per_side, abs=1e-6)
        assert sell["fee"] == pytest.approx(per_side, abs=1e-6)
        # the old flat 1% would have been $0.04 on the buy; the real fee differs
        assert buy["fee"] != pytest.approx(4.0 * 0.01)


def test_paper_fee_follows_the_sol_price(monkeypatch):
    monkeypatch.setitem(config.PAPER, "use_live_fee_model", True)
    monkeypatch.setitem(config.EXECUTION, "live_sol_price_usd", 100.0)
    cheap = paper_side_fee(4.0)["total_usd"]
    monkeypatch.setitem(config.EXECUTION, "live_sol_price_usd", 400.0)
    dear = paper_side_fee(4.0)["total_usd"]
    assert dear == pytest.approx(cheap * 4, rel=1e-6)


def test_snapshot_exposes_the_fee_model(pinned_sol):
    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(Path(d) / "paper_ledger.json")
        b.buy("MINT1", "SYM", 4.0, 1e-6)
        snap = b.snapshot()
        assert snap["fees"]["mode"] == "live"
        assert snap["fees"]["ticket_usd"] == pytest.approx(4.0)
        assert snap["fees"]["total_usd"] > 0


def test_live_fee_lookup_falls_back_to_the_model(monkeypatch):
    """A getTransaction miss must not report a decorative zero."""
    from desk import live as live_mod
    monkeypatch.setitem(config.EXECUTION, "live_sol_price_usd", 100.0)
    b = live_mod.LiveBroker.__new__(live_mod.LiveBroker)
    monkeypatch.setattr(b, "_rpc", lambda m, p: {"meta": {}})
    expected = fees.base_fee_usd() + fees.priority_fee_usd()
    assert b._fee_usd("sig") == pytest.approx(expected, abs=1e-6)
