"""Fetch real GeckoTerminal OHLCV for a basket of Solana meme pools and cache it.

The cache is what makes the backtest reproducible: once a pool/timeframe file
exists, the backtest reads it and never touches the network. Delete the cache to
re-fetch. No keys, no auth, public endpoint, rate limited to one call per second.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

GT = "https://api.geckoterminal.com/api/v2"
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "g87e")
UA = {"Accept": "application/json", "User-Agent": "solana-desk-g87e-backtest/1.0"}

# pool id -> label. Majors give a long history, fresh memes give the regime the
# strategy actually trades in.
POOLS = {
    "5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9": "BONK/SOL",
    "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx": "WIF/SOL",
    "FRhB8L7Y9Qq41qZXYLtC2nw8An1RJfLLxRF2x9RwLLMo": "POPCAT/SOL",
    "3C5YE97HADPDxZehYq9Cis8AXr9aNyrUsczKzE1nDbW9": "TRUMP/USDC",
    "2XhQqp1vDVxtaa21bPsekQF5gjts9euqRPSZ6Urs41qk": "FOOD/SOL",
    "DxbQyPTjckQTSUWyUqJB11HpupPyLGJZ5x4z7ufHp3Gh": "AMC/SOL",
    "FbR7WmU8WAAzSLsgwthiqAivzifdsW381FW3p4Vkmiru": "Sakura/SOL",
    "AQFC3QfATJggPQEuQGS65RuAQq5u2Pin6MYjz1eu4s31": "inuhao/SOL",
    "GCApLjm8aviTumEXSYPCVMGK6Rer9KG7gUN4L9YKP394": "SHARKBUX/SOL",
    "7fxqDbq1CJjKkWy51UbQVyGcct6VNqCzhjq4HrTjcNje": "ANTH/SOL",
}

# label -> (geckoterminal timeframe, aggregate, seconds per bar)
TIMEFRAMES = {
    "5m": ("minute", 5, 300),
    "15m": ("minute", 15, 900),
    "1h": ("hour", 1, 3600),
}


def _get(url: str, tries: int = 3) -> dict:
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(8 * (attempt + 1))
                continue
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError(f"fetch failed: {url} ({last})")


def fetch(pool: str, tf: str, limit: int = 1000) -> list[dict]:
    timeframe, aggregate, secs = TIMEFRAMES[tf]
    url = (f"{GT}/networks/solana/pools/{pool}/ohlcv/{timeframe}"
           f"?aggregate={aggregate}&limit={limit}&currency=usd")
    body = _get(url)
    raw = (body.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    out = []
    for ts, o, h, l, c, v in raw:
        if not c or float(c) <= 0:
            continue
        out.append({"ts": float(ts), "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c), "vol": float(v or 0)})
    out.sort(key=lambda x: x["ts"])
    return out


def path(pool: str, tf: str) -> str:
    return os.path.join(CACHE, f"{pool[:8]}_{tf}.json")


def load(pool: str, tf: str, label: str = "", refresh: bool = False) -> list[dict]:
    os.makedirs(CACHE, exist_ok=True)
    p = path(pool, tf)
    if os.path.exists(p) and not refresh:
        with open(p) as f:
            return json.load(f)["candles"]
    candles = fetch(pool, tf)
    with open(p, "w") as f:
        json.dump({"pool": pool, "label": label or pool[:8], "tf": tf,
                   "fetched_at": time.time(), "candles": candles}, f)
    time.sleep(1.2)   # be polite: public endpoint, 30 calls/min
    return candles


def load_all(refresh: bool = False) -> dict:
    """{label: {tf: candles}} — one entry per pool that returned data."""
    out = {}
    for pool, label in POOLS.items():
        out[label] = {}
        for tf in TIMEFRAMES:
            try:
                c = load(pool, tf, label, refresh)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {label} {tf}: {e}")
                c = []
            if len(c) >= 60:
                out[label][tf] = c
            print(f"  {label:14s} {tf:4s} {len(c):5d} bars")
    return {k: v for k, v in out.items() if v}


if __name__ == "__main__":
    import sys
    data = load_all(refresh="--refresh" in sys.argv)
    print(f"\n{len(data)} pools cached -> {CACHE}")
