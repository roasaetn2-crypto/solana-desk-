"""Seat 1: the scanner. Pulls fresh Solana pools and kills everything a comparison can kill.

GeckoTerminal is free and needs no key, but the public cap is about 30 calls
a minute (we stay at <= 10, and only 1-2 calls per cycle). DexScreener is the
second source. Endpoints are rotated, cached, and cooled on 429 so a cycle
never sleeps on a rate limit. A rolling mint-keyed pool keeps candidates
alive when one source is cooling.

Jupiter's free, keyless token API (tokens/v2) is the third host. It serves
two jobs: a rotated discovery list (top trending / top traded, 1h), and
hydrating the mints the PumpPortal websocket saw launch (one batched search
call, up to 100 mints). Rows from that second job carry source="pumpportal".
Render's shared outbound IP gets 429s from GeckoTerminal and DexScreener for
hours at a time, so these two keep the candidate pool filled. Every host has
its own token bucket and its own jittered 429 cooldown.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime

import requests

from config import ESTABLISHED, SCAN, SCAN_FETCH

log = logging.getLogger("desk.scanner")

GT = "https://api.geckoterminal.com/api/v2"
DS = "https://api.dexscreener.com"
DEX = "https://api.dexscreener.com/latest/dex/tokens"
DS_TOKENS_V1 = "https://api.dexscreener.com/tokens/v1/solana"
# Jupiter tokens v2: free, no key. lite-api first; api.jup.ag if lite-api is retired.
JUP_BASES = ("https://lite-api.jup.ag/tokens/v2", "https://api.jup.ag/tokens/v2")
_jup_base = [0]
SOURCES = ("geckoterminal", "dexscreener", "jupiter", "pumpportal")
# Which HTTP host a source's calls go to (pumpportal rows are hydrated through Jupiter).
_SOURCE_HOST = {"geckoterminal": "geckoterminal", "dexscreener": "dexscreener",
                "jupiter": "jupiter", "pumpportal": "jupiter"}
# Same-mint precedence inside the pool TTL: a higher number is not replaced by a lower one.
_RANK = {"geckoterminal": 3, "dexscreener": 2, "jupiter": 1, "pumpportal": 1}
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "solana-trading-desk/1.1 (paper-scanner; +https://github.com/roasaetn2-crypto/solana-desk-)",
}

# Base mints we never treat as a tradable pool (the quote side of a pair).
_QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "USD1ttGY1N17mxyTXcFmcrzJJPDZX1apYe0NAKwrpump",  # USD1 (sometimes)
}

# DexScreener dexId -> the names GeckoTerminal / SCAN["pump_dexes"] use.
_DEX_ALIAS = {
    "pumpfun": "pump-fun",
    "pump-fun": "pump-fun",
    "pumpswap": "pumpswap",
}

# Fetch outcome of the last new_tokens() call. The agent prints this.
LAST: dict = {name: {"pools": 0, "calls": 0, "errors": []} for name in SOURCES}

# Cumulative HTTP counters (not reset each cycle) for simulations and tests.
CALLS: dict = {"geckoterminal": 0, "dexscreener": 0, "jupiter": 0, "429": 0}

# ---------- rate limit / cache / cooldown state ----------


class TokenBucket:
    """Refills `per_min` tokens per minute, up to `burst`. Never blocks."""

    def __init__(self, per_min: float, burst: int):
        self.per_min = float(per_min)
        self.burst = float(burst)
        self.tokens = float(burst)
        self.t = time.time()

    def reset(self, now: float | None = None) -> None:
        self.tokens = self.burst
        self.t = time.time() if now is None else now

    def allow(self, n: float = 1, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        elapsed = max(0.0, now - self.t)
        self.tokens = min(self.burst, self.tokens + elapsed * (self.per_min / 60.0))
        self.t = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


def _new_buckets() -> dict[str, TokenBucket]:
    return {
        "geckoterminal": TokenBucket(SCAN_FETCH["gt_per_min"], SCAN_FETCH["gt_burst"]),
        "dexscreener": TokenBucket(SCAN_FETCH["ds_per_min"], SCAN_FETCH["ds_burst"]),
        "jupiter": TokenBucket(SCAN_FETCH["jup_per_min"], SCAN_FETCH["jup_burst"]),
    }


_BUCKETS = _new_buckets()
_cycle = 0
_gt_cursor = [0]           # next GeckoTerminal rotation slot; advances only when a slot was served
# endpoint id -> {"until": ts, "strikes": int}  (non-429 errors only)
_COOL: dict[str, dict] = {}
# host ("geckoterminal" | "dexscreener") -> {"until": ts, "strikes": int}
# A 429 is a per-IP limit, so it cools the whole host, not one endpoint.
_HOST_COOL: dict[str, dict] = {}
# endpoint id -> {"exp": ts, "body": ...}
_EP_CACHE: dict[str, dict] = {}
# mint -> {"token": dict, "at": ts}  (rolling candidate pool, ~10 min)
_POOL: dict[str, dict] = {}
# mint -> {"exp": ts, "pairs": list}
_PAIR_CACHE: dict[str, dict] = {}
# mint -> {"exp": ts, "q": dict}
_QUOTE_CACHE: dict[str, dict] = {}
# mint -> {"exp": ts, "row": raw Jupiter token}  (socials / quote fallback)
_JUP_ROWS: dict[str, dict] = {}
# mint -> ts of the last Jupiter hydrate (PumpPortal mints), so we rotate through them
_JUP_HYDRATED: dict[str, float] = {}


def _host_of(source: str) -> TokenBucket:
    return _BUCKETS[_SOURCE_HOST.get(source, source)]


def _jup_enabled() -> bool:
    """Jupiter discovery / PumpPortal hydrate. Off under DESK_OFFLINE=1 like other optional lookups."""
    from desk.netutil import offline
    return bool(SCAN_FETCH.get("jup_enabled", True)) and not offline()


def _jitter(seconds: float) -> float:
    """Spread retries so probes never sync up. Only ever lengthens: a server's
    Retry-After is a floor we must not undercut."""
    j = float(SCAN_FETCH.get("cooldown_jitter", 0.15))
    return seconds * random.uniform(1.0, 1.0 + j)


def reset_runtime() -> None:
    """Clear limiter / cache / cooldown / cycle state. Used by tests."""
    global _cycle, _BUCKETS
    _cycle = 0
    _gt_cursor[0] = 0
    _BUCKETS = _new_buckets()
    _jup_base[0] = 0
    _JUP_ROWS.clear()
    _JUP_HYDRATED.clear()
    _COOL.clear()
    _HOST_COOL.clear()
    _EP_CACHE.clear()
    _POOL.clear()
    _PAIR_CACHE.clear()
    _QUOTE_CACHE.clear()
    CALLS["geckoterminal"] = 0
    CALLS["dexscreener"] = 0
    CALLS["jupiter"] = 0
    CALLS["429"] = 0
    _reset_last()
    from desk import chart
    chart.reset()


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _i(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _created_at_iso(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _age_minutes(iso: str | None) -> float:
    created = _created_at_iso(iso)
    if created is None:
        return 0.0
    return max(0.0, (time.time() - created) / 60)


def _reset_last() -> None:
    for name in SOURCES:
        LAST[name] = {"pools": 0, "calls": 0, "errors": []}


def _note(source: str, message: str) -> None:
    """Record a visible, secret-free failure and log it (once per message)."""
    log.warning("%s: %s", source, message)
    errs = LAST[source]["errors"]
    if message not in errs and len(errs) < 12:
        errs.append(message)


def fetch_report() -> dict:
    """Last fetch, for the agent log: per-source counts and status reasons."""
    warnings = []
    for name, row in LAST.items():
        for err in row["errors"]:
            warnings.append(f"{name} {err}")
    return {
        **{name: dict(LAST[name]) for name in SOURCES},
        "cooling": {h: round(host_cooling_until(h) - time.time())
                    for h in ("geckoterminal", "dexscreener", "jupiter") if host_cooling(h)},
        "warnings": warnings,
        "calls_total": dict(CALLS),
        "cycle": _cycle,
    }


def source_label(tokens: list[dict]) -> str:
    counts = source_counts(tokens)
    parts = [name for name in SOURCES if counts[name]]
    if parts:
        return "+".join(parts)
    return "all-sources-empty"


def source_counts(tokens: list[dict]) -> dict[str, int]:
    return {name: sum(1 for t in tokens if t.get("source") == name) for name in SOURCES}


def _host_name(endpoint_id: str) -> str:
    if endpoint_id.startswith("gt:"):
        return "geckoterminal"
    if endpoint_id.startswith("jup:"):
        return "jupiter"
    return "dexscreener"


def host_cooling_until(source: str, now: float | None = None) -> float:
    now = time.time() if now is None else now
    until = float((_HOST_COOL.get(source) or {}).get("until") or 0)
    return until if until > now else 0.0


def host_cooling(source: str, now: float | None = None) -> bool:
    """True while a 429 cooldown is active for the whole host."""
    return host_cooling_until(source, now) > 0


def cooling_until(endpoint_id: str, now: float | None = None) -> float:
    now = time.time() if now is None else now
    until = float((_COOL.get(endpoint_id) or {}).get("until") or 0)
    until = until if until > now else 0.0
    return max(until, host_cooling_until(_host_name(endpoint_id), now))


def is_cooling(endpoint_id: str, now: float | None = None) -> bool:
    return cooling_until(endpoint_id, now) > 0


# ---------- parsers (unchanged shapes) ----------

def parse_pool(pool: dict, symbols: dict[str, str]) -> dict | None:
    """One GeckoTerminal pool -> the desk's token dict. Field names are fixed here, once."""
    a = pool.get("attributes", {})
    rel = pool.get("relationships", {})
    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    dex_id = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
    if not base_id.startswith("solana_"):
        return None
    mint = base_id.split("_", 1)[1]
    tx_h1 = (a.get("transactions") or {}).get("h1") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    return {
        "mint": mint,
        "symbol": symbols.get(base_id, a.get("name", "?").split(" / ")[0]),
        "name": a.get("name", "?"),
        "pool": a.get("address"),
        "dex": dex_id,
        "price_usd": _f(a.get("base_token_price_usd")),
        "mcap_usd": _f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")),
        "liquidity_usd": _f(a.get("reserve_in_usd")),
        "volume_m5": _f(vol.get("m5")), "volume_h1": _f(vol.get("h1")),
        "volume_h6": _f(vol.get("h6")), "volume_h24": _f(vol.get("h24")),
        "buys_h1": int(tx_h1.get("buys") or 0), "sells_h1": int(tx_h1.get("sells") or 0),
        "buyers_h1": int(tx_h1.get("buyers") or 0),
        "change_m5": _f(chg.get("m5")), "change_h1": _f(chg.get("h1")),
        "change_h6": _f(chg.get("h6")),
        "age_minutes": round(_age_minutes(a.get("pool_created_at")), 1),
        "created_at": _created_at_iso(a.get("pool_created_at")),
        "source": "geckoterminal",
    }


