"""Small OHLC series for the ticket chart.

Fresh coins (scalp, under 6 hours) get 15s/30s tabs, built from the desk's
own price ticks so we do not invent a GeckoTerminal 15s candle — that API
does not exist. Older coins (swing) get 15m/1H/4H from GeckoTerminal OHLCV,
fetched through the scanner's rate limiter and cache. If the budget is empty
or the endpoint is cooling, we return cached or tick-built bars and never
sleep, so the chart cannot add a new 429 storm.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from config import EXITS

SCALP_MAX_AGE_H = float(EXITS.get("scalp_max_age_hours", 6.0))
SCALP_TABS = ["15s", "30s"]
SWING_TABS = ["15m", "1H", "4H"]
MAX_CANDLES = 80
MAX_TICKS = 2400
MIN_TICK_BARS = 8

# tf -> (bucket_seconds, geckoterminal (timeframe, aggregate) or None)
_TF = {
    "15s": (15, None),
    "30s": (30, None),
    "1m": (60, ("minute", 1)),
    "15m": (15 * 60, ("minute", 15)),
    "1H": (3600, ("hour", 1)),
    "4H": (4 * 3600, ("hour", 4)),
}

# mint -> deque of (ts, price, volume)
_TICKS: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_TICKS))

# test hook: when set, network OHLCV is not used
_fetch_ohlcv = None


def reset() -> None:
    _TICKS.clear()


def tabs_for(*, horizon: str | None = None, age_hours: float | None = None) -> list[str]:
    """15s/30s for fresh coins; 15m/1H/4H for older ones (old desk timeframe.py)."""
    if horizon == "swing":
        return list(SWING_TABS)
    if horizon == "scalp":
        return list(SCALP_TABS)
    if age_hours is not None and float(age_hours) >= SCALP_MAX_AGE_H:
        return list(SWING_TABS)
    return list(SCALP_TABS)


def record_tick(mint: str, price, volume: float = 0.0, ts: float | None = None) -> None:
    """Append a price sample. Called from quote_many — no extra HTTP."""
    if not mint or price is None:
        return
    try:
        px = float(price)
    except (TypeError, ValueError):
        return
    if px <= 0:
        return
    _TICKS[mint].append((float(ts if ts is not None else time.time()), px, float(volume or 0.0)))


def ticks(mint: str) -> list[tuple[float, float, float]]:
    return list(_TICKS.get(mint) or ())


def bucket_ticks(rows: list[tuple[float, float, float]], bucket_s: int) -> list[dict]:
    """Resample (ts, price, vol) into {t,o,h,l,c,v} bars."""
    out: dict[int, list] = {}
    for ts, px, vol in rows:
        k = int(ts // bucket_s) * bucket_s
        bar = out.get(k)
        if bar is None:
            out[k] = [k, px, px, px, px, vol or 0.0]
        else:
            bar[2] = max(bar[2], px)
            bar[3] = min(bar[3], px)
            bar[4] = px
            bar[5] += vol or 0.0
    return [
        {"t": int(b[0]), "o": b[1], "h": b[2], "l": b[3], "c": b[4], "v": round(b[5], 6)}
        for b in (out[k] for k in sorted(out))
    ][-MAX_CANDLES:]


def _parse_ohlcv(body) -> list[dict]:
    lst = (((body or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    candles = []
    for row in lst:
        if not row or len(row) < 5:
            continue
        ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        v = row[5] if len(row) > 5 else 0
        try:
            candles.append({
                "t": int(ts), "o": float(o), "h": float(h),
                "l": float(l), "c": float(c), "v": float(v or 0),
            })
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda x: x["t"])
    return candles[-MAX_CANDLES:]


def _pool_address(mint: str) -> str | None:
    from desk import scanner
    row = scanner._POOL.get(mint)
    if row:
        pool = (row.get("token") or {}).get("pool")
        if pool:
            return pool
    for p in scanner.cached_raw_pairs(mint) or []:
        addr = p.get("pairAddress")
        if addr:
            return addr
    return None


def _network_ohlcv(mint: str, tf: str, session=None) -> list[dict]:
    """One cached, rate-limited GeckoTerminal OHLCV call. Never sleeps."""
    if _fetch_ohlcv is not None:
        body = _fetch_ohlcv(mint, tf)
        if body is None:
            return []
        if isinstance(body, list):
            return body[-MAX_CANDLES:]
        return _parse_ohlcv(body)
    spec = _TF.get(tf, (None, None))[1]
    if not spec:
        return []
    pool = _pool_address(mint)
    if not pool:
        return []
    timeframe, aggregate = spec
    from desk import scanner
    import requests
    s = session or requests.Session()
    now = time.time()
    ep = {
        "id": f"gt:ohlcv:{pool}:{timeframe}:{aggregate}",
        "source": "geckoterminal",
        "url": f"{scanner.GT}/networks/solana/pools/{pool}/ohlcv/{timeframe}",
        "params": {"aggregate": aggregate, "limit": 100, "currency": "usd"},
        "label": f"ohlcv {tf} pool={pool[:8]}…",
        "kind": "gt_ohlcv",
    }
    body, _fresh = scanner._fetch_endpoint(s, ep, now)
    return _parse_ohlcv(body) if body else []


def series(mint: str, tf: str | None = None, *,
           horizon: str | None = None, age_hours: float | None = None,
           entry: float | None = None, session=None) -> dict:
    """Candles + tab list for one mint. Safe to call on every dashboard refresh."""
    tabs = tabs_for(horizon=horizon, age_hours=age_hours)
    want = tf if tf in _TF else tabs[0]
    fallback = False
    source = "ticks"
    bucket, gt = _TF[want]
    candles = bucket_ticks(ticks(mint), bucket) if not gt else []

    if gt:
        net = _network_ohlcv(mint, want, session)
        if net:
            candles = net
            source = "geckoterminal"
        elif not candles:
            # last resort: resample ticks at this bucket
            candles = bucket_ticks(ticks(mint), bucket)
            source = "ticks" if candles else "none"
    elif len(candles) < MIN_TICK_BARS:
        # 15s/30s without enough local samples: fall back to 1m OHLCV
        net = _network_ohlcv(mint, "1m", session)
        if net:
            candles = net
            want = "1m"
            fallback = True
            source = "geckoterminal"
            if "1m" not in tabs:
                tabs = list(tabs) + ["1m"]

    return {
        "mint": mint,
        "tf": want,
        "tabs": tabs,
        "fallback": fallback,
        "source": source,
        "entry": entry,
        "candles": candles,
        "note": ("sub-minute samples were thin, showing 1m candles"
                 if fallback else None),
    }
