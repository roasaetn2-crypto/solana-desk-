"""Seat 4: the risk engine. Plain Python. No model touches this file. Ever.

It decides whether a trade happens and how big it is. It cannot be argued with,
which is the entire point.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import config
from config import EXITS, PAPER, RISK


@dataclass
class Account:
    equity: float
    day_pnl: float
    open_positions: int
    realized_equity: float | None = None   # MERGE: compounding base (start + closed P&L)


def in_quiet_hours(now: datetime | None = None) -> bool:
    """True when new entries are blocked.

    `quiet_hours` is a (start, end) pair of local hours. A wrapping window such
    as (22, 6) means 22:00 -> 06:00. An empty/equal pair means the window is
    disabled: the desk trades around the clock.

    This used to fall through to the wrap-around branch for (0, 0), which
    evaluated `h >= 0 or h < 0` -- always true -- so setting quiet hours to
    (0, 0) to turn them off actually blocked every entry, all day. That is why
    the desk looked alive and never traded.
    """
    window = RISK.get("quiet_hours")
    if not window:
        return False
    start, end = window
    if start == end:
        return False
    h = (now or datetime.now()).hour
    return start <= h < end if start < end else (h >= start or h < end)


def sizing_base(account: Account) -> float:
    """Equity that position size scales with. Compounding: realized profits are reinvested and
    realized losses shrink size. Unrealized gains are never sized against."""
    if RISK.get("compound") and account.realized_equity is not None:
        return max(0.0, account.realized_equity)
    return account.equity


DAILY_LOSS_REASON = "daily loss limit hit, desk halted until tomorrow"


def daily_loss_limit_usd(account: Account) -> float:
    """The daily stop in dollars.

    RISK["max_daily_loss_usd"] (0 = off) wins when it is set, so the operator can
    name the day's loss in cash instead of a percentage of a small paper book.
    Otherwise the stop is RISK["max_daily_loss_pct"] of the book.
    """
    usd_limit = float(RISK.get("max_daily_loss_usd") or 0.0)
    if usd_limit > 0:
        return usd_limit
    return RISK["max_daily_loss_pct"] * account.equity


def daily_loss_hit(account: Account) -> bool:
    """True once the day's realised loss reaches the daily stop.

    The limit lives in config.RISK and is never changed here. Either a hard
    dollar stop (RISK["max_daily_loss_usd"], 0 = disabled) or a percentage of
    the book (RISK["max_daily_loss_pct"]); the dollar stop takes precedence
    when set. Once hit, check_token_trade refuses every new entry until the
    day rolls over (or the operator uses the paper-only "continue today").
    """
    limit = daily_loss_limit_usd(account)
    if limit <= 0:
        return False
    return account.day_pnl <= -limit


def daily_override_active(mode: str | None, override_date: str | None, today: str) -> bool:
    """Operator 'Continue today' override. PAPER only, today only.

    It is ignored in any mode other than paper (LIVE can never use it), and it
    expires by itself at the day roll because the stored date stops matching.
    """
    if (mode or "").strip().lower() != "paper":
        return False
    return bool(override_date) and str(override_date) == str(today)


def check_token_trade(thesis, account: Account, liquidity_usd: float,
                      now: datetime | None = None,
                      stop_pct: float | None = None,
                      daily_override: bool = False) -> tuple[bool, float, str]:
    """Return (approved, ticket_usd, reason). stop_pct = the stop of the profile this trade will use.

    `daily_override` skips ONLY the daily-loss halt (paper, today, operator tapped
    Continue). Every other gate below still applies. check_live_trade has no
    such parameter: live can never bypass its daily loss limit.
    """
    if daily_loss_hit(account) and not daily_override:
        return False, 0.0, DAILY_LOSS_REASON
    if in_quiet_hours(now):
        return False, 0.0, "quiet hours, no new entries"
    if thesis.verdict != "valid_setup":
        return False, 0.0, f"analyst verdict: {thesis.verdict}"
    if thesis.score < config.MIN_SCORE:
        return False, 0.0, f"score {thesis.score} < {config.MIN_SCORE}"
    if account.open_positions >= RISK["max_open_positions"]:
        return False, 0.0, "max open positions"

    base, stop = sizing_base(account), (stop_pct or EXITS["stop_pct"])
    by_risk = RISK["max_risk_per_trade"] * base / stop
    by_size = RISK["max_position_pct"] * base
    by_pool = RISK["max_pool_share"] * liquidity_usd
    ticket = round(min(by_risk, by_size, by_pool), 2)
    floor = PAPER.get("min_ticket_usd", RISK["min_ticket_usd"])
    if ticket < floor:
        return False, 0.0, f"ticket ${ticket} below fee floor ${floor}"
    return True, ticket, "approved"


def sizing_breakdown(account: Account, liquidity_usd: float, stop_pct: float | None = None) -> dict:
    """The three caps, for the dashboard. The smallest one wins."""
    base, stop = sizing_base(account), (stop_pct or EXITS["stop_pct"])
    by_risk = RISK["max_risk_per_trade"] * base / stop
    by_size = RISK["max_position_pct"] * base
    by_pool = RISK["max_pool_share"] * liquidity_usd
    return {
        "by_risk": round(by_risk, 2),
        "by_size": round(by_size, 2),
        "by_pool": round(by_pool, 2),
        "ticket": round(min(by_risk, by_size, by_pool), 2),
    }


def check_live_trade(thesis, day_pnl: float, open_positions: int, liquidity_usd: float,
                     caps: dict, now: datetime | None = None) -> tuple[bool, float, str]:
    """The live version of seat 4. Same gates, but sized from the live caps.

    Paper sizes off a $20 book with ~$3-5 tickets. That is right for a
    simulator and wrong for real money, where the cap IS the bankroll. Here the
    ticket is the smallest of: the per-trade cap, what is left of the total
    notional cap, and 2% of the pool. The broker re-checks all of it before it
    builds a transaction, so this is the first of two locks, not the only one.
    """
    if day_pnl <= -caps["daily_loss_usd"]:
        return False, 0.0, (f"live daily loss limit hit (${day_pnl:.2f} of "
                            f"-${caps['daily_loss_usd']:.2f})")
    if in_quiet_hours(now):
        return False, 0.0, "quiet hours, no new entries"
    if thesis.verdict != "valid_setup":
        return False, 0.0, f"analyst verdict: {thesis.verdict}"
    if thesis.score < config.MIN_SCORE:
        return False, 0.0, f"score {thesis.score} < {config.MIN_SCORE}"
    if open_positions >= RISK["max_open_positions"]:
        return False, 0.0, "max open positions"

    remaining = max(0.0, caps["max_notional"] - caps.get("deployed", 0.0))
    by_pool = RISK["max_pool_share"] * liquidity_usd
    ticket = round(min(caps["max_trade"], remaining, by_pool), 2)
    if ticket < RISK["min_ticket_usd"]:
        return False, 0.0, f"ticket ${ticket} below fee floor ${RISK['min_ticket_usd']}"
    return True, ticket, "approved"


def live_sizing_breakdown(liquidity_usd: float, caps: dict) -> dict:
    remaining = max(0.0, caps["max_notional"] - caps.get("deployed", 0.0))
    by_pool = RISK["max_pool_share"] * liquidity_usd
    return {
        "by_risk": caps["max_trade"],
        "by_size": remaining,
        "by_pool": round(by_pool, 2),
        "ticket": round(min(caps["max_trade"], remaining, by_pool), 2),
    }