def parse_dex_pair(pair: dict) -> dict | None:
    """One DexScreener pair -> the same token dict the rest of the desk already uses."""
    if (pair.get("chainId") or "").lower() != "solana":
        return None
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    mint = base.get("address") or ""
    if not mint or mint in _QUOTE_MINTS:
        return None
    tx_h1 = (pair.get("txns") or {}).get("h1") or {}
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    created_ms = pair.get("pairCreatedAt")
    created_at = (float(created_ms) / 1000.0) if created_ms else None
    age = round((time.time() - created_at) / 60.0, 1) if created_at else 0.0
    dex = (pair.get("dexId") or "").lower()
    dex = _DEX_ALIAS.get(dex, dex)
    buys = _i(tx_h1.get("buys"))
    symbol = base.get("symbol") or "?"
    quote_sym = quote.get("symbol") or "SOL"
    return {
        "mint": mint,
        "symbol": symbol,
        "name": f"{symbol} / {quote_sym}",
        "pool": pair.get("pairAddress"),
        "dex": dex,
        "price_usd": _f(pair.get("priceUsd")),
        "mcap_usd": _f(pair.get("marketCap")) or _f(pair.get("fdv")),
        "liquidity_usd": _f((pair.get("liquidity") or {}).get("usd")),
        "volume_m5": _f(vol.get("m5")), "volume_h1": _f(vol.get("h1")),
        "volume_h6": _f(vol.get("h6")), "volume_h24": _f(vol.get("h24")),
        "buys_h1": buys, "sells_h1": _i(tx_h1.get("sells")),
        "buyers_h1": buys,  # DexScreener has no unique-buyer count; buys is the stand-in
        "change_m5": _f(chg.get("m5")), "change_h1": _f(chg.get("h1")),
        "change_h6": _f(chg.get("h6")),
        "age_minutes": age,
        "created_at": created_at,
        "source": "dexscreener",
    }


