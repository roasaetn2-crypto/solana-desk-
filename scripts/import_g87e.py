#!/usr/bin/env python3
"""Seed the pool store from the flat ``data/g87e/*_5m.json`` candle dumps.

Those dumps are ``{"pool","label","tf","fetched_at","candles":[...]}``. We map
each into the store's record shape so ``winrate_lab.py`` can consume them
immediately, without waiting on the network harvester.

Usage:
    python scripts/import_g87e.py --dir data/g87e --store data/poolstore.json
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk.poolstore import PoolStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/g87e")
    ap.add_argument("--store", default="data/poolstore.json")
    ap.add_argument("--max-bars", type=int, default=1000)
    args = ap.parse_args()

    store = PoolStore(args.store, max_bars=args.max_bars)
    files = sorted(glob.glob(str(Path(args.dir) / "*_5m.json")))
    if not files:
        print(f"no *_5m.json under {args.dir}")
        return 1

    added = 0
    for f in files:
        d = json.load(open(f))
        addr = d.get("pool")
        candles = d.get("candles") or []
        if not addr or not candles:
            continue
        rec = {
            "name": d.get("label") or addr[:12],
            "created_at": d.get("created_at"),
        }
        store.put(addr, rec, candles)
        added += 1
        print(f"  + {d.get('label','?'):14s} {addr[:8]} {len(candles)} candles")

    store.save()
    print(f"seeded {added} pools -> {args.store}  stats={store.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
