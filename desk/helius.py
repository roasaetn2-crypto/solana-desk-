"""Helius: an optional upgrade, never a requirement.

The desk runs on the public Solana RPC and three free HTTP APIs. That is enough
to be honest, and it is what happens with no key at all. Helius buys you four
specific things the public endpoint cannot give you:

    rpc          a real node with a real rate limit, instead of being throttled
    DAS          token metadata + a true holder count (getAsset, getTokenAccounts)
    priority fee a live estimate, so a fill is not dropped when the chain is busy
    webhooks     push notifications for new pools, instead of polling

Everything here degrades to None. A Helius outage must never look like a rug.

Set one variable and the whole desk uses it:

    export HELIUS_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

Free tier is 1M credits/month, which at a 45-second cycle is not a constraint.
"""
from __future__ import annotations

import os
import threading
import time
from urllib.parse import parse_qs, urlparse

import requests

BASE = "https://mainnet.helius-rpc.com"
TIMEOUT = int(os.getenv("HELIUS_TIMEOUT", "15"))
# Stay a little under the common free-tier cap of 10 req/s.
_RPS = float(os.getenv("HELIUS_RPS", "8"))
_THROTTLE_LOCK = threading.Lock()
_last_call = 0.0

# DAS calls cost 10 credits each and the free tier is 1M/month at 2 req/sec.
# Without a cache the desk burns the whole free tier in under two days and
# spends 14 of every 45 seconds waiting on DAS rate limits. So: cache.
#
#   metadata      immutable in practice  -> 6 hours
#   holder_count  moves slowly           -> 5 minutes
#   priority_fee  moves constantly       -> 30 seconds
_TTL = {"metadata": 6 * 3600, "holders": 300, "fee": 30}
_CACHE: dict[tuple, tuple[float, object]] = {}
_LOCK = threading.Lock()


def _cached(kind: str, k: str, fn):
    now = time.time()
    slot = (kind, k)
    with _LOCK:
        hit = _CACHE.get(slot)
        if hit and hit[0] > now:
            return hit[1]
    val = fn()
    if val is not None:
        with _LOCK:
            _CACHE[slot] = (now + _TTL[kind], val)
    return val


def cache_clear():
    with _LOCK:
        _CACHE.clear()


def cache_stats() -> dict:
    with _LOCK:
        return {"entries": len(_CACHE), "ttl": dict(_TTL)}


def _raw_key() -> str:
    return os.getenv("HELIUS_API_KEY", "").strip().strip('"').strip("'")


def key() -> str:
    """The bare API key, even if someone pasted a full Helius URL."""
    raw = _raw_key()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    lower = raw.lower()
    if "api-key=" in lower:
        parsed = urlparse(raw if "://" in raw else "https://unused/?" + raw)
        for name, vals in parse_qs(parsed.query).items():
            if name.lower() == "api-key" and vals and vals[0].strip():
                return vals[0].strip()
        idx = lower.find("api-key=")
        return raw[idx + len("api-key="):].split("&", 1)[0].strip()
    return raw


def configured() -> bool:
    return bool(_raw_key())


def rpc_url() -> str | None:
    """The Helius endpoint, or None when no key is set.

    Accepts a bare key or a pasted full URL so a dashboard copy-paste still works.
    """
    raw = _raw_key()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    k = key()
    return f"{BASE}/?api-key={k}" if k else None


def is_helius_url(url: str | None) -> bool:
    u = (url or "").lower()
    return "helius-rpc.com" in u or "helius.xyz" in u or "helius.com" in u


def reset_throttle() -> None:
    global _last_call
    with _THROTTLE_LOCK:
        _last_call = 0.0


def throttle() -> None:
    """Block briefly so Helius calls stay under the free-tier rate."""
    global _last_call
    interval = 1.0 / max(0.5, _RPS)
    with _THROTTLE_LOCK:
        now = time.time()
        wait = interval - (now - _last_call)
        if wait > 0:
            time.sleep(min(wait, 2.0))
            now = time.time()
        _last_call = now


def _post(method: str, params, session: requests.Session | None = None):
    url = rpc_url()
    if not url:
        return None
    s = session or requests.Session()
    try:
        last = None
        for attempt in range(3):
            throttle()
            r = s.post(url,
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                       timeout=TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                last = r
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                return None
            body = r.json()
            if "error" in body:
                return None
            return body.get("result")
        return None
    except Exception:
        return None


# ---------- DAS: metadata ----------
def metadata(mint: str, session: requests.Session | None = None) -> dict | None:
    """Name, symbol, image and socials straight from the token's own metadata.

    GeckoTerminal gives a symbol but often no name and no socials, which is
    exactly what the narrative classifier needs. This fills that gap.
    """
    return _cached("metadata", mint, lambda: _metadata_uncached(mint, session))


def _metadata_uncached(mint: str, session=None) -> dict | None:
    res = _post("getAsset", {"id": mint}, session)
    if not res:
        return None
    content = res.get("content") or {}
    md = content.get("metadata") or {}
    links = content.get("links") or {}
    out = {
        "name": md.get("name"),
        "symbol": md.get("symbol"),
        "image": links.get("image"),
        "website": links.get("external_url"),
        "has_x": bool(links.get("twitter") or links.get("x")),
        "has_telegram": bool(links.get("telegram")),
        "source": "helius",
    }
    return out if (out["name"] or out["symbol"]) else None


# ---------- DAS: real holder count ----------
def holder_count(mint: str, session: requests.Session | None = None) -> int | None:
    """How many distinct token accounts hold a balance.

    The public RPC can only tell you the top 20 accounts. This is the number the
    community score actually wants, because "150 holders" is a claim about the
    whole distribution, not about the top twenty.
    """
    return _cached("holders", mint, lambda: _holders_uncached(mint, session))


def _holders_uncached(mint: str, session=None) -> int | None:
    res = _post("getTokenAccounts", {"mint": mint, "limit": 1000,
                                     "options": {"showZeroBalance": False}}, session)
    if not res:
        return None
    total = res.get("total")
    if isinstance(total, int):
        return total
    accts = res.get("token_accounts") or []
    return len(accts) if accts else None


# ---------- priority fee ----------
def priority_fee(account_keys: list[str] | None = None,
                 session: requests.Session | None = None) -> int | None:
    """A live priority-fee estimate in micro-lamports.

    Paper trading does not need this. A live fill does: on a busy block a
    zero-fee transaction simply never lands, and a stop that never lands is
    worse than no stop.
    """
    k = ",".join((account_keys or [])[:3]) or "global"
    return _cached("fee", k, lambda: _fee_uncached(account_keys, session))


def _fee_uncached(account_keys=None, session=None) -> int | None:
    params = {"options": {"includeAllPriorityFeeLevels": True}}
    if account_keys:
        params["accountKeys"] = account_keys[:100]
    res = _post("getPriorityFeeEstimate", params, session)
    if not res:
        return None
    lv = res.get("priorityFeeLevels") or {}
    # medium is the honest default: high overpays, low gets dropped
    return int(lv.get("medium") or res.get("priorityFeeEstimate") or 0) or None


# ---------- what the dashboard shows ----------
def health() -> dict:
    return {
        "configured": configured(),
        "rpc": "helius" if configured() else "public",
        "das": configured(),
        "priority_fee": configured(),
        "cache": cache_stats(),
        "note": ("Helius key present: real node, true holder counts, live fees"
                 if configured() else
                 "no key: public RPC + free APIs (works, but rate-limited)"),
    }
