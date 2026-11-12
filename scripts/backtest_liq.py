"""Liquidity-filtered backtest. Fetches real GeckoTerminal OHLCV once for all
pools >= MIN_LIQ, caches candles, then simulates the desk's exit rules and
reports win rate per liquidity tier (>= 50k, >= 100k, >= 250k).

Usage:
    python scripts/backtest_liq.py --min-liq 50000 --cache ohlcv_cache.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
from backtest_100 import simulate_trade, fetch_ohlcv, GT, SESSION  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-liq", type=float, default=50000)
    ap.add_argument("--cache", default="ohlcv_cache.json")
    ap.add_argument("--max-pools", type=int, default=140)
    args = ap.parse_args()

    pools = json.load(open("pools_cache.json"))
    pools = [p for p in pools if (p.get("liquidity") or 0) >= args.min_liq]
    pools.sort(key=lambda p: -(p.get("liquidity") or 0))
    pools = pools[: args.max_pools]
    print(f"Fetching OHLCV for {len(pools)} pools (liq >= ${args.min_liq:,.0f})", flush=True)

    cache: dict[str, list] = {}
    if Path(args.cache).exists():
        cache = json.load(open(args.cache))
    fetched = 0
    for i, p in enumerate(pools):
        addr = p["address"]
        if addr in cache:
            continue
        candles = fetch_ohlcv(addr, "minute", 5)
        cache[addr] = candles
        fetched += 1
        if fetched % 10 == 0:
            Path(args.cache).write_text(json.dumps(cache), encoding="utf-8")
            print(f"  fetched {fetched} (pool {i+1}/{len(pools)})", flush=True)
        time.sleep(0.35)
    Path(args.cache).write_text(json.dumps(cache), encoding="utf-8")
    print(f"Done. Cached {len(cache)} pools -> {args.cache}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
