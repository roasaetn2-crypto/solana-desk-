"""The $20-book sizing these older tests were written for.

The 2026-11-12 retune moved the default risk per trade to 1.5% and max open positions to 6.
On a $20 book 1.5% risk makes a $1.50 ticket, under the $2 paper fee floor, so tests that
exercise entries pin the pre-retune sizing explicitly instead of relying on config defaults.

The bankroll is pinned here too. These tests hardcode dollar tickets computed from a $20
book ("risk 20*5%/30% = 3.33"), so changing PAPER["starting_equity_usd"] in config must not
silently rewrite what they are asserting.
"""
from unittest import mock

LEGACY_RISK = {"max_risk_per_trade": 0.05, "max_open_positions": 4}
LEGACY_PAPER = {"starting_equity_usd": 20}


def legacy_book(cls):
    """Class decorator: run every test with the pre-retune sizing and the $20 book."""
    return mock.patch.dict("config.PAPER", LEGACY_PAPER)(
        mock.patch.dict("config.RISK", LEGACY_RISK)(cls))