def _jup_stats_vol(st: dict) -> float:
    return _f(st.get("buyVolume")) + _f(st.get("sellVolume"))


def parse_jup_token(row: dict, source: str = "jupiter") -> dict | None:
    """One Jupiter tokens/v2 row -> the desk's token dict (same keys as parse_pool)."""
    if not isinstance(row, dict):
        return None
    mint = row.get("id")
    if not isinstance(mint, str) or not (30 <= len(mint) <= 50) or mint in _QUOTE_MINTS:
        return None
    s5 = row.get("stats5m") or {}
    s1 = row.get("stats1h") or {}
    s6 = row.get("stats6h") or {}
    s24 = row.get("stats24h") or {}
    first = row.get("firstPool") or {}
    created_at = _created_at_iso(first.get("createdAt") or row.get("createdAt"))
    age = round(max(0.0, (time.time() - created_at) / 60.0), 1) if created_at else 0.0
    launchpad = (row.get("launchpad") or "").lower()
    if launchpad == "pump.fun":
        dex = "pumpswap" if row.get("graduatedPool") else "pump-fun"
    else:
        dex = launchpad or "jupiter"
    symbol = str(row.get("symbol") or "?")[:24]
    buys = _i(s1.get("numBuys"))
    return {
        "mint": mint,
        "symbol": symbol,
        "name": f"{symbol} / SOL",
        "pool": row.get("graduatedPool") or first.get("id") or mint,
        "dex": dex,
        "price_usd": _f(row.get("usdPrice")),
        "mcap_usd": _f(row.get("mcap")) or _f(row.get("fdv")),
        "liquidity_usd": _f(row.get("liquidity")),
        "volume_m5": _jup_stats_vol(s5), "volume_h1": _jup_stats_vol(s1),
        "volume_h6": _jup_stats_vol(s6), "volume_h24": _jup_stats_vol(s24),
        "buys_h1": buys, "sells_h1": _i(s1.get("numSells")),
        "buyers_h1": _i(s1.get("numTraders")) or buys,
        "change_m5": _f(s5.get("priceChange")), "change_h1": _f(s1.get("priceChange")),
        "change_h6": _f(s6.get("priceChange")),
        "age_minutes": age,
        "created_at": created_at,
        "source": source,
    }


# ---------- HTTP: one shot, no in-cycle sleep ----------

def _parse_retry_after(raw, default: float) -> float:
    if not raw:
        return default
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return default


def _retry_after(r: requests.Response, default: float) -> float:
    raw = r.headers.get("Retry-After") or r.headers.get("retry-after")
    return _parse_retry_after(raw, default)


def _cool(endpoint_id: str, source: str, wait: float, label: str, status: int, reason: str) -> None:
    """Start a cooldown and log it once.

    429 is per client IP, so it cools the whole host (every endpoint on it) with
    one exponential backoff that persists across cycles until a call succeeds.
    While the host is already cooling, further 429s are silent. Other errors
    cool only their own endpoint.
    """
    cap = SCAN_FETCH["cooldown_cap_s"]
    now = time.time()
    if status == 429:
        if host_cooling(source, now):
            return                      # one log line per cooldown start
        rec = _HOST_COOL.get(source) or {"until": 0.0, "strikes": 0}
        rec["strikes"] = int(rec.get("strikes") or 0) + 1
        exp = _jitter(min(cap, max(wait, SCAN_FETCH["cooldown_floor_s"]) * (2 ** (rec["strikes"] - 1))))
        rec["until"] = now + exp
        _HOST_COOL[source] = rec
        CALLS["429"] += 1
        _note(source, f"{label} -> 429 {reason}, cooling {source} for {exp:.0f}s")
        return
    floor = min(10.0, wait)
    rec = _COOL.get(endpoint_id) or {"until": 0.0, "strikes": 0}
    rec["strikes"] = int(rec.get("strikes") or 0) + 1
    exp = _jitter(min(cap, max(wait, floor) * (2 ** max(0, rec["strikes"] - 1))))
    rec["until"] = now + exp
    _COOL[endpoint_id] = rec
    _note(source, f"{label} -> {status} {reason}, cooling {exp:.0f}s")


def note_429(source: str, label: str, retry_after=None) -> None:
    """For other modules that call a scanner host directly (promo, dexscreener)."""
    wait = _parse_retry_after(retry_after, SCAN_FETCH["cooldown_floor_s"])
    prefix = {"geckoterminal": "gt", "jupiter": "jup"}.get(source, "ds")
    _cool(f"{prefix}:external", source, wait, label,
          429, "Too Many Requests")


