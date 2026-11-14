#!/usr/bin/env python3
"""Harvest pools in the desk's *tradeable age window* (6h .. 48h).

Why this window:
  * younger than ~6h  -> fewer than 70 five-minute bars, cannot backtest;
  * older than 48h     -> the scan gate rejects it (max_age_hours = 48).

The generic harvester walks ``new_pools`` but keeps everything; ``harvest_old``
walks trending/pools and gets year-old majors. Neither is the desk's universe.
``new_pools`` is ordered newest-first, so paging back reaches the 6-48h band.
This script discovers many ``new_pools`` pages, keeps only the window, sorts
oldest-first (most bars per request), and stops at ``--target`` usable pools.

Usage:
    python scripts/harvest_window.py --target 50 --pages 14 --gap 7 \
        --proxy http://127.0.0.1:18080 --store data/poolstore.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from desk.poolstore import PoolStore  # noqa: E402
import pool_harvest as ph  # noqa: E402


def discover_new(pacer, pages, proxies):
    seen: dict[str, dict] = {}
    empty = 0
    for page in range(1, pages + 1):
        body = ph.get(pacer, f"{ph.GT}/networks/solana/new_pools", {"page": page}, proxies)
        rows = (body or {}).get("data") or []
        if not rows:
            empty += 1
            if empty >= 2:
                break
            continue
        empty = 0
        for row in rows:
            rec = ph.pool_record(row)
            addr = rec.get("address")
            if addr and addr not in seen:
                seen[addr] = rec
        print(f"    new_pools p{page}: {len(seen)} unique", flush=True)
    return list(seen.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=50)
    ap.add_argument("--pages", type=int, default=14)
    ap.add_argument("--gap", type=float, default=7.0)
    ap.add_argument("--min-age-hours", type=float, default=6.0)
    ap.add_argument("--max-age-hours", type=float, default=48.0)
    ap.add_argument("--store", default="data/poolstore.json")
    ap.add_argument("--max-bars", type=int, default=1000)
    ap.add_argument("--proxy", default=None)
    args = ap.parse_args()

    proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else None
    pacer = ph.Pacer(gap_s=args.gap)
    store = PoolStore(args.store, max_bars=args.max_bars)

    before = store.count(ph.MIN_BARS)
    print(f"store: {len(store)} pools, {before} usable -> target {args.target}", flush=True)
    if before >= args.target:
        print("target already reached.")
        return 0

    print("discovering new_pools (paging into the 6-48h band)...", flush=True)
    found = discover_new(pacer, args.pages, proxies)

    def age(r):
        a = ph._age_hours(r.get("created_at"))
        return a if a is not None else -1.0

    window = [r for r in found
              if args.min_age_hours <= age(r) <= args.max_age_hours]
    print(f"  {len(found)} discovered, {len(window)} in "
          f"{args.min_age_hours:.0f}-{args.max_age_hours:.0f}h window", flush=True)
    window.sort(key=age, reverse=True)  # oldest-in-window first = most bars

    added = extended = skipped = failed = 0
    for i, rec in enumerate(window, 1):
        addr = rec["address"]
        have = store.get(addr) or {}
        have_bars = len(have.get("candles") or [])
        if have_bars >= ph.MIN_BARS:
            skipped += 1
            continue
        candles = ph.fetch_ohlcv(pacer, addr, proxies=proxies)
        if len(candles) < ph.MIN_BARS:
            failed += 1
            continue
        if have_bars:
            extended += 1
        else:
            added += 1
        store.put(addr, rec, candles)
        if (added + extended) % 3 == 0:
            store.save()
            print(f"  [{i}/{len(window)}] +{added} new, ~{extended} ext, "
                  f"{store.count(ph.MIN_BARS)} usable", flush=True)
        if store.count(ph.MIN_BARS) >= args.target:
            break

    store.save()
    st = store.stats()
    print("=" * 56)
    print(f"  added {added}, extended {extended}, skipped {skipped}, failed {failed}")
    print(f"  store: {st['pools']} pools, {st['usable']} usable, {st['candles']:,} candles")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
