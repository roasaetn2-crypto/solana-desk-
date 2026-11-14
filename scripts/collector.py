#!/usr/bin/env python3
"""Collector: grow the candle store with pools in the desk's *tradeable* band.

The problem this solves, measured rather than assumed.

``new_pools`` is a rolling ~6-minute feed. On 2026-11-15, eight pages returned
160 pools and **every one** was under 0.1 hours old; paging back does not walk
into the past, it walks minutes apart. So a one-shot harvest can never see a
6-48h pool, which is exactly the band the desk trades and the only band with
enough bars (70+) to backtest. That is why ``harvest_window.py`` kept finding 0.

The fix is to separate *discovery* from *fetching*:

  phase 1 (discover)  pull new_pools, remember every candidate in a watchlist
                      with its created_at. Cheap: 1-3 requests, no OHLCV.
  phase 2 (harvest)   for each watched pool whose age has now entered the
                      window, fetch its OHLCV once and write it to the store.

Run it on a schedule (every ~20 min). A pool seen young in phase 1 becomes
harvestable a few hours later, with no long-running process and no dependence on
the phone staying awake between runs -- each invocation is short and idempotent.

Usage:
    python3 scripts/collector.py                       # one pass (cron-friendly)
    python3 scripts/collector.py --discover-pages 2 --max-fetch 12
    python3 scripts/collector.py --status              # what is in the watchlist
    python3 scripts/collector.py --proxy http://127.0.0.1:18080
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from desk.poolstore import PoolStore  # noqa: E402
import pool_harvest as ph  # noqa: E402

WATCH_PATH = "data/watchlist.json"
STORE_PATH = "data/poolstore.json"

# The desk's tradeable band. Younger than this has too few 5m bars to judge;
# older than this is outside SCAN["max_age_hours"] unless the established lane
# accepts it (which needs its own, stricter floors -- see config.ESTABLISHED).
MIN_AGE_H = 6.0
MAX_AGE_H = 48.0
# Keep watching a little past the window so a run that lands late still catches
# the pool instead of dropping it the moment it crosses 48h.
GRACE_H = 6.0
MAX_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_h(created_at: str | None, now: datetime | None = None) -> float | None:
    """Hours since the pool was created, or None when the feed omitted it."""
    if not created_at:
        return None
    try:
        t = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return ((now or _now()) - t).total_seconds() / 3600.0


class Watchlist:
    """Candidate pools remembered from when they were too young to harvest.

    Append-only in spirit: a pool is added once and only ever gains a
    ``harvested_at`` stamp. Writes are atomic so a phone killed mid-run cannot
    leave a truncated JSON file behind.
    """

    def __init__(self, path: str = WATCH_PATH):
        self.path = Path(path)
        self.pools: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.pools = d.get("pools") or {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"pools": self.pools}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, rec: dict) -> bool:
        """Remember a pool. False when it was already known."""
        addr = rec.get("address")
        if not addr:
            return False
        if addr in self.pools:
            return False
        self.pools[addr] = {
            "address": addr,
            "name": rec.get("name"),
            "created_at": rec.get("created_at"),
            "liq": rec.get("liq"),
            "mcap": rec.get("mcap"),
            "vol_h1": rec.get("vol_h1"),
            "buys_h1": rec.get("buys_h1"),
            "sells_h1": rec.get("sells_h1"),
            "first_seen": _now().isoformat(),
            "harvested_at": None,
            "attempts": 0,
        }
        return True

    def due(self, now: datetime | None = None, min_age_h: float = MIN_AGE_H,
            max_age_h: float = MAX_AGE_H) -> list[dict]:
        """Watched pools now inside the window, oldest first (most bars first)."""
        out = []
        for p in self.pools.values():
            if p.get("harvested_at"):
                continue
            if p.get("attempts", 0) >= MAX_ATTEMPTS:
                continue
            a = _age_h(p.get("created_at"), now)
            if a is None:
                continue
            if min_age_h <= a <= max_age_h:
                out.append(p)
        out.sort(key=lambda p: _age_h(p.get("created_at"), now) or 0.0, reverse=True)
        return out

    def prune(self, now: datetime | None = None, keep_h: float = MAX_AGE_H + GRACE_H) -> int:
        """Drop pools that aged out without ever being harvestable.

        Without this the watchlist grows without bound on a phone: every run adds
        ~40 addresses and nothing is ever removed.
        """
        dead = []
        for addr, p in self.pools.items():
            if p.get("harvested_at"):
                continue
            a = _age_h(p.get("created_at"), now)
            if a is not None and a > keep_h:
                dead.append(addr)
            elif p.get("attempts", 0) >= MAX_ATTEMPTS:
                dead.append(addr)
        for addr in dead:
            self.pools.pop(addr, None)
        return len(dead)

    def stats(self) -> dict:
        total = len(self.pools)
        harvested = sum(1 for p in self.pools.values() if p.get("harvested_at"))
        return {"watched": total, "harvested": harvested,
                "pending": total - harvested}


def discover(pacer, pages: int, proxies, watch: Watchlist) -> int:
    """Phase 1: pull new_pools and remember every candidate."""
    added = 0
    for page in range(1, pages + 1):
        body = ph.get(pacer, f"{ph.GT}/networks/solana/new_pools", {"page": page}, proxies)
        rows = (body or {}).get("data") or []
        if not rows:
            break
        for row in rows:
            rec = ph.pool_record(row)
            if watch.add(rec):
                added += 1
    return added


def harvest_due(pacer, store: PoolStore, watch: Watchlist, proxies,
                max_fetch: int = 12, min_age_h: float = MIN_AGE_H,
                max_age_h: float = MAX_AGE_H) -> tuple[int, int]:
    """Phase 2: fetch OHLCV for watched pools that have aged into the window.

    Returns (added, failed). A failed fetch increments ``attempts`` so a pool
    that consistently returns too few bars is not retried forever.
    """
    added = failed = 0
    due = watch.due(min_age_h=min_age_h, max_age_h=max_age_h)
    for p in due[:max_fetch]:
        addr = p["address"]
        candles = ph.fetch_ohlcv(pacer, addr, proxies=proxies)
        p["attempts"] = p.get("attempts", 0) + 1
        if len(candles) < ph.MIN_BARS:
            failed += 1
            continue
        store.put(addr, {"name": p.get("name"), "address": addr,
                         "created_at": p.get("created_at"), "liq": p.get("liq"),
                         "mcap": p.get("mcap"), "vol_h1": p.get("vol_h1"),
                         "buys_h1": p.get("buys_h1"), "sells_h1": p.get("sells_h1")},
                  candles)
        p["harvested_at"] = _now().isoformat()
        added += 1
    return added, failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", default=WATCH_PATH)
    ap.add_argument("--store", default=STORE_PATH)
    ap.add_argument("--discover-pages", type=int, default=2)
    ap.add_argument("--max-fetch", type=int, default=12)
    ap.add_argument("--min-age-hours", type=float, default=MIN_AGE_H)
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_H)
    ap.add_argument("--gap", type=float, default=2.5)
    ap.add_argument("--max-bars", type=int, default=1000)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--status", action="store_true", help="print watchlist state and exit")
    ap.add_argument("--offline", action="store_true", help="no network: just prune/report")
    args = ap.parse_args(argv)

    watch = Watchlist(args.watch)
    store = PoolStore(args.store, max_bars=args.max_bars)

    if args.status:
        st = watch.stats()
        due = watch.due(min_age_h=args.min_age_hours, max_age_h=args.max_age_hours)
        print(json.dumps({**st, "due_now": len(due),
                          "store": store.stats(),
                          "window_h": [args.min_age_hours, args.max_age_hours]}, indent=2))
        return 0

    proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else None
    pacer = ph.Pacer(gap_s=args.gap)

    if args.offline:
        pruned = watch.prune()
        watch.save()
        print(f"offline: pruned {pruned}, watched {len(watch.pools)}")
        return 0

    started = time.time()
    try:
        new = discover(pacer, args.discover_pages, proxies, watch)
    except Exception as e:
        print(f"discover failed ({type(e).__name__}: {e})", file=sys.stderr)
        new = 0

    try:
        added, failed = harvest_due(pacer, store, watch, proxies,
                                    max_fetch=args.max_fetch,
                                    min_age_h=args.min_age_hours,
                                    max_age_h=args.max_age_hours)
    except Exception as e:
        print(f"harvest failed ({type(e).__name__}: {e})", file=sys.stderr)
        added = failed = 0

    pruned = watch.prune()
    watch.save()
    if added:
        store.save()

    st = watch.stats()
    ss = store.stats()
    print(f"discover +{new} | harvest +{added} (fail {failed}) | pruned {pruned} | "
          f"watched {st['watched']} pending {st['pending']} | "
          f"store {ss['pools']} pools {ss['usable']} usable | "
          f"{time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
