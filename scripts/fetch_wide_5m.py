"""Fetch a WIDE 5m candle set for the volume-gate validation (caveat A).

Reads research/pools_cache.json (402 discovered pools), keeps the ones in the
liquidity band the desk actually trades, and pages GeckoTerminal OHLCV so each
pool yields ~3000 bars (~10 days of 5m) instead of the 1000 a single call gives.

Writes data/g87e_wide/<pool8>_5m.json in the same shape refine_sweep.py expects.
Resumable: existing files are skipped unless --refresh.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_g87e_data as F  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "g87e_wide")
POOLS_JSON = os.path.join(ROOT, "research", "pools_cache.json")

MIN_LIQ = 100_000.0      # desk trades this band
MAX_LIQ = 2_000_000.0
MIN_VOL24 = 50_000.0     # skip dead pools that will have no fills
MIN_TURNOVER = 3.0       # vol24 / liq: real meme churn, not a parked stablecoin
PAGES = 3                # 3 x 1000 bars ~= 10 days of 5m
MIN_BARS = 400

# Base assets that are NOT memecoins. A 15% pullback on these is noise, and they
# were what a naive "sort by liquidity" selection picked -- USDT/USDC and RAY
# move 0.35% per 5m bar vs 2.9% for the memes the desk actually trades.
NON_MEME = {
    "USDC", "USDT", "USDG", "SOL", "WSOL", "MSOL", "JITOSOL", "BSOL", "INF",
    "RAY", "KMNO", "JUP", "JTO", "PYTH", "ORCA", "MNDE", "BONK", "WIF", "POPCAT",
    "TRUMP", "FARTCOIN", "PENGU", "W", "ONDO", "ENA", "AERO", "MOBILE", "HNT",
}


def _base(name: str) -> str:
    return (name or "").split("/")[0].strip().upper()


def candidates():
    raw = json.load(open(POOLS_JSON))
    out = []
    for p in raw:
        try:
            liq = float(p.get("liquidity") or 0)
            vol = float(p.get("vol_h24") or 0)
        except (TypeError, ValueError):
            continue
        if not (MIN_LIQ <= liq <= MAX_LIQ) or vol < MIN_VOL24:
            continue
        addr, name = p.get("address"), p.get("name") or ""
        if not addr or _base(name) in NON_MEME:
            continue
        turn = vol / liq if liq else 0.0
        if turn < MIN_TURNOVER:
            continue
        out.append((addr, name or addr[:8], liq, vol, turn))
    # highest turnover first: the most meme-like, most tradeable pools
    out.sort(key=lambda x: -x[4])
    return out


def fetch_paged(pool: str, pages: int = PAGES) -> list[dict]:
    bars: dict[float, dict] = {}
    oldest = None
    for _ in range(pages):
        url = (f"{F.GT}/networks/solana/pools/{pool}/ohlcv/minute"
               f"?aggregate=5&limit=1000&currency=usd")
        if oldest is not None:
            url += f"&before_timestamp={int(oldest)}"
        try:
            body = F._get(url)
        except Exception as e:  # noqa: BLE001
            print(f"    ! page failed: {e}")
            break
        raw = (body.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
        if not raw:
            break
        for ts, o, h, l, c, v in raw:
            try:
                c = float(c)
                if c <= 0:
                    continue
                bars[float(ts)] = {"ts": float(ts), "open": float(o), "high": float(h),
                                   "low": float(l), "close": c, "vol": float(v or 0)}
            except (TypeError, ValueError):
                continue
        oldest = min(float(x[0]) for x in raw)
        time.sleep(1.2)
    return [bars[k] for k in sorted(bars)]


def main():
    os.makedirs(CACHE, exist_ok=True)
    refresh = "--refresh" in sys.argv
    limit = None
    for a in sys.argv[1:]:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])

    cands = candidates()
    if limit:
        cands = cands[:limit]
    print(f"{len(cands)} candidate pools in ${MIN_LIQ:,.0f}-${MAX_LIQ:,.0f} liq band")

    ok = skipped = failed = 0
    for n, (addr, label, liq, vol, turn) in enumerate(cands, 1):
        p = os.path.join(CACHE, f"{addr[:8]}_5m.json")
        if os.path.exists(p) and not refresh:
            skipped += 1
            continue
        try:
            candles = fetch_paged(addr)
        except Exception as e:  # noqa: BLE001
            print(f"[{n}/{len(cands)}] {label:22s} FAILED {e}")
            failed += 1
            continue
        if len(candles) < MIN_BARS:
            print(f"[{n}/{len(cands)}] {label:22s} only {len(candles)} bars, skip")
            failed += 1
            continue
        with open(p, "w") as f:
            json.dump({"pool": addr, "label": label, "tf": "5m",
                       "liquidity": liq, "vol_h24": vol,
                       "fetched_at": time.time(), "candles": candles}, f)
        ok += 1
        print(f"[{n}/{len(cands)}] {label:22s} {len(candles):5d} bars  liq=${liq:,.0f} turn={turn:.1f}")
    print(f"\ndone: {ok} fetched, {skipped} cached, {failed} skipped/failed -> {CACHE}")


if __name__ == "__main__":
    main()
