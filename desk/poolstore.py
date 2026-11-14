"""Append-only, resumable candle store keyed by Solana pool address.

The desk's experiments all hit the same wall: a handful of pools produce a
handful of gated trades, so every result is an anecdote. The fix is not another
rule, it is more independent observations. This module is the storage half of
that fix.

Design rules, learned from the rest of the repo:

  * **Append-only by pool.** A pool is fetched once and never re-downloaded
    from scratch. New candles are merged by timestamp, so a later harvest
    extends history instead of replacing it.
  * **Resumable.** Every write is atomic (temp file + ``os.replace``), so a
    crash mid-harvest leaves the previous good store on disk, not a truncated
    JSON file.
  * **Bounded.** Each pool keeps at most ``max_bars`` candles (newest win), so
    the store cannot grow without limit on a phone.
  * **Pure I/O, no network.** Fetching lives in ``scripts/pool_harvest.py``.
    This module only reads and writes the file, which makes it testable
    offline.

The store is a single JSON document::

    {
      "version": 1,
      "updated_at": 1700000000,
      "pools": {
        "<pool_address>": {
          "address": "...",
          "name": "WIF / SOL",
          "created_at": "2026-11-01T12:00:00Z",
          "liq": 12345.0,
          "mcap": 1000000.0,
          "fdv": 1000000.0,
          "vol_h1": 5000.0,
          "buys_h1": 120, "sells_h1": 80, "buyers_h1": 60,
          "candles": [{"ts":..., "open":..., "high":..., "low":..., "close":..., "vol":...}, ...],
          "updated_at": 1700000000
        }, ...
      }
    }
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_PATH = "data/poolstore.json"
DEFAULT_MAX_BARS = 1000


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _clean_candle(c: dict) -> dict | None:
    """Keep only finite, positive, correctly-ordered OHLCV rows."""
    try:
        ts = int(c["ts"])
        o = float(c.get("open") or 0.0)
        h = float(c.get("high") or 0.0)
        lo = float(c.get("low") or 0.0)
        cl = float(c["close"])
        v = float(c.get("vol") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    if cl <= 0 or ts <= 0:
        return None
    if h <= 0:
        h = max(o, cl)
    if lo <= 0:
        lo = min(o, cl)
    return {"ts": ts, "open": o, "high": h, "low": lo, "close": cl, "vol": v}


class PoolStore:
    """A dict of pool records persisted as one JSON file."""

    def __init__(self, path: str | Path = DEFAULT_PATH,
                 max_bars: int = DEFAULT_MAX_BARS):
        self.path = Path(path)
        self.max_bars = int(max_bars)
        self.data: dict = {"version": 1, "updated_at": 0, "pools": {}}
        self.load()

    # ------------------------------------------------------------------ #
    # load / save
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # A corrupt store must not take the desk down. Move it aside so a
            # human can inspect it, then start clean.
            try:
                self.path.replace(self.path.with_suffix(".corrupt"))
            except OSError:
                pass
            return
        if isinstance(raw, dict) and isinstance(raw.get("pools"), dict):
            self.data = raw
            self.data.setdefault("version", 1)
            self.data.setdefault("updated_at", 0)

    def save(self) -> None:
        self.data["updated_at"] = int(time.time())
        _atomic_write(self.path, json.dumps(self.data, separators=(",", ":")))

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self.data.get("pools") or {})

    def has(self, address: str) -> bool:
        return address in (self.data.get("pools") or {})

    def get(self, address: str) -> dict | None:
        return (self.data.get("pools") or {}).get(address)

    def addresses(self) -> list[str]:
        return list((self.data.get("pools") or {}).keys())

    def count(self, min_bars: int = 1) -> int:
        """Pools with at least ``min_bars`` candles (the usable ones)."""
        return sum(1 for p in (self.data.get("pools") or {}).values()
                   if len(p.get("candles") or []) >= min_bars)

    def candles(self, address: str) -> list[dict]:
        p = self.get(address) or {}
        return list(p.get("candles") or [])

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    def put(self, address: str, record: dict | None = None,
            candles: list[dict] | None = None) -> dict:
        """Insert or merge a pool. Returns the stored record.

        ``record`` carries the pool attributes (name, liquidity, ...).
        ``candles`` are merged into any existing history by timestamp.
        """
        if not address:
            raise ValueError("address required")
        pools = self.data.setdefault("pools", {})
        cur = pools.get(address) or {"address": address, "candles": []}
        if record:
            for k, v in record.items():
                if k == "candles":
                    continue
                if v is not None:
                    cur[k] = v
        merged: dict[int, dict] = {}
        for c in (cur.get("candles") or []):
            cc = _clean_candle(c)
            if cc:
                merged[cc["ts"]] = cc
        for c in (candles or []):
            cc = _clean_candle(c)
            if cc:
                merged[cc["ts"]] = cc
        ordered = [merged[k] for k in sorted(merged)]
        if self.max_bars > 0:
            ordered = ordered[-self.max_bars:]
        cur["candles"] = ordered
        cur["updated_at"] = int(time.time())
        pools[address] = cur
        return cur

    def remove(self, address: str) -> bool:
        return (self.data.get("pools") or {}).pop(address, None) is not None

    def stats(self) -> dict:
        pools = list((self.data.get("pools") or {}).values())
        bars = [len(p.get("candles") or []) for p in pools]
        return {
            "pools": len(pools),
            "usable": sum(1 for b in bars if b > 0),
            "candles": sum(bars),
            "min_bars": min(bars) if bars else 0,
            "max_bars": max(bars) if bars else 0,
        }


def load_paths(path: str | Path = DEFAULT_PATH, min_bars: int = 70) -> list[dict]:
    """Turn a store into the ``[{"name","candles","horizon"}]`` shape the
    backtest harnesses expect. Pools with too little history are skipped."""
    store = PoolStore(path)
    out = []
    for addr in store.addresses():
        rec = store.get(addr) or {}
        cs = rec.get("candles") or []
        if len(cs) < min_bars:
            continue
        out.append({
            "name": (rec.get("name") or addr)[:12],
            "address": addr,
            "candles": cs,
            "horizon": "scalp",
            "liq": rec.get("liq"),
            "mcap": rec.get("mcap") or rec.get("fdv"),
            "vol_h1": rec.get("vol_h1"),
            "buys_h1": rec.get("buys_h1"),
            "sells_h1": rec.get("sells_h1"),
            "buyers_h1": rec.get("buyers_h1"),
            "created_at": rec.get("created_at"),
        })
    return out
