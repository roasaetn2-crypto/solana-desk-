"""GoPlus Solana token security (free, no key). Token-2022 and authority traps.

  GET https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses=<mint>

Flags (all checked in PAPER, they only ever add caution):
  BLOCK  non_transferable, default_frozen, transfer_hook, balance_mutable,
         closable, malicious_creator, transfer_fee above 5%
  HALVE  transfer_fee between 0% and 5%
Quota: GoPlus allows about 30 calls a minute without a key. We stay at 24.
Results are cached an hour; a failed or rate-limited lookup returns "unknown"
(no flags) for two minutes instead of retrying, so the loop never waits on it.
"""
from __future__ import annotations

import requests

from desk.netutil import TIMEOUT_S, SlidingLimiter, TTLCache, unknown

URL = "https://api.gopluslabs.io/api/v1/solana/token_security"
HEADERS = {"Accept": "application/json", "User-Agent": "solana-trading-desk/1.2"}
MAX_PER_MIN = 24
CACHE_OK_S = 3600
CACHE_FAIL_S = 120
BIG_FEE_BPS = 500            # 5%

_limiter = SlidingLimiter(MAX_PER_MIN, 60)
_cache = TTLCache(1024)


def reset() -> None:
    _limiter.reset()
    _cache.clear()


def _on(x) -> bool:
    """GoPlus encodes booleans as "1"/"0" strings, sometimes ints."""
    return str(x).strip() == "1"


def _status(rec: dict, key: str) -> bool:
    v = rec.get(key)
    return _on(v.get("status")) if isinstance(v, dict) else _on(v)


def _bps(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse(rec: dict) -> dict:
    """Turn one GoPlus token record into flags. Pure."""
    block: list[str] = []
    halve: list[str] = []
    info: dict = {}

    if _on(rec.get("non_transferable")):
        block.append("non_transferable")
    if str(rec.get("default_account_state", "")).strip() == "2":
        block.append("default_frozen")
    hooks = rec.get("transfer_hook") or []
    if hooks:
        block.append("transfer_hook")
        if any(_on(h.get("malicious_address")) for h in hooks if isinstance(h, dict)):
            info["hook_malicious"] = True
    if _status(rec, "balance_mutable_authority"):
        block.append("balance_mutable")
    if _status(rec, "closable"):
        block.append("closable")
    creators = rec.get("creators") or []
    if any(_on(c.get("malicious_address")) for c in creators if isinstance(c, dict)):
        block.append("malicious_creator")

    fee = rec.get("transfer_fee") or {}
    cur = _bps((fee.get("current_fee_rate") or {}).get("fee_rate")) if isinstance(fee, dict) else 0.0
    sched = [_bps(s.get("fee_rate")) for s in (fee.get("scheduled_fee_rate") or [])
             if isinstance(s, dict)] if isinstance(fee, dict) else []
    worst = max([cur] + sched)
    if worst > 0:
        info["transfer_fee_bps"] = worst
        (block if worst > BIG_FEE_BPS else halve).append("transfer_fee")

    flags = block + halve
    return {"available": True, "source": "goplus", "flags": flags, "block": block,
            "halve": halve, "info": info, "trusted": _on(rec.get("trusted_token"))}


def check(mint: str, session: requests.Session | None = None) -> dict:
    """Security flags for one mint. Always returns a dict, never raises."""
    if not mint or mint.startswith("DEMO"):
        return unknown("goplus", "demo or empty mint")
    hit, val = _cache.get(mint)
    if hit:
        return val
    if not _limiter.allow():
        return unknown("goplus", "rate limit, skipped")
    try:
        r = (session or requests).get(URL, params={"contract_addresses": mint},
                                      headers=HEADERS, timeout=TIMEOUT_S)
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        body = r.json()
        rec = ((body or {}).get("result") or {}).get(mint)
        if not isinstance(rec, dict) or not rec:
            out = unknown("goplus", "no record yet")
            _cache.set(mint, out, CACHE_FAIL_S)
            return out
        out = parse(rec)
        _cache.set(mint, out, CACHE_OK_S)
        return out
    except Exception as e:
        out = unknown("goplus", type(e).__name__)
        _cache.set(mint, out, CACHE_FAIL_S)
        return out
