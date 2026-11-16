#!/usr/bin/env python3
"""Fetch a *fresh-launch* Solana dataset (5m OHLCV + mcap/liquidity/created_at).

The desk's g87e cache is calm majors. A 1-10 minute scalp only has an edge on
fresh, volatile launches, so this harvester pulls the population the desk
actually trades:

  * GeckoTerminal ``new_pools`` (several pages) and ``trending`` 5m/1h pools
  * dedupe by pool address
  * fetch 5-minute OHLCV for each, politely (<= ~28 req/min)
  * keep pools with >= MIN_BARS bars (a pool that survived a few hours — fresh
    but alive, not a dead second-old launch)

Writes one JSON per pool into ``data/fresh/`` in the same shape the scalper lab
reads. Resumable: a pool already cached is skipped.

Usage:
    python3 scripts/fetch_fresh.py --max-pools 60
    python3 scripts/fetch_fresh.py --pages 10 --max-pools 80
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "fresh"
GT = "https://api.geckoterminal.com/api/v2"
UA = {"Accept": "application/json", "User-Agent": "solana-desk-fresh-harvest/1.0"}
MIN_BARS = 40
GAP_S = 2.2


def _get(url: str, tries: int = 4) -> dict:
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(6 * (a + 1))
                continue
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError(f"fetch failed: {url} ({last})")


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def discover(pages: int) -> list[dict]:
    """Return candidate pools {address, name, created_at, fdv, liq, vol}."""
    seen: dict[str, dict] = {}

    def add(items):
        for p in items:
            a = p.get("attributes") or {}
            addr = a.get("address")
            if not addr or addr in seen:
                continue
            vol = a.get("volume_usd")
            seen[addr] = {
                "address": addr,
                "name": a.get("name") or addr[:8],
                "created_at": a.get("pool_created_at"),
                "fdv": _num(a.get("fdv_usd")) or _num(a.get("market_cap_usd")),
                "liq": _num(a.get("reserve_in_usd")),
                "vol": _num(vol.get("h24")) if isinstance(vol, dict) else None,
            }

    # GeckoTerminal only serves OHLCV for pools with enough age/volume, so the
    # second-old launches in new_pools return zero bars. Trending + top pools
    # are the ones that actually have candle history; we treat each pool's first
    # few hours as its fresh-launch window in the lab.
    for dur in ("5m", "1h"):
        for page in range(1, pages + 1):
            try:
                add((_get(f"{GT}/networks/solana/trending_pools?duration={dur}&page={page}").get("data") or []))
            except Exception as e:  # noqa: BLE001
                print(f"  trending {dur} p{page}: {e}", file=sys.stderr)
            time.sleep(GAP_S)
    for page in range(1, pages + 1):
        try:
            add((_get(f"{GT}/networks/solana/pools?page={page}").get("data") or []))
        except Exception as e:  # noqa: BLE001
            print(f"  top p{page}: {e}", file=sys.stderr)
        time.sleep(GAP_S)
    return list(seen.values())


def fetch_ohlcv(addr: str) -> list[dict]:
    url = (f"{GT}/networks/solana/pools/{addr}/ohlcv/minute"
           f"?aggregate=5&limit=1000&currency=usd")
    body = _get(url)
    raw = (body.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    out = []
    for row in raw:
        try:
            ts, o, h, l, c, v = row
            if not c or float(c) <= 0:
                continue
            out.append({"ts": float(ts), "open": float(o), "high": float(h),
                        "low": float(l), "close": float(c), "vol": float(v)})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["ts"])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--max-pools", type=int, default=60)
    ap.add_argument("--min-bars", type=int, default=MIN_BARS)
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"discovering fresh pools ({args.pages} new_pools pages + trending)...", flush=True)
    cands = discover(args.pages)
    print(f"  {len(cands)} unique candidates", flush=True)

    kept = 0
    for i, c in enumerate(cands, 1):
        if kept >= args.max_pools:
            break
        path = OUT / f"{c['address'][:8]}_5m.json"
        if path.exists():
            kept += 1
            continue
        try:
            cs = fetch_ohlcv(c["address"])
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(cands)}] {c['name'][:18]:18} ohlcv fail: {e}", file=sys.stderr)
            time.sleep(GAP_S)
            continue
        if len(cs) < args.min_bars:
            print(f"  [{i}/{len(cands)}] {c['name'][:18]:18} skip ({len(cs)} bars)", flush=True)
            time.sleep(GAP_S)
            continue
        doc = {"pool": c["address"], "label": c["name"], "tf": "5m",
               "created_at": c["created_at"], "mcap": c["fdv"], "liq": c["liq"],
               "vol_h24": c["vol"], "candles": cs}
        path.write_text(json.dumps(doc), encoding="utf-8")
        kept += 1
        print(f"  [{i}/{len(cands)}] {c['name'][:18]:18} KEEP {len(cs)} bars "
              f"mcap={c['fdv']} liq={c['liq']}", flush=True)
        time.sleep(GAP_S)

    print(f"\nkept {kept} pools in {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
