"""Real Jupiter + Solana fee model: one source of truth for paper and live.

Jupiter charges 0%. A live quote comes back with ``platformFee: null`` and the
desk never sends ``platformFeeBps``, so every dollar of cost is Solana's:

    base      5,000 lamports per signature, one signature per swap
    priority  what you bid to land, capped by ``live_priority_fee_max_lamports``
    ATA rent  0.00203928 SOL to create a token account, once per new mint.
              This is a *deposit*, refundable when the account closes -- not a
              fee -- so it is priced into the live floor but never into the
              paper ledger's fee line.
    spread    price impact + the quote-vs-fill gap, modelled as half the
              slippage cap. Paper already pays this inside its fill price, so
              it is not charged again as a fee.

Paper charges the same dollars the chain would, so its P&L is comparable to a
live run instead of being shaped by an arbitrary flat 1%. Live reads the true
on-chain fee from the transaction meta and uses this model only to price the
floor before a ticket is placed.

The SOL price used to turn lamports into dollars is the pinned
``live_sol_price_usd`` if set, otherwise the cached live spot from
``desk.regime`` (refreshed by the agent loop), otherwise a deliberately
pessimistic fallback. Nothing here makes a network call on the hot path.
"""
from __future__ import annotations

from config import EXECUTION, PAPER

LAMPORTS_PER_SOL = 1_000_000_000


def sol_price_usd() -> float:
    """Dollars per SOL used to price lamport costs.

    Order of preference:
      1. ``live_sol_price_usd`` -- an operator pin, wins always
      2. the cached live spot from ``desk.regime`` (<= 1h old)
      3. ``live_sol_price_fallback_usd`` -- pessimistic on purpose

    Never fetched here: this runs inside the fill path and a network call in the
    hot path is how a fill turns into a timeout.
    """
    pinned = float(EXECUTION.get("live_sol_price_usd") or 0.0)
    if pinned > 0:
        return pinned
    try:
        from desk import regime  # lazy: no import cycle, no network
        live = regime.sol_price()
        if live and live > 0:
            return float(live)
    except Exception:
        pass
    return float(EXECUTION.get("live_sol_price_fallback_usd") or 150.0)


def _lamports(key: str) -> float:
    try:
        return float(EXECUTION.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def base_fee_usd(sol_usd: float | None = None) -> float:
    """One signature's base fee, in dollars (5,000 lamports by default)."""
    sol = sol_price_usd() if sol_usd is None else sol_usd
    return _lamports("live_base_fee_lamports") / LAMPORTS_PER_SOL * sol


def priority_fee_usd(sol_usd: float | None = None) -> float:
    """The priority-fee ceiling for one transaction, in dollars."""
    sol = sol_price_usd() if sol_usd is None else sol_usd
    return _lamports("live_priority_fee_max_lamports") / LAMPORTS_PER_SOL * sol


def ata_rent_usd(sol_usd: float | None = None) -> float:
    """The refundable ATA deposit for a new mint, in dollars."""
    sol = sol_price_usd() if sol_usd is None else sol_usd
    return _lamports("live_ata_rent_lamports") / LAMPORTS_PER_SOL * sol


def spread_usd(notional_usd: float, bps: float | None = None,
               sol_usd: float | None = None) -> float:
    """Half the slippage cap: the realistic mid-case price impact of a route."""
    if bps is None:
        bps = float(EXECUTION.get("live_max_slippage_bps") or 0)
    return max(0.0, float(notional_usd)) * (float(bps) / 2.0 / 10_000.0)


def side_fee_usd(notional_usd: float, *, first_buy: bool = False,
                 include_spread: bool = False,
                 include_rent: bool = True,
                 sol_usd: float | None = None) -> dict:
    """What one swap costs in dollars, itemised.

    ``include_spread`` is False for the paper ledger because the paper fill
    already pays slippage in its price; adding spread again would double-count.
    It is True when pricing the live floor, where the quote's impact is not yet
    known.

    ``include_rent`` prices the ATA deposit for a first buy of a new mint. It is
    a deposit, not a fee, so callers that want the pure fee can turn it off.
    """
    sol = sol_price_usd() if sol_usd is None else sol_usd
    base = base_fee_usd(sol)
    prio = priority_fee_usd(sol)
    rent = ata_rent_usd(sol) if (first_buy and include_rent) else 0.0
    spread = spread_usd(notional_usd, sol_usd=sol) if include_spread else 0.0
    total = base + prio + rent + spread
    return {
        "base_usd": round(base, 6),
        "priority_usd": round(prio, 6),
        "rent_usd": round(rent, 6),
        "spread_usd": round(spread, 6),
        "total_usd": round(total, 6),
        "sol_usd": round(sol, 2),
    }


def round_trip_usd(ticket_usd: float, *, first_buy: bool = True,
                   sol_usd: float | None = None) -> dict:
    """The full buy-then-sell bill for one ticket, in dollars and percent."""
    buy = side_fee_usd(ticket_usd, first_buy=first_buy, include_spread=True,
                       include_rent=True, sol_usd=sol_usd)
    sell = side_fee_usd(ticket_usd, first_buy=False, include_spread=True,
                        include_rent=False, sol_usd=sol_usd)
    total = buy["total_usd"] + sell["total_usd"]
    return {
        "ticket_usd": round(float(ticket_usd), 2),
        "buy_usd": round(buy["total_usd"], 4),
        "sell_usd": round(sell["total_usd"], 4),
        "total_usd": round(total, 4),
        "pct": round(total / ticket_usd * 100, 2) if ticket_usd > 0 else 0.0,
        "sol_usd": round(sol_usd if sol_usd is not None else sol_price_usd(), 2),
    }


def paper_fee_per_side_usd(notional_usd: float) -> float:
    """The flat paper fee for one swap: PAPER["fee_per_side_usd"] ($0.10).

    Never more than half the notional, so a tiny ticket cannot be eaten whole.
    """
    flat = max(0.0, float(PAPER.get("fee_per_side_usd") or 0.0))
    return min(flat, max(0.0, float(notional_usd)) * 0.5)
