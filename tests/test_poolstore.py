"""Tests for the append-only pool store and the win-rate lab filters."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from desk.poolstore import PoolStore, load_paths  # noqa: E402


def _c(ts, close=1.0):
    return {"ts": ts, "open": close, "high": close, "low": close,
            "close": close, "vol": 10.0}


def test_put_and_get_roundtrip(tmp_path):
    s = PoolStore(tmp_path / "store.json")
    s.put("addr1", {"name": "WIF / SOL", "liq": 5000.0},
          [_c(100), _c(200)])
    assert len(s) == 1
    assert s.has("addr1")
    assert len(s.candles("addr1")) == 2
    s.save()

    s2 = PoolStore(tmp_path / "store.json")
    assert s2.get("addr1")["name"] == "WIF / SOL"
    assert [c["ts"] for c in s2.candles("addr1")] == [100, 200]


def test_merge_dedupes_and_sorts(tmp_path):
    s = PoolStore(tmp_path / "store.json")
    s.put("a", None, [_c(300), _c(100)])
    s.put("a", None, [_c(200), _c(100, close=2.0)])   # 100 updated, 200 new
    cs = s.candles("a")
    assert [c["ts"] for c in cs] == [100, 200, 300]
    assert cs[0]["close"] == 2.0                       # newer value wins


def test_max_bars_keeps_newest(tmp_path):
    s = PoolStore(tmp_path / "store.json", max_bars=3)
    s.put("a", None, [_c(t) for t in range(1, 11)])
    assert [c["ts"] for c in s.candles("a")] == [8, 9, 10]


def test_corrupt_file_does_not_raise(tmp_path):
    p = tmp_path / "store.json"
    p.write_text("{not json", encoding="utf-8")
    s = PoolStore(p)
    assert len(s) == 0
    assert (tmp_path / "store.corrupt").exists()


def test_bad_candles_rejected(tmp_path):
    s = PoolStore(tmp_path / "store.json")
    s.put("a", None, [{"ts": 1, "close": 0}, {"ts": 2, "close": 1.0},
                      {"close": 5}, {"ts": 3, "close": "x"}])
    assert [c["ts"] for c in s.candles("a")] == [2]


def test_count_and_stats(tmp_path):
    s = PoolStore(tmp_path / "store.json")
    s.put("a", None, [_c(t) for t in range(1, 101)])
    s.put("b", None, [_c(t) for t in range(1, 6)])
    assert s.count(min_bars=70) == 1
    st = s.stats()
    assert st["pools"] == 2 and st["usable"] == 2 and st["candles"] == 105


def test_load_paths_filters_short_history(tmp_path):
    s = PoolStore(tmp_path / "store.json")
    s.put("a", {"name": "AAA", "liq": 1.0}, [_c(t) for t in range(100)])
    s.put("b", {"name": "BBB"}, [_c(t) for t in range(10)])
    s.save()
    paths = load_paths(tmp_path / "store.json", min_bars=70)
    assert len(paths) == 1 and paths[0]["name"] == "AAA"


def test_gate_ok_and_pullback():
    from winrate_lab import gate_ok, pullback_ok
    candles = [{"ts": i * 300, "open": 1.0, "high": 1.0, "low": 1.0,
                "close": 1.0, "vol": 10.0} for i in range(40)]
    row = {"candles": candles, "idx": 30, "close": 1.0, "recent_high": 2.0,
           "attrs": {"liq": 5000, "mcap": 1_000_000, "buys_h1": 100,
                     "sells_h1": 50, "buyers_h1": 40, "created_at": None}}
    assert gate_ok(row, "none") is True
    assert gate_ok(row, "full") is True
    # price 50% below a 2.0 high clears a 20% pullback
    assert pullback_ok(row, 0.20) is True
    row["close"] = 1.9
    assert pullback_ok(row, 0.20) is False
    # a honeypot (almost no sells) must fail the flow gate
    row["attrs"]["sells_h1"] = 1
    assert gate_ok(row, "scan+flow") is False
