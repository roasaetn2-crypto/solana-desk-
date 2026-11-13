"""Tests for the offline indicator evaluation harness (verdict logic + wiring)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESK_OFFLINE", "1")

from desk import indicator_backtest as ib  # noqa: E402


def test_synth_paths_deterministic_and_shaped():
    a = ib.synth_paths(n=3, bars=40, seed=11)
    b = ib.synth_paths(n=3, bars=40, seed=11)
    assert [c["close"] for c in a[0]["candles"]] == [c["close"] for c in b[0]["candles"]]
    assert len(a) == 3 and len(a[0]["candles"]) == 40
    for c in a[0]["candles"]:
        assert c["high"] >= c["close"] >= 0
        assert c["low"] <= c["close"]


def test_verdict_bar():
    base = {"trades": 100, "win_rate": 0.30, "profit_factor": 0.9}
    # clears every condition
    assert ib.verdict({"trades": 60, "win_rate": 0.40, "profit_factor": 1.5}, base) == "ADD"
    # too few trades
    assert "only" in ib.verdict({"trades": 5, "win_rate": 0.9, "profit_factor": 3.0}, base)
    # lift too small
    assert "win lift" in ib.verdict({"trades": 60, "win_rate": 0.32, "profit_factor": 1.5}, base)
    # profit factor below the floor
    assert "PF" in ib.verdict({"trades": 60, "win_rate": 0.45, "profit_factor": 1.0}, base)
    # profit factor worse than baseline
    assert "baseline" in ib.verdict(
        {"trades": 60, "win_rate": 0.45, "profit_factor": 1.3},
        {"trades": 100, "win_rate": 0.30, "profit_factor": 1.4})


def test_summarize_marks_add_lists():
    report = {
        "baseline": {"trades": 100, "win_rate": 0.30, "profit_factor": 0.9},
        "entry_filters": {
            "good": {"trades": 60, "win_rate": 0.40, "profit_factor": 1.5},
            "weak": {"trades": 60, "win_rate": 0.31, "profit_factor": 1.5},
        },
        "exit_rules": {},
        "paths": 1, "bars": 100,
    }
    s = ib.summarize(report)
    assert s["add_entry"] == ["good"]
    assert s["entry_filters"]["weak"]["verdict"].startswith("DO-NOT-ADD")


def test_simulate_with_exit_triggers_extra_rule():
    # a path that keeps rising: desk TP may fire, but an always-true extra exit
    # must close the position and record the reason.
    candles = [{"ts": 1_700_000_000 + i * 300, "open": 1 + i * 0.01,
                "high": 1.01 + i * 0.01, "low": 0.99 + i * 0.01,
                "close": 1 + i * 0.01, "vol": 1000} for i in range(60)]
    t = ib.simulate_with_exit(candles, 30, "scalp", extra_exit=lambda s: True)
    assert t is not None
    assert "indicator exit" in t["reasons"]


def test_evaluate_runs_on_small_sample():
    paths = ib.synth_paths(n=4, bars=80, seed=3)
    rep = ib.evaluate(paths, bar_minutes=5, step=5, min_history=30)
    assert "baseline" in rep and "entry_filters" in rep
    assert rep["paths"] == 4
    assert "RSI < 35 (oversold)" in rep["entry_filters"]
