"""Small OHLC series for the ticket chart.

Fresh coins (scalp, under 6 hours) get 15s/30s tabs, built from the desk's
own price ticks so we do not invent a GeckoTerminal 15s candle — that API
does not exist. Older coins (swing) get 15m/1H/4H from GeckoTerminal OHLCV,
fetched through the scanner's rate limiter and cache. The agent cycle prefetches
one open-position pool at a time and the dashboard reads the last-good copy, so
a poll never adds a request and a 429 still returns real candles.
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

# Last-good OHLCV per "pool:tf". The dashboard reads this; only the agent cycle
# (prefetch) refreshes it, one pool per cycle, through the scanner's host limiter.
# A 429 therefore still returns real candles instead of an empty or flat chart.
REFRESH_S = {"1m": 60, "15m": 300, "1H": 900, "4H": 1800}          # re-fetch when older
LAST_GOOD_S = {"1m": 300, "15m": 900, "1H": 7200, "4H": 21600}     # serve stale up to this
MISS_RETRY_S = 60                                                  # min gap between miss fetches
_OHLCV: dict[str, dict] = {}
_ATTEMPT: dict[str, float] = {}


def reset() -> None:
    _TICKS.clear()
    _OHLCV.clear()
    _ATTEMPT.clear()


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


def _ohlcv_key(pool: str, tf: str) -> str:
    return f"{pool}:{tf}"


def _fetch_and_store(pool: str, tf: str, session, now: float) -> list[dict]:
    """One GeckoTerminal OHLCV call via the scanner's cache, budget and host cooldown."""
    spec = _TF.get(tf, (None, None))[1]
    if not spec:
        return []
    timeframe, aggregate = spec
    from desk import scanner
    import requests
    s = session or requests.Session()
    ep = {
        "id": f"gt:ohlcv:{pool}:{timeframe}:{aggregate}",
        "source": "geckoterminal",
        "url": f"{scanner.GT}/networks/solana/pools/{pool}/ohlcv/{timeframe}",
        "params": {"aggregate": aggregate, "limit": 100, "currency": "usd"},
        "label": f"ohlcv {tf} pool={pool[:8]}…",
        "kind": "gt_ohlcv",
    }
    key = _ohlcv_key(pool, tf)
    _ATTEMPT[key] = now
    body, fresh = scanner._fetch_endpoint(s, ep, now)
    candles = _parse_ohlcv(body) if body else []
    if candles and fresh:
        _OHLCV[key] = {"at": now, "candles": candles}
    return candles


def _network_ohlcv(mint: str, tf: str, session=None) -> list[dict]:
    """Candles for the dashboard, served from the last-good cache. Never sleeps.

    A fresh entry is returned as is. A stale one is returned too (up to
    LAST_GOOD_S) and left for the agent cycle's prefetch to refresh. Only a
    complete miss fetches, at most once per MISS_RETRY_S per pool and timeframe.
    """
    if _fetch_ohlcv is not None:
        body = _fetch_ohlcv(mint, tf)
        if body is None:
            return []
        if isinstance(body, list):
            return body[-MAX_CANDLES:]
        return _parse_ohlcv(body)
    if not _TF.get(tf, (None, None))[1]:
        return []
    pool = _pool_address(mint)
    if not pool:
        return []
    now = time.time()
    key = _ohlcv_key(pool, tf)
    row = _OHLCV.get(key)
    if row and now - row["at"] <= LAST_GOOD_S.get(tf, 600):
        return row["candles"]
    if now - _ATTEMPT.get(key, 0.0) < MISS_RETRY_S:
        return []
    return _fetch_and_store(pool, tf, session, now)


def prefetch(session, positions, now: float | None = None):
    """Refresh OHLCV for at most ONE open-position pool. Call once per agent cycle.

    `positions` is a list of (mint, horizon). Scalps need 1m candles (the 15s and
    30s tabs fall back to them); swings need 15m, 1H and 4H. Takes the
    most overdue (mint, tf) pair, missing ones first, and spends one call, or none if the host is cooling.
    Returns the (mint, tf) fetched, or None.
    """
    if _fetch_ohlcv is not None:
        return None
    from desk import scanner
    now = time.time() if now is None else now
    if scanner.host_cooling("geckoterminal", now):
        return None
    wanted = []                                   # (level, mint, tf, pool)
    for mint, horizon in positions or []:
        if not mint or str(mint).startswith("DEMO"):
            continue
        pool = _pool_address(mint)
        if not pool:
            continue
        for level, tf in enumerate(SWING_TABS if horizon == "swing" else ["1m"]):
            wanted.append((level, mint, tf, pool))
    wanted.sort(key=lambda w: w[0])               # first tab of every position before any second tab
    due = []
    for _level, mint, tf, pool in wanted:
        row = _OHLCV.get(_ohlcv_key(pool, tf))
        if row is None:
            due.append((-1.0, mint, tf, pool))
        elif now - row["at"] >= REFRESH_S[tf]:
            due.append((row["at"] - now, mint, tf, pool))   # older is more negative
    if not due:
        return None
    due.sort(key=lambda d: d[0])                  # stable: missing first, then oldest
    _, mint, tf, pool = due[0]
    _fetch_and_store(pool, tf, session, now)
    return mint, tf


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
