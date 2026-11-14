#!/usr/bin/env python3
"""Grow the desk's candle cache, one pool at a time, without ever re-fetching.

Every experiment in this repo has the same bottleneck: too few independent
pools. ``backtest_cache.json`` held 101 pools and produced 5 fully-gated trades;
``data/indicator_paths.json`` held 60. Five trades is not a result. This script
is the unglamorous fix: a resumable, append-only harvester that grows the pool
count every day.

What it does
------------
1. Discovers Solana pools from GeckoTerminal (new + trending + top, several
   pages each), deduped by address.
2. Skips any pool already in the store with enough candles -- it never
   re-downloads history it already has.
3. Fetches 5-minute OHLCV for the rest, politely (one global limiter, real
   429/Retry-After handling), and merges it into ``desk/poolstore.py``.
4. Saves every few pools, so Ctrl-C or a crash loses at most a handful.
5. Stops when the store reaches ``--target`` usable pools.

The store is append-only per pool: a later run extends a pool's history by
timestamp instead of replacing it. That is what makes this safe to run on a
timer (cron / Termux boot script) forever.

Usage
-----
    python scripts/pool_harvest.py --target 600
    python scripts/pool_harvest.py --target 150 --pages 6
    python scripts/pool_harvest.py --target 600 --refresh   # re-fetch stale pools
    python scripts/pool_harvest.py --list                   # show store stats

Honest limits
-------------
* GeckoTerminal's free tier is ~30 requests/minute. This script defaults to
  ~28/min. A 600-pool harvest therefore takes roughly 25-35 minutes and is
  meant to be run repeatedly, not in one heroic burst.
* OHLCV is capped at 1,000 five-minute bars (~3.5 days) per request. Re-running
  later appends newer bars; it does not recover history older than the first
  fetch. Start harvesting now if you want long history later.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk.poolstore import DEFAULT_PATH, PoolStore  # noqa: E402

GT = "https://api.geckoterminal.com/api/v2"
UA = "Mozilla/5.0 (compatible; solana-desk-harvester/1.0)"
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": UA})

CANDLE_MIN = 5
MIN_BARS = 70                 # enough for EMA-50 + a 20-bar high + forward path
DEFAULT_GAP_S = 2.15          # ~28 req/min, under the ~30/min public cap
MAX_RETRIES_429 = 8


class Pacer:
    """One global minimum gap between requests, plus 429 backoff."""

    def __init__(self, gap_s: float = DEFAULT_GAP_S):
        self.gap = max(0.0, float(gap_s))
        self.last = 0.0
        self.calls = 0

    def wait(self) -> None:
        delta = self.gap - (time.time() - self.last)
        if delta > 0:
            time.sleep(delta)
        self.last = time.time()
        self.calls += 1


def get(pacer: Pacer, url: str, params: dict | None = None,
        proxies: dict | None = None) -> dict | None:
    """GET with pacing and patient 429 handling. None on any hard failure."""
    for attempt in range(MAX_RETRIES_429):
        pacer.wait()
        try:
            r = SESSION.get(url, params=params, timeout=25, proxies=proxies)
        except requests.RequestException:
            time.sleep(2.0)
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return None
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            try:
                back = max(5.0, float(ra)) if ra else 30.0
            except ValueError:
                back = 30.0
            back = min(back, 120.0)
            print(f"    429 rate-limited, sleeping {back:.0f}s", flush=True)
            time.sleep(back)
            continue
        if r.status_code in (400, 404):
            return None
        time.sleep(2.0)
    return None


def _f(v):
    try:
        if v is None:
            return None
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def pool_record(row: dict) -> dict:
    """Flatten a GeckoTerminal pool row into the store's attribute shape."""
    a = row.get("attributes") or {}
    vol = a.get("volume_usd") or {}
    tx = a.get("transactions") or {}
    h1 = tx.get("h1") or {}
    return {
        "address": (row.get("id") or "").replace("solana_", ""),
        "name": a.get("name"),
        "created_at": a.get("pool_created_at"),
        "liq": _f(a.get("reserve_in_usd")),
        "mcap": _f(a.get("market_cap_usd")),
        "fdv": _f(a.get("fdv_usd")),
        "vol_h1": _f(vol.get("h1")),
        "vol_h6": _f(vol.get("h6")),
        "vol_h24": _f(vol.get("h24")),
        "buys_h1": _f(h1.get("buys")),
        "sells_h1": _f(h1.get("sells")),
        "buyers_h1": _f(h1.get("buyers")),
        "sellers_h1": _f(h1.get("sellers")),
    }