def _get_once(s: requests.Session, url: str, params: dict | None, source: str,
              label: str, endpoint_id: str):
    """Single GET. Never sleeps. On 429, cools the endpoint and returns None."""
    LAST[source]["calls"] += 1
    CALLS[source] += 1
    try:
        r = s.get(url, params=params, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        _note(source, f"{label} request failed ({type(e).__name__})")
        _cool(endpoint_id, source, 10.0, label, 0, type(e).__name__)
        return None
    if r.status_code == 429:
        wait = _retry_after(r, SCAN_FETCH["cooldown_floor_s"])
        _cool(endpoint_id, source, wait, label, 429, r.reason or "Too Many Requests")
        return None
    if r.status_code >= 500:
        _cool(endpoint_id, source, 8.0, label, r.status_code, r.reason or "server error")
        return None
    if r.status_code != 200:
        if source == "jupiter" and r.status_code in (401, 403, 404, 410):
            _jup_base[0] = (_jup_base[0] + 1) % len(JUP_BASES)   # try the other Jupiter base next
        if r.status_code in (401, 403, 408):
            _cool(endpoint_id, source, 30.0, label, r.status_code, r.reason or "error")
        else:
            _note(source, f"{label} -> {r.status_code} {r.reason or 'error'}")
        return None
    if endpoint_id in _COOL:
        _COOL[endpoint_id]["strikes"] = 0
        _COOL[endpoint_id]["until"] = 0.0
    if source in _HOST_COOL:
        _HOST_COOL[source]["strikes"] = 0
        _HOST_COOL[source]["until"] = 0.0
    return r


def _cached_body(endpoint_id: str, now: float):
    row = _EP_CACHE.get(endpoint_id)
    if row and row["exp"] > now:
        return row["body"], True
    if row:
        return row["body"], False  # stale, usable as a fallback
    return None, False


def _store_cache(endpoint_id: str, body, now: float) -> None:
    _EP_CACHE[endpoint_id] = {"exp": now + SCAN_FETCH["cache_ttl_s"], "body": body}


def _fetch_endpoint(s: requests.Session, ep: dict, now: float):
    """Return (body, fresh). Uses cache, rate budget, and cooldown. Never sleeps."""
    endpoint_id = ep["id"]
    source = ep["source"]
    label = ep["label"]
    cached, fresh = _cached_body(endpoint_id, now)
    if cached is not None and fresh:
        return cached, True
    if is_cooling(endpoint_id, now):
        return cached, False
    if not _host_of(source).allow(1, now=now):
        return cached, False
    r = _get_once(s, ep["url"], ep.get("params"), source, label, endpoint_id)
    if r is None:
        return cached, False
    try:
        body = r.json()
    except ValueError:
        _note(source, f"{label} -> 200 but body was not JSON")
        return cached, False
    _store_cache(endpoint_id, body, now)
    return body, True


def _fetch_live(s: requests.Session, ep: dict, now: float):
    """Like _fetch_endpoint but never serves a cached body (the URL varies per call)."""
    source = ep["source"]
    if is_cooling(ep["id"], now) or not _host_of(source).allow(1, now=now):
        return None
    r = _get_once(s, ep["url"], ep.get("params"), source, ep["label"], ep["id"])
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        _note(source, f"{ep['label']} -> 200 but body was not JSON")
        return None


# ---------- rolling pool ----------

def _stamp(t: dict, now: float) -> dict:
    row = dict(t)
    row["_age_at"] = now
    return row


def _view(t: dict, now: float) -> dict:
    origin = t.get("_age_at", now)
    view = {k: v for k, v in t.items() if not k.startswith("_")}
    created = t.get("created_at")
    if created:
        view["created_at"] = created
        view["age_minutes"] = round(max(0.0, (now - float(created)) / 60.0), 1)
    else:
        view["age_minutes"] = round(float(t.get("age_minutes") or 0) + (now - origin) / 60.0, 1)
    return view


def _keep(t: dict | None) -> dict | None:
    if not t:
        return None
    if SCAN["pump_only"] and t["dex"] not in SCAN["pump_dexes"]:
        return None
    return t


def _put_pool(t: dict, now: float) -> None:
    """Insert into the rolling pool. For the same mint, inside the pool TTL, a
    GeckoTerminal row beats DexScreener, which beats Jupiter / PumpPortal."""
    mint = t["mint"]
    prev = _POOL.get(mint)
    if (prev and _RANK.get(prev["token"].get("source"), 0) > _RANK.get(t.get("source"), 0)
            and now - prev["at"] < SCAN_FETCH["pool_ttl_s"]):
        return
    _POOL[mint] = {"token": _stamp(t, now), "at": now}


def _prune_pool(now: float) -> None:
    expired = [m for m, row in _POOL.items() if now - row["at"] > SCAN_FETCH["pool_ttl_s"]]
    for m in expired:
        _POOL.pop(m, None)


def _pool_tokens(now: float) -> list[dict]:
    _prune_pool(now)
    return [_view(row["token"], now) for row in _POOL.values()]


# ---------- endpoint schedules ----------

# GeckoTerminal feeds the rotation walks through: (id part, path, extra params).
# new_pools page 1 is fetched every cycle; everything else takes turns, one slot per cycle,
# so the desk sees more than the same 20 newest pools without spending more GT calls.
GT_FEEDS = (
    ("new_pools", "new_pools", {}),
    ("trending_5m", "trending_pools", {"duration": "5m"}),
    ("trending_1h", "trending_pools", {"duration": "1h"}),
)


def _gt_ep(feed: str, path: str, extra: dict, page: int) -> dict:
    q = "".join(f" {k}={v}" for k, v in extra.items())
    return {"id": f"gt:{feed}:{page}", "source": "geckoterminal",
            "url": f"{GT}/networks/solana/{path}",
            "params": {**extra, "page": page, "include": "base_token,dex"},
            "label": f"/networks/solana/{path}{q} page={page}",
            # trending lists mix in old pools: only launch-window rows enter the pool
            "kind": "gt_pools", "age_cut": path != "new_pools"}


def _gt_endpoints() -> list[dict]:
    """[new_pools page 1] + the rotation (page-major: every feed's page 1, then page 2, ...)."""
    first = _gt_ep(*GT_FEEDS[0], 1)
    rot = []
    for page in range(1, int(SCAN_FETCH.get("gt_max_page", 3)) + 1):
        for feed, path, extra in GT_FEEDS:
            if feed == "new_pools" and page == 1:
                continue
            rot.append(_gt_ep(feed, path, extra, page))
    return [first] + rot


def _gt_rotation() -> list[dict]:
    return _gt_endpoints()[1:]


def _jup_endpoints() -> list[dict]:
    base = JUP_BASES[_jup_base[0]]
    return [
        {"id": "jup:toptrending:1h", "source": "jupiter",
         "url": f"{base}/toptrending/1h", "params": {"limit": 100},
         "label": "jupiter toptrending/1h", "kind": "jup_tokens"},
        {"id": "jup:toptraded:1h", "source": "jupiter",
         "url": f"{base}/toptraded/1h", "params": {"limit": 100},
         "label": "jupiter toptraded/1h", "kind": "jup_tokens"},
    ]


def _jup_search_ep(mints: list[str], ep_id: str, label: str) -> dict:
    return {"id": ep_id, "source": "jupiter",
            "url": f"{JUP_BASES[_jup_base[0]]}/search",
            "params": {"query": ",".join(mints)},
            "label": f"{label} n={len(mints)}", "kind": "jup_tokens"}


def _remember_jup(rows, now: float) -> None:
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            _JUP_ROWS[row["id"]] = {"at": now, "row": row}
    if len(_JUP_ROWS) > 4000:                       # bounded: drop the oldest quarter
        for m, _ in sorted(_JUP_ROWS.items(), key=lambda kv: kv[1]["at"])[:1000]:
            _JUP_ROWS.pop(m, None)


def _absorb_jup(body, now: float, source: str = "jupiter") -> int:
    rows = body if isinstance(body, list) else []
    _remember_jup(rows, now)
    n = 0
    max_age_min = age_cut_minutes()
    for row in rows:
        t = _keep(parse_jup_token(row, source))
        # The trending lists are mostly months-old tokens; the age cut would reject them
        # every bench period and flood the log, so only launch-window rows enter the pool.
        if t and t["created_at"] and t["age_minutes"] <= max_age_min:
            _put_pool(t, now)
            n += 1
    return n


def _is_hot(t: dict | None) -> bool:
    """A launch with some real trading: worth re-pricing every minute."""
    if not t:
        return False
    return (t["buys_h1"] + t["sells_h1"] >= SCAN_FETCH["pp_hot_trades"]
            or t["volume_h1"] >= SCAN_FETCH["pp_hot_volume_usd"])


def _hydrate_order(mints: list[str], now: float) -> list[str]:
    """Hot launches due a refresh first, then never-priced ones (newest first), then cold ones."""
    hot, fresh, cold = [], [], []
    for m in mints:
        row = _JUP_HYDRATED.get(m)
        if row is None:
            fresh.append(m)
        elif row["hot"]:
            if now - row["at"] >= SCAN_FETCH["jup_rehydrate_s"]:
                hot.append(m)
        elif now - row["at"] >= SCAN_FETCH["pp_cold_recheck_s"]:
            cold.append(m)
    return hot + fresh + cold


def _hydrate_pumpportal(s: requests.Session, now: float) -> int:
    """One Jupiter search call for up to 100 mints PumpPortal saw launch."""
    from desk import pumpportal
    mints = pumpportal.recent_mints(now, min_age_s=SCAN["min_age_minutes"] * 60,
                                    max_age_s=SCAN_FETCH["pp_max_age_s"], limit=3000)
    pick = _hydrate_order(mints, now)[:int(SCAN_FETCH["jup_batch"])]
    if not pick:
        return 0
    body = _fetch_live(s, _jup_search_ep(pick, "jup:pumpportal", "jupiter search (pumpportal mints)"), now)
    if not isinstance(body, list):
        return 0
    _remember_jup(body, now)
    parsed = {}
    for row in body:
        t = parse_jup_token(row, "pumpportal")
        if t:
            parsed[t["mint"]] = t
    n = 0
    for m in pick:
        t = parsed.get(m)
        hot = _is_hot(t)
        _JUP_HYDRATED[m] = {"at": now, "hot": hot}
        t = _keep(t) if hot else None       # dead launches never reach the agent (no reject spam)
        if t:
            _put_pool(t, now)
            n += 1
    if len(_JUP_HYDRATED) > 6000:
        for m in [m for m, r in _JUP_HYDRATED.items() if now - r["at"] > SCAN_FETCH["pp_max_age_s"]]:
            _JUP_HYDRATED.pop(m, None)
    return n


def _ds_rotation() -> list[dict]:
    # One search query per slot — never both SOL and pump in the same cycle.
    return [
        {"id": "ds:search:pump", "source": "dexscreener",
         "url": f"{DS}/latest/dex/search", "params": {"q": "pump"},
         "label": "search q=pump", "kind": "ds_pairs"},
        {"id": "ds:search:sol", "source": "dexscreener",
         "url": f"{DS}/latest/dex/search", "params": {"q": "SOL"},
         "label": "search q=SOL", "kind": "ds_pairs"},
        {"id": "ds:profiles", "source": "dexscreener",
         "url": f"{DS}/token-profiles/latest/v1", "params": None,
         "label": "token-profiles/latest", "kind": "ds_mints"},
        {"id": "ds:boosts-latest", "source": "dexscreener",
         "url": f"{DS}/token-boosts/latest/v1", "params": None,
         "label": "token-boosts/latest", "kind": "ds_mints"},
        {"id": "ds:boosts-top", "source": "dexscreener",
         "url": f"{DS}/token-boosts/top/v1", "params": None,
         "label": "token-boosts/top", "kind": "ds_mints"},
    ]


def _absorb_gt(body: dict, now: float, age_cut: bool = False) -> int:
    if not isinstance(body, dict):
        return 0
    symbols = {i["id"]: i["attributes"].get("symbol", "?")
               for i in body.get("included", []) if i.get("type") == "token"}
    max_age_min = age_cut_minutes()
    n = 0
    for pool in body.get("data") or []:
        t = _keep(parse_pool(pool, symbols))
        if not t:
            continue
        if age_cut and t["age_minutes"] > max_age_min:
            continue
        _put_pool(t, now)
        n += 1
    return n


def _best_solana_pairs_by_mint(pairs: list[dict]) -> list[dict]:
    """Deepest Solana pair per mint, already parsed and pump-filtered."""
    best: dict[str, dict] = {}
    for raw in pairs or []:
        t = parse_dex_pair(raw)
        if not _keep(t):
            continue
        prev = best.get(t["mint"])
        if prev is None or t["liquidity_usd"] > prev["liquidity_usd"]:
            best[t["mint"]] = t
    return list(best.values())


def _pairs_from_body(body) -> list[dict]:
    if isinstance(body, dict):
        return list(body.get("pairs") or [])
    if isinstance(body, list) and body and isinstance(body[0], dict) and (
            "pairAddress" in body[0] or "priceUsd" in body[0]):
        return list(body)
    return []


def _absorb_pairs(body, now: float) -> int:
    tokens = _best_solana_pairs_by_mint(_pairs_from_body(body))
    for t in tokens:
        _put_pool(t, now)
    _remember_pairs(_pairs_from_body(body), now)
    return len(tokens)


def _mints_from_profiles(body) -> list[str]:
    rows = body if isinstance(body, list) else []
    out, seen = [], set()
    for row in rows:
        if (row.get("chainId") or "").lower() != "solana":
            continue
        addr = row.get("tokenAddress") or ""
        if addr and addr not in seen and addr not in _QUOTE_MINTS:
            seen.add(addr)
            out.append(addr)
    return out


def _remember_pairs(pairs: list[dict], now: float) -> None:
    ttl = max(SCAN_FETCH["quote_ttl_s"], 30)
    by_mint: dict[str, list] = {}
    for p in pairs or []:
        if (p.get("chainId") or "").lower() != "solana":
            continue
        mint = ((p.get("baseToken") or {}).get("address")) or ""
        if not mint:
            continue
        by_mint.setdefault(mint, []).append(p)
    for mint, group in by_mint.items():
        _PAIR_CACHE[mint] = {"exp": now + ttl, "pairs": group}


def cached_raw_pairs(mint: str, now: float | None = None) -> list[dict] | None:
    """Raw DexScreener pairs for a mint, if a recent fetch is still warm."""
    now = time.time() if now is None else now
    row = _PAIR_CACHE.get(mint)
    if not row or row["exp"] <= now:
        return None
    return row["pairs"]


def _hydrate_mints(s: requests.Session, mints: list[str], now: float) -> int:
    """One batch (max 30) via /tokens/v1/solana/{mints}. Skip mints already in the pool."""
    need, seen = [], set()
    for m in mints:
        if m in seen or m in _POOL:
            continue
        seen.add(m)
        need.append(m)
        if len(need) >= 30:
            break
    if not need:
        return 0
    ep = {
        "id": "ds:tokens-v1",
        "source": "dexscreener",
        "url": f"{DS_TOKENS_V1}/{','.join(need)}",
        "params": None,
        "label": f"tokens/v1 batch n={len(need)}",
        "kind": "ds_pairs",
    }
    body, _fresh = _fetch_endpoint(s, ep, now)
    if body is None:
        return 0
    return _absorb_pairs(body, now)


def _schedule(cycle: int) -> list[dict]:
    """GeckoTerminal: new_pools page 1 plus SCAN["pages"] rotated feed/page slots (the cursor
    moves on only after a slot was actually served), and exactly one DexScreener slot."""
    planned: list[dict] = []
    gt = _gt_endpoints()
    planned.append(gt[0])                    # new_pools page 1 every cycle (freshest launches)
    rot_gt = gt[1:]
    for i in range(max(0, int(SCAN.get("pages", 1)))):
        if rot_gt:
            ep = dict(rot_gt[(_gt_cursor[0] + i) % len(rot_gt)])
            ep["rot"] = True
            planned.append(ep)
    if cycle % 2 and len(planned) > 1:
        # GT is often cooling and grants one call at a time: on odd cycles the rotated slot
        # goes first, so page 1 cannot take every call and starve the rest of the feed.
        planned = planned[1:] + planned[:1]
    rot = _ds_rotation()
    # If the rotated slot is cooling, try the next ones rather than skip DS entirely.
    for i in range(len(rot)):
        slot = rot[(cycle + i) % len(rot)]
        if not is_cooling(slot["id"]):
            planned.append(slot)
            break
    else:
        planned.append(rot[cycle % len(rot)])  # all cooling: still try (will use cache)
    return planned


def new_tokens(session: requests.Session | None = None) -> list[dict]:
    """Solana pools worth looking at, deduplicated by mint.

    Each cycle spends at most 1 + SCAN["pages"] GeckoTerminal calls (new_pools page 1 always,
    plus the next slot of a feed/page rotation: new_pools p2-3, trending 5m and 1h p1-3) and one
    rotated DexScreener discovery endpoint. All GT calls share one token bucket (gt_per_min).
    Responses are cached (90s) and merged into a rolling 10-minute pool, so a
    429 cannot empty the candidate set. 429s cool that endpoint and never sleep.
    """
    global _cycle
    s = session or requests.Session()
    _reset_last()
    now = time.time()
    cycle = _cycle
    _cycle += 1

    planned = _schedule(cycle)
    if _jup_enabled():
        jup = _jup_endpoints()
        planned.append(jup[cycle % len(jup)])       # one cached Jupiter discovery list per cycle
    for ep in planned:
        body, _fresh = _fetch_endpoint(s, ep, now)
        if body is None:
            continue
        if ep["kind"] == "jup_tokens":
            _absorb_jup(body, now)
        elif ep["kind"] == "gt_pools":
            _absorb_gt(body, now, age_cut=bool(ep.get("age_cut")))
            if ep.get("rot"):
                _gt_cursor[0] = (_gt_cursor[0] + 1) % max(1, len(_gt_rotation()))
        elif ep["kind"] == "ds_pairs":
            _absorb_pairs(body, now)
        elif ep["kind"] == "ds_mints":
            mints = _mints_from_profiles(body)
            if mints:
                _hydrate_mints(s, mints, now)

    if _jup_enabled():
        try:
            _hydrate_pumpportal(s, now)                # PumpPortal launches, priced via Jupiter
        except Exception as e:                         # never let the fallback break a cycle
            _note("pumpportal", f"hydrate failed ({type(e).__name__})")

    tokens = _pool_tokens(now)
    for name, n in source_counts(tokens).items():
        LAST[name]["pools"] = n
    return tokens


def established_ok(t: dict) -> bool:
    """Is an over-age pool still alive enough to trade in the established lane?

    Only called for pools past ``SCAN["max_age_hours"]``. The floors are stricter
    than the launch floors on purpose: age is being forgiven, so depth and real
    activity have to earn it back. A dead 5-day-old launch fails all three.
    """
    if not ESTABLISHED.get("enabled", False):
        return False
    age = t.get("age_minutes")
    if age is None or age > ESTABLISHED["max_age_hours"] * 60:
        return False
    return (t.get("liquidity_usd", 0) >= ESTABLISHED["min_liquidity_usd"]
            and t.get("volume_h1", 0) >= ESTABLISHED["min_volume_h1_usd"]
            and (t.get("buys_h1", 0) + t.get("sells_h1", 0)) >= ESTABLISHED["min_trades_h1"])


def age_cut_minutes() -> float:
    """The widest age (in minutes) that may ENTER the candidate pool.

    The launch cut, widened to the established lane's cap when that lane is on.
    Used by the feed absorbers so an old-but-alive pool is not dropped before
    ``free_kill`` ever sees it -- otherwise the lane would be unreachable and the
    desk would look like it was trading it while silently discarding every pool.
    """
    cut = SCAN["max_age_hours"] * 60
    if ESTABLISHED.get("enabled", False):
        cut = max(cut, ESTABLISHED["max_age_hours"] * 60)
    return cut


def free_kill(t: dict) -> str | None:
    """Pass one. Pure arithmetic on data we already hold. Returns the failed check or None."""
    if t["age_minutes"] < SCAN["min_age_minutes"]:
        return "age"
    if t["age_minutes"] > SCAN["max_age_hours"] * 60 and not established_ok(t):
        return "age"
    if t["liquidity_usd"] < SCAN["min_liquidity_usd"]:
        return "liquidity"
    if t["volume_h1"] < SCAN["min_volume_h1_usd"]:
        return "volume"
    if not SCAN["min_mcap_usd"] <= t["mcap_usd"] <= SCAN["max_mcap_usd"]:
        return "mcap"
    if t["buys_h1"] + t["sells_h1"] < SCAN["min_trades_h1"]:
        return "trades"
    if t.get("change_h1", 0) > SCAN["max_h1_change_pct"]:
        return "chase"
    if t["buys_h1"] and t["sells_h1"] / t["buys_h1"] < SCAN["min_sells_ratio_h1"]:
        return "no_sells"
    return None


def _quote_from_pairs(pairs: list[dict]) -> dict | None:
    sol = [p for p in pairs if (p.get("chainId") or "solana").lower() == "solana"] or list(pairs)
    if not sol:
        return None
    p = max(sol, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    vol = p.get("volume") or {}
    return {"price_usd": _f(p.get("priceUsd")),
            "liquidity_usd": _f((p.get("liquidity") or {}).get("usd")),
            "volume_h6": _f(vol.get("h6")), "volume_h24": _f(vol.get("h24"))}


def quote_many(mints: list[str], session: requests.Session | None = None) -> dict[str, dict]:
    """Live quotes for open positions, batched (max 30) on /tokens/v1/solana/{mints}.

    Uses the short quote cache and the pair cache. If the endpoint is cooling or
    the DexScreener budget is empty, returns whatever is still cached so exits
    can keep working without a discovery call.
    """
    s = session or requests.Session()
    now = time.time()
    out: dict[str, dict] = {}
    need: list[str] = []
    seen: set[str] = set()
    for mint in mints:
        if not mint or mint.startswith("DEMO") or mint in seen:
            continue
        seen.add(mint)
        row = _QUOTE_CACHE.get(mint)
        if row and row["exp"] > now:
            out[mint] = row["q"]
            _record_quote_tick(mint, row["q"])
            continue
        pairs = cached_raw_pairs(mint, now)
        if pairs:
            q = _quote_from_pairs(pairs)
            if q:
                out[mint] = q
                _QUOTE_CACHE[mint] = {"exp": now + SCAN_FETCH["quote_ttl_s"], "q": q}
                _record_quote_tick(mint, q)
                continue
        need.append(mint)

    for i in range(0, len(need), 30):
        chunk = need[i:i + 30]
        ep = {
            "id": "ds:tokens-v1",
            "source": "dexscreener",
            "url": f"{DS_TOKENS_V1}/{','.join(chunk)}",
            "params": None,
            "label": f"tokens/v1 quotes n={len(chunk)}",
            "kind": "ds_pairs",
        }
        # The URL embeds the mint list, so this body must NOT go through the shared
        # endpoint cache: _EP_CACHE is keyed by endpoint id alone, and caching a
        # chunk of 30 mints under "ds:tokens-v1" served that same body to every
        # later quote for 90s. The mints in the new request were absent from the
        # cached body, _quote_from_pairs returned nothing, quote() returned None,
        # and the desk rejected the entry as "no_confirm_quote" — 156 times in a
        # row. Per-mint results are cached by _QUOTE_CACHE and _PAIR_CACHE below,
        # which are correctly keyed, so nothing is lost by fetching live here.
        body = _fetch_live(s, ep, now)
        if body is None:
            # DexScreener cooling / 429: price from Jupiter, then any stale quote
            jq = _jup_quotes(s, chunk, now)
            for mint in chunk:
                if mint in jq:
                    out[mint] = jq[mint]
                    _QUOTE_CACHE[mint] = {"exp": now + SCAN_FETCH["quote_ttl_s"], "q": jq[mint]}
                    _record_quote_tick(mint, jq[mint])
                    continue
                stale = _QUOTE_CACHE.get(mint)
                if stale:
                    out[mint] = stale["q"]
            continue
        pairs = _pairs_from_body(body)
        _remember_pairs(pairs, now)
        by_mint: dict[str, list] = {}
        for p in pairs:
            mint = ((p.get("baseToken") or {}).get("address")) or ""
            if mint:
                by_mint.setdefault(mint, []).append(p)
        for mint in chunk:
            q = _quote_from_pairs(by_mint.get(mint) or [])
            if not q:
                stale = _QUOTE_CACHE.get(mint)
                if stale:
                    out[mint] = stale["q"]
                continue
            out[mint] = q
            _QUOTE_CACHE[mint] = {"exp": now + SCAN_FETCH["quote_ttl_s"], "q": q}
            _record_quote_tick(mint, q)
    return out


def _quote_from_jup(row: dict) -> dict | None:
    price = _f(row.get("usdPrice"))
    if price <= 0:
        return None
    return {"price_usd": price, "liquidity_usd": _f(row.get("liquidity")),
            "volume_h6": _jup_stats_vol(row.get("stats6h") or {}),
            "volume_h24": _jup_stats_vol(row.get("stats24h") or {}),
            "volume_m5": _jup_stats_vol(row.get("stats5m") or {})}


def _jup_quotes(s: requests.Session, mints: list[str], now: float) -> dict[str, dict]:
    """Jupiter prices for open positions when DexScreener is cooling. Fresh rows first."""
    out: dict[str, dict] = {}
    need = []
    for m in mints:
        row = _JUP_ROWS.get(m)
        if row and now - row["at"] <= SCAN_FETCH["quote_ttl_s"]:
            q = _quote_from_jup(row["row"])
            if q:
                out[m] = q
                continue
        need.append(m)
    if need and _jup_enabled():
        body = _fetch_live(s, _jup_search_ep(need[:100], "jup:quotes", "jupiter search quotes"), now)
        if isinstance(body, list):
            _remember_jup(body, now)
            for row in body:
                m = row.get("id") if isinstance(row, dict) else None
                if m in need:
                    q = _quote_from_jup(row)
                    if q:
                        out[m] = q
    return out


def socials(mint: str, session: requests.Session | None = None) -> dict:
    """X / Telegram / site links as the project published them (DexScreener token info).
    Taken from the listing, never from a search: a search finds fan accounts."""
    now = time.time()
    pairs = cached_raw_pairs(mint, now)
    if pairs is None:
        quote_many([mint], session)
        pairs = cached_raw_pairs(mint) or []
    info = {}
    for p in pairs:
        if p.get("info"):
            info = p["info"]
            break
    if not info and mint in _JUP_ROWS:          # DexScreener cooling: the listing Jupiter returned
        row = _JUP_ROWS[mint]["row"]
        return {"x": row.get("twitter"), "telegram": row.get("telegram"),
                "website": row.get("website")}
    links = {x.get("type"): x.get("url") for x in info.get("socials", [])}
    sites = info.get("websites") or []
    return {"x": links.get("twitter"), "telegram": links.get("telegram"),
            "website": sites[0].get("url") if sites else None}



def _record_quote_tick(mint: str, q: dict) -> None:
    """Feed the ticket chart without an extra HTTP call."""
    if not q:
        return
    from desk import chart
    chart.record_tick(mint, q.get("price_usd"), q.get("volume_m5") or 0.0)


def quote(mint: str, session: requests.Session | None = None) -> dict | None:
    """Live price and volume for an open position, from the deepest pair."""
    return quote_many([mint], session).get(mint)


# ---------- demo feed: transparent synthetic pools so the desk is alive off-hours ----------
_DEMO_NAMES = ["PEPEAI", "SOLCAT", "MOONRAT", "DEGENX", "BONK2", "WIFHAT", "TURBO", "GIGA",
               "SNIPER", "CHADFI", "LAMBO", "RUGPROOF", "ALPHA", "NOVA", "ZEROX"]
_DEMO_ADJ = ["ai", "sol", "moon", "degen", "bonk", "wif", "turbo", "giga", "chad", "lambo"]


def demo_tokens(n: int = 6, seed: int | None = None) -> list[dict]:
    """Deterministic-ish synthetic pools. Clearly tagged source='demo' so nothing pretends
    to be live market data. Used only when a live scan yields nothing usable."""
    rng = random.Random(seed if seed is not None else int(time.time() // 60))
    out = []
    for i in range(n):
        name = rng.choice(_DEMO_NAMES) + rng.choice(["", "2", "X", "AI", "INU"])
        price = round(rng.uniform(0.000002, 0.004), 9)
        liq = rng.uniform(4_000, 120_000)
        vol1 = rng.uniform(2_000, 300_000)
        buys = rng.randint(30, 900)
        sells = rng.randint(10, int(buys * 0.9) + 1)
        mcap = rng.uniform(12_000, 8_000_000)
        out.append({
            "mint": f"DEMO{i}{rng.randrange(10**6, 10**7)}pump",
            "symbol": name,
            "name": f"{name} / SOL",
            "pool": f"DEMOpool{i}",
            "dex": rng.choice(["pump-fun", "pumpswap", "raydium", "orca"]),
            "price_usd": price,
            "mcap_usd": round(mcap, 2),
            "liquidity_usd": round(liq, 2),
            "volume_m5": round(vol1 / rng.uniform(8, 40), 2),
            "volume_h1": round(vol1, 2),
            "volume_h6": round(vol1 * rng.uniform(2, 6), 2),
            "volume_h24": round(vol1 * rng.uniform(4, 12), 2),
            "buys_h1": buys, "sells_h1": sells,
            "buyers_h1": rng.randint(max(1, buys // 4), buys),
            "change_m5": round(rng.uniform(-25, 40), 2),
            "change_h1": round(rng.uniform(-60, 180), 2),
            "change_h6": round(rng.uniform(-70, 400), 2),
            "age_minutes": (age_m := round(rng.uniform(6, 500), 1)),
            "created_at": time.time() - age_m * 60,
            "source": "demo",
        })
    return out


def demo_quote(mint: str, last: float) -> dict:
    """Random-walk a demo position's price so exits have something to act on."""
    rng = random.Random(f"{mint}:{int(time.time() // 20)}")
    drift = rng.gauss(0.0, 0.06)
    price = max(1e-12, last * (1 + drift))
    return {"price_usd": price, "liquidity_usd": 50_000.0,
            "volume_h6": rng.uniform(500, 20_000), "volume_h24": rng.uniform(20_000, 120_000)}
