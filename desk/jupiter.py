"""Jupiter quote API as a price-impact check. QUOTES ONLY.

  GET https://api.jup.ag/swap/v1/quote   (keyless)

This module reads a price quote and nothing else. It never builds, signs or
sends a swap, and it has no wallet or key. The only use is advisory:

  * entry: how much would a ticket of this size move the price (buy side)?
  * exit:  how much would selling this position move the price (sell side)?

priceImpactPct comes back as a ratio (0.05 = 5%). At or above HIGH_IMPACT the
desk adds a flag; on an entry it also halves the paper ticket (only ever
smaller). Rate limit: 0.5 requests a second, enforced here as one call per 2 s;
a call that would exceed it is skipped and returns "unknown".
"""
from __future__ import annotations

import requests

from desk.netutil import TIMEOUT_S, SlidingLimiter, TTLCache, unknown

URL = "https://api.jup.ag/swap/v1/quote"
HEADERS = {"Accept": "application/json", "User-Agent": "solana-trading-desk/1.2"}
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
HIGH_IMPACT = 0.05           # 5%
CACHE_OK_S = 20
CACHE_FAIL_S = 60

_limiter = SlidingLimiter(1, 2.0)          # 0.5 requests / second
_cache = TTLCache(512)


def reset() -> None:
    _limiter.reset()
    _cache.clear()


def _quote(params: dict, key: tuple, session) -> dict:
    hit, val = _cache.get(key)
    if hit:
        return val
    if not _limiter.allow():
        return unknown("jupiter", "rate limit, skipped")
    try:
        r = (session or requests).get(URL, params=params, headers=HEADERS, timeout=TIMEOUT_S)
        if r.status_code == 400 or r.status_code == 404:
            out = {"available": True, "source": "jupiter", "route": False, "impact": None,
                   "flags": ["jup_no_route"], "block": [], "halve": []}
            _cache.set(key, out, CACHE_OK_S)
            return out
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        body = r.json()
        if not isinstance(body, dict) or "outAmount" not in body:
            raise ValueError("no quote")
        impact = float(body.get("priceImpactPct") or 0.0)
        high = impact >= HIGH_IMPACT
        out = {"available": True, "source": "jupiter", "route": True, "impact": impact,
               "impact_pct": round(impact * 100, 2), "high": high,
               "flags": ["jup_impact_high"] if high else [],
               "block": [], "halve": ["jup_impact_high"] if high else []}
        _cache.set(key, out, CACHE_OK_S)
        return out
    except Exception as e:
        out = unknown("jupiter", type(e).__name__)
        _cache.set(key, out, CACHE_FAIL_S)
        return out


def entry_impact(mint: str, ticket_usd: float, session: requests.Session | None = None) -> dict:
    """Buy-side impact of `ticket_usd` of USDC into the token."""
    if not mint or mint.startswith("DEMO") or ticket_usd <= 0:
        return unknown("jupiter", "demo or empty")
    amount = int(round(ticket_usd * 10 ** 6))
    return _quote({"inputMint": USDC, "outputMint": mint, "amount": amount,
                   "slippageBps": 100}, ("buy", mint, amount), session)


def exit_impact(mint: str, qty: float, decimals: int | None,
                session: requests.Session | None = None) -> dict:
    """Sell-side impact of selling `qty` tokens into USDC. Unknown without decimals."""
    if not mint or mint.startswith("DEMO") or qty <= 0 or decimals is None:
        return unknown("jupiter", "demo, empty, or decimals unknown")
    amount = int(qty * 10 ** int(decimals))
    if amount <= 0:
        return unknown("jupiter", "amount rounds to zero")
    return _quote({"inputMint": mint, "outputMint": USDC, "amount": amount,
                   "slippageBps": 100}, ("sell", mint, amount), session)
