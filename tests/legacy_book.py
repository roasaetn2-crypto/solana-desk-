"""The $20-book sizing these older tests were written for.

The 2026-11-12 retune moved the default risk per trade to 1.5% and max open positions to 6.
On a $20 book 1.5% risk makes a $1.50 ticket, under the $2 paper fee floor, so tests that
exercise entries pin the pre-retune sizing explicitly instead of relying on config defaults.
"""
from unittest import mock

LEGACY_RISK = {"max_risk_per_trade": 0.05, "max_open_positions": 4}


def legacy_book(cls):
    """Class decorator: run every test with the pre-retune sizing."""
    return mock.patch.dict("config.RISK", LEGACY_RISK)(cls)