def discover(pacer: Pacer, pages: int, proxies: dict | None = None) -> list[dict]:
    """New + trending + top pools, several pages each, deduped by address."""
    seen: dict[str, dict] = {}
    endpoints = ("new_pools", "trending_pools", "pools")
    for ep in endpoints:
        empty = 0
        for page in range(1, max(1, pages) + 1):
            body = get(pacer, f"{GT}/networks/solana/{ep}", {"page": page}, proxies)
            rows = (body or {}).get("data") or []
            if not rows:
                empty += 1
                if empty >= 2:
                    break
                continue
            empty = 0
            for row in rows:
                rec = pool_record(row)
                addr = rec["address"]
                if addr and addr not in seen:
                    seen[addr] = rec
            print(f"    {ep} p{page}: +{len(rows)} rows, {len(seen)} unique", flush=True)
    return list(seen.values())


def fetch_ohlcv(pacer: Pacer, address: str, limit: int = 1000,
                proxies: dict | None = None) -> list[dict]:
    body = get(pacer, f"{GT}/networks/solana/pools/{address}/ohlcv/minute",
               {"aggregate": CANDLE_MIN, "limit": limit, "currency": "usd"}, proxies)
    lst = (((body or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = []
    for c in lst:
        if not c or len(c) < 6:
            continue
        cl = _f(c[4])
        if not cl or cl <= 0:
            continue
        out.append({"ts": int(c[0]), "open": _f(c[1]) or cl, "high": _f(c[2]) or cl,
                    "low": _f(c[3]) or cl, "close": cl, "vol": _f(c[5]) or 0.0})
    out.sort(key=lambda x: x["ts"])
    return out


def _age_hours(created_at: str | None) -> float | None:
    if not created_at:
        return None
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return (time.time() - ts) / 3600.0


def harvest(args) -> int:
    store = PoolStore(args.store, max_bars=args.max_bars)
    pacer = Pacer(args.gap)
    before = store.count(MIN_BARS)
    print(f"store: {len(store)} pools, {before} usable (>= {MIN_BARS} bars) -> "
          f"target {args.target}", flush=True)
    if before >= args.target:
        print("target already reached; nothing to do (use --refresh to extend).")
        return 0

    print(f"discovering pools ({args.pages} pages/endpoint)...", flush=True)
    proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else None
    found = discover(pacer, args.pages, proxies)
    print(f"  {len(found)} unique pools discovered", flush=True)

    added = extended = skipped = failed = 0
    for i, rec in enumerate(found, 1):
        addr = rec["address"]
        have = store.get(addr) or {}
        have_bars = len(have.get("candles") or [])
        if have_bars >= MIN_BARS and not args.refresh:
            skipped += 1
            continue
        # Don't waste a request on a pool the feed already says is dead/old.
        age = _age_hours(rec.get("created_at"))
        if age is not None and age > args.max_age_hours:
            skipped += 1
            continue
        candles = fetch_ohlcv(pacer, addr, proxies=proxies)
        if len(candles) < MIN_BARS:
            failed += 1
            continue
        if have_bars:
            extended += 1
        else:
            added += 1
        store.put(addr, rec, candles)
        if (added + extended) % 5 == 0:
            store.save()
            print(f"  [{i}/{len(found)}] +{added} new, ~{extended} extended, "
                  f"{store.count(MIN_BARS)} usable", flush=True)
        if store.count(MIN_BARS) >= args.target:
            break

    store.save()
    st = store.stats()
    print("\n" + "=" * 60)
    print(f"  added {added} new pools, extended {extended}, skipped {skipped}, "
          f"failed {failed}")
    print(f"  store: {st['pools']} pools, {st['usable']} usable, "
          f"{st['candles']:,} candles -> {args.store}")
    print("=" * 60)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grow the candle cache, resumably.")
    ap.add_argument("--target", type=int, default=600, help="usable pools to stop at")
    ap.add_argument("--pages", type=int, default=8, help="pages per GT endpoint")
    ap.add_argument("--store", default=str(ROOT / DEFAULT_PATH))
    ap.add_argument("--max-bars", type=int, default=1000)
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP_S,
                    help="seconds between requests (~28/min at 2.15)")
    ap.add_argument("--max-age-hours", type=float, default=24 * 14,
                    help="skip pools older than this (0 = no limit)")
    ap.add_argument("--proxy", default=None,
                    help="HTTP proxy, e.g. http://127.0.0.1:18080")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch pools already in the store (extend history)")
    ap.add_argument("--list", action="store_true", help="print store stats and exit")
    args = ap.parse_args(argv)

    if args.list:
        st = PoolStore(args.store).stats()
        print(json.dumps(st, indent=2))
        return 0
    if args.max_age_hours <= 0:
        args.max_age_hours = 10 ** 9
    return harvest(args)


if __name__ == "__main__":
    raise SystemExit(main())
