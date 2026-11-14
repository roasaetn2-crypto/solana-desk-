#!/usr/bin/env python3
"""Targeted harvest of *older* pools.

The generic harvester walks ``new_pools`` first, but a pool younger than ~6h
cannot hold 70 five-minute bars, so those requests are wasted against a tight
rate limit. This pass discovers from ``trending_pools`` and ``pools`` only,
sorts oldest-first (most history per request), and stops at ``--target`` usable
pools. It reuses the harvester's own pacer / fetch / record helpers.

Usage:
    python scripts/harvest_old.py --target 60 --pages 6 --gap 7 \
        --proxy http://127.0.0.1:18080 --store data/poolstore.json
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from desk.poolstore import PoolStore  # noqa: E402
import pool_harvest as ph  # noqa: E402


def discover_old(pacer, pages, proxies):
    seen: dict[str, dict] = {}
    for ep in ("trending_pools", "pools"):
        empty = 0
        for page in range(1, pages + 1):
            body = ph.get(pacer, f"{ph.GT}/networks/solana/{ep}", {"page": page}, proxies)
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
            print(f"    {ep} p{page}: {len(seen)} unique", flush=True)
    return list(seen.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=60)
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--gap", type=float, default=7.0)
    ap.add_argument("--min-age-hours", type=float, default=8.0)
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

    print("discovering older pools...", flush=True)
    found = discover_old(pacer, args.pages, proxies)

    # oldest first = most bars per request
    def age(r):
        a = ph._age_hours(r.get("created_at"))
        return a if a is not None else 0.0
    found.sort(key=age, reverse=True)

    added = extended = skipped = failed = 0
    for i, rec in enumerate(found, 1):
        addr = rec["address"]
        have = store.get(addr) or {}
        have_bars = len(have.get("candles") or [])
        if have_bars >= ph.MIN_BARS:
            skipped += 1
            continue
        a = ph._age_hours(rec.get("created_at"))
        if a is not None and a < args.min_age_hours:
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
            print(f"  [{i}/{len(found)}] +{added} new, ~{extended} ext, "
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
