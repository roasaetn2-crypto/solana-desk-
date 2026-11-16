"""The chart must survive a restart.

Regression: equity_curve was two in-memory deques, so every restart wiped the
chart and the 1H/4H/1D/1W/ALL tabs had nothing to replay.
"""
from __future__ import annotations

import time
from collections import deque

import pytest

from desk import equity_curve as ec


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ec, "_last_sync", [time.time()])   # never hit the network
    return tmp_path


def _deques():
    return deque(maxlen=ec.MAX_POINTS), deque(maxlen=ec.MAX_MARKS)


def test_roundtrip_survives_restart(store):
    pts, mks = _deques()
    now = time.time()
    for i in range(4):
        ec.record_point(pts, now - 300 + i * 60, 46.0 + i * 0.25, i * 0.25)
    ec.record_mark(mks, now - 200, "buy", "WIF", usd=3.0, mint="m1", entry=0.001)
    ec.record_mark(mks, now - 100, "sell", "WIF", pnl_usd=0.8, reason="tp", mint="m1")
    assert ec.save(pts, mks) is True

    pts2, mks2 = _deques()          # a fresh process
    assert ec.load(pts2, mks2) == 4
    assert len(mks2) == 2
    assert [m["kind"] for m in mks2] == ["buy", "sell"]


def test_every_timeframe_has_data_after_restart(store):
    pts, mks = _deques()
    now = time.time()
    ec.record_point(pts, now - 60, 46.0, 0.0)
    ec.record_mark(mks, now - 60, "buy", "BONK", usd=2.0)
    ec.save(pts, mks)

    pts2, mks2 = _deques()
    ec.load(pts2, mks2)
    for tf in ec.TABS:
        p, m = ec.window(pts2, mks2, tf, now)
        assert p, f"{tf} lost its points"
        assert m, f"{tf} lost its marks"


def test_corrupt_store_does_not_crash_boot(store):
    ec.store_path().write_text("{not json")
    pts, mks = _deques()
    assert ec.load(pts, mks) == 0      # clean start, no exception


def test_dedupe_drops_repeated_rows(store):
    pts, mks = _deques()
    t = time.time()
    pt = ec.record_point(pts, t, 46.0, 0.0)
    pts.append(dict(pt))               # same instant, same point
    ec.record_mark(mks, t, "buy", "WIF", usd=1.0)
    ec.record_mark(mks, t, "buy", "WIF", usd=1.0)
    ec.save(pts, mks)

    pts2, mks2 = _deques()
    ec.load(pts2, mks2)
    assert len(pts2) == 1
    assert len(mks2) == 1


def test_backfill_rebuilds_marks_from_ledger(store):
    now = time.time()
    state = {
        "start_equity": 46.0, "cash": 20.0, "day_start": 46.0,
        "positions": {"mA": {"symbol": "WIF", "qty": 1000, "entry": 0.001,
                             "last": 0.0012, "cost": 3.0, "opened": now - 3600}},
        "closed": [
            {"symbol": "BONK", "pnl_usd": 1.5, "reason": "tp", "closed": now - 1800},
            {"symbol": "POPCAT", "pnl_usd": -0.4, "reason": "stop", "closed": now - 600},
        ],
    }
    pts, mks = ec.backfill_from_ledger(state)
    assert len(mks) == 3
    assert [m["kind"] for m in mks] == ["buy", "sell", "sell"]
    # equity walks the real realised P&L, it is not invented
    assert pts[0]["equity"] == 46.0
    assert pts[-1]["equity"] == pytest.approx(47.1)


def test_backfill_ignores_empty_ledger(store):
    pts, mks = ec.backfill_from_ledger({"start_equity": 46.0, "positions": {}, "closed": []})
    assert pts == [] and mks == []
