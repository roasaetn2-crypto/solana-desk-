"""Seat 1: the scanner. Pulls fresh Solana pools and kills everything a comparison can kill.

GeckoTerminal is free and needs no key, but the public cap is about 30 calls
a minute (we stay at <= 10, and only 1-2 calls per cycle). DexScreener is the
second source. Endpoints are rotated, cached, and cooled on 429 so a cycle
never sleeps on a rate limit. A rolling mint-keyed pool keeps candidates
alive when one source is cooling.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

import requests

from config import SCAN, SCAN_FETCH

log = logging.getLogger("desk.scanner")

GT = "https://api.geckoterminal.com/api/v2"
DS = "https://api.dexscreener.com"
DEX = "https://api.dexscreener.com/latest/dex/tokens"
DS_TOKENS_V1 = "https://api.dexscreener.com/tokens/v1/solana"
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
LAST: dict = {
    "geckoterminal": {"pools": 0, "calls": 0, "errors": []},
    "dexscreener": {"pools": 0, "calls": 0, "errors": []},
}

# Cumulative HTTP counters (not reset each cycle) for simulations and tests.
CALLS: dict = {"geckoterminal": 0, "dexscreener": 0, "429": 0}

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


def _new_buckets() -> tuple[TokenBucket, TokenBucket]:
    return (
        TokenBucket(SCAN_FETCH["gt_per_min"], SCAN_FETCH["gt_burst"]),
        TokenBucket(SCAN_FETCH["ds_per_min"], SCAN_FETCH["ds_burst"]),
    )


_gt_bucket, _ds_bucket = _new_buckets()
_cycle = 0
# endpoint id -> {"until": ts, "strikes": int}
_COOL: dict[str, dict] = {}
# endpoint id -> {"exp": ts, "body": ...}
_EP_CACHE: dict[str, dict] = {}
# mint -> {"token": dict, "at": ts}  (rolling candidate pool, ~10 min)
_POOL: dict[str, dict] = {}
# mint -> {"exp": ts, "pairs": list}
_PAIR_CACHE: dict[str, dict] = {}
# mint -> {"exp": ts, "q": dict}
_QUOTE_CACHE: dict[str, dict] = {}


def _host_of(source: str) -> TokenBucket:
    return _gt_bucket if source == "geckoterminal" else _ds_bucket


def reset_runtime() -> None:
    """Clear limiter / cache / cooldown / cycle state. Used by tests."""
    global _cycle, _gt_bucket, _ds_bucket
    _cycle = 0
    _gt_bucket, _ds_bucket = _new_buckets()
    _COOL.clear()
    _EP_CACHE.clear()
    _POOL.clear()
    _PAIR_CACHE.clear()
    _QUOTE_CACHE.clear()
    CALLS["geckoterminal"] = 0
    CALLS["dexscreener"] = 0
    CALLS["429"] = 0
    _reset_last()


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


def _age_minutes(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        created = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 60)


def _reset_last() -> None:
    LAST["geckoterminal"] = {"pools": 0, "calls": 0, "errors": []}
    LAST["dexscreener"] = {"pools": 0, "calls": 0, "errors": []}


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
        "geckoterminal": dict(LAST["geckoterminal"]),
        "dexscreener": dict(LAST["dexscreener"]),
        "warnings": warnings,
        "calls_total": dict(CALLS),
        "cycle": _cycle,
    }


def source_label(tokens: list[dict]) -> str:
    gt = sum(1 for t in tokens if t.get("source") == "geckoterminal")
    ds = sum(1 for t in tokens if t.get("source") == "dexscreener")
    parts = []
    if gt:
        parts.append("geckoterminal")
    if ds:
        parts.append("dexscreener")
    if parts:
        return "+".join(parts)
    return "geckoterminal+dexscreener-empty"


def source_counts(tokens: list[dict]) -> dict[str, int]:
    return {
        "geckoterminal": sum(1 for t in tokens if t.get("source") == "geckoterminal"),
        "dexscreener": sum(1 for t in tokens if t.get("source") == "dexscreener"),
    }


def cooling_until(endpoint_id: str, now: float | None = None) -> float:
    now = time.time() if now is None else now
    until = float((_COOL.get(endpoint_id) or {}).get("until") or 0)
    return until if until > now else 0.0


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
    age = round((time.time() * 1000 - created_ms) / 60000, 1) if created_ms else 0.0
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
        "source": "dexscreener",
    }


# ---------- HTTP: one shot, no in-cycle sleep ----------

def _retry_after(r: requests.Response, default: float) -> float:
    raw = r.headers.get("Retry-After") or r.headers.get("retry-after")
    if not raw:
        return default
    try:
        return max(1.0, float(raw))
    except ValueError:
        return default


def _cool(endpoint_id: str, source: str, wait: float, label: str, status: int, reason: str) -> None:
    """Start or extend a cooldown. Log a 429 (or 5xx) once per cooldown window."""
    cap = SCAN_FETCH["cooldown_cap_s"]
    floor = SCAN_FETCH["cooldown_floor_s"] if status == 429 else min(10.0, wait)
    rec = _COOL.get(endpoint_id) or {"until": 0.0, "strikes": 0}
    rec["strikes"] = int(rec.get("strikes") or 0) + 1
    exp = min(cap, max(wait, floor) * (2 ** max(0, rec["strikes"] - 1)))
    rec["until"] = time.time() + min(cap, exp)
    _COOL[endpoint_id] = rec
    if status == 429:
        CALLS["429"] += 1
    _note(source, f"{label} -> {status} {reason}, cooling {min(cap, exp):.0f}s")


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
        if r.status_code in (401, 403, 408):
            _cool(endpoint_id, source, 30.0, label, r.status_code, r.reason or "error")
        else:
            _note(source, f"{label} -> {r.status_code} {r.reason or 'error'}")
        return None
    if endpoint_id in _COOL:
        _COOL[endpoint_id]["strikes"] = 0
        _COOL[endpoint_id]["until"] = 0.0
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


# ---------- rolling pool ----------

def _stamp(t: dict, now: float) -> dict:
    row = dict(t)
    row["_age_at"] = now
    return row


def _view(t: dict, now: float) -> dict:
    origin = t.get("_age_at", now)
    view = {k: v for k, v in t.items() if not k.startswith("_")}
    view["age_minutes"] = round(float(t.get("age_minutes") or 0) + (now - origin) / 60.0, 1)
    return view


def _keep(t: dict | None) -> dict | None:
    if not t:
        return None
    if SCAN["pump_only"] and t["dex"] not in SCAN["pump_dexes"]:
        return None
    return t


def _put_pool(t: dict, now: float) -> None:
    """Insert into the rolling pool. A live GeckoTerminal row wins over DexScreener
    for the same mint while it is still inside the pool TTL (same-cycle merge)."""
    mint = t["mint"]
    prev = _POOL.get(mint)
    if (prev and prev["token"].get("source") == "geckoterminal"
            and t.get("source") == "dexscreener"
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

def _gt_endpoints() -> list[dict]:
    return [
        {"id": "gt:new_pools:1", "source": "geckoterminal",
         "url": f"{GT}/networks/solana/new_pools",
         "params": {"page": 1, "include": "base_token,dex"},
         "label": "/networks/solana/new_pools page=1", "kind": "gt_pools"},
        {"id": "gt:new_pools:2", "source": "geckoterminal",
         "url": f"{GT}/networks/solana/new_pools",
         "params": {"page": 2, "include": "base_token,dex"},
         "label": "/networks/solana/new_pools page=2", "kind": "gt_pools"},
    ]


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


def _absorb_gt(body: dict, now: float) -> int:
    if not isinstance(body, dict):
        return 0
    symbols = {i["id"]: i["attributes"].get("symbol", "?")
               for i in body.get("included", []) if i.get("type") == "token"}
    n = 0
    for pool in body.get("data") or []:
        t = _keep(parse_pool(pool, symbols))
        if not t:
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
    """1-2 GeckoTerminal calls and exactly one DexScreener discovery slot per cycle."""
    planned: list[dict] = []
    gt = _gt_endpoints()
    planned.append(gt[0])                    # new_pools page 1 every cycle
    if cycle % 2 == 1:
        planned.append(gt[1])                # page 2 every other cycle
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

    Each cycle spends at most 1-2 GeckoTerminal calls (new_pools page 1 always,
    page 2 every other cycle) and one rotated DexScreener discovery endpoint.
    Responses are cached (90s) and merged into a rolling 10-minute pool, so a
    429 cannot empty the candidate set. 429s cool that endpoint and never sleep.
    """
    global _cycle
    s = session or requests.Session()
    _reset_last()
    now = time.time()
    cycle = _cycle
    _cycle += 1

    for ep in _schedule(cycle):
        body, _fresh = _fetch_endpoint(s, ep, now)
        if body is None:
            continue
        if ep["kind"] == "gt_pools":
            _absorb_gt(body, now)
        elif ep["kind"] == "ds_pairs":
            _absorb_pairs(body, now)
        elif ep["kind"] == "ds_mints":
            mints = _mints_from_profiles(body)
            if mints:
                _hydrate_mints(s, mints, now)

    tokens = _pool_tokens(now)
    LAST["geckoterminal"]["pools"] = sum(1 for t in tokens if t["source"] == "geckoterminal")
    LAST["dexscreener"]["pools"] = sum(1 for t in tokens if t["source"] == "dexscreener")
    return tokens


def free_kill(t: dict) -> str | None:
    """Pass one. Pure arithmetic on data we already hold. Returns the failed check or None."""
    if not SCAN["min_age_minutes"] <= t["age_minutes"] <= SCAN["max_age_hours"] * 60:
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
            continue
        pairs = cached_raw_pairs(mint, now)
        if pairs:
            q = _quote_from_pairs(pairs)
            if q:
                out[mint] = q
                _QUOTE_CACHE[mint] = {"exp": now + SCAN_FETCH["quote_ttl_s"], "q": q}
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
        body, fresh = _fetch_endpoint(s, ep, now)
        if body is None:
            # fall back to any stale quote / pair cache
            for mint in chunk:
                stale = _QUOTE_CACHE.get(mint)
                if stale:
                    out[mint] = stale["q"]
            continue
        pairs = _pairs_from_body(body)
        _remember_pairs(pairs, now)
        if fresh:
            _store_cache("ds:tokens-v1", body, now)
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
    links = {x.get("type"): x.get("url") for x in info.get("socials", [])}
    sites = info.get("websites") or []
    return {"x": links.get("twitter"), "telegram": links.get("telegram"),
            "website": sites[0].get("url") if sites else None}


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
            "age_minutes": round(rng.uniform(6, 500), 1),
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
