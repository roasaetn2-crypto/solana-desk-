"""GMGN OpenAPI, READ ONLY. Dormant unless GMGN_API_KEY is set.

Endpoints used (all GET, API-key auth, from the GMGNAI/gmgn-skills docs):
  /v1/token/security   rug_ratio, wash trading, bundler / insider / sniper rates
  /v1/token/info       smart-money, KOL, sniper, bundler wallet counts, fresh-wallet rate

There is no swap, order, trade, strategy, cooking or signed-request code in this
file, and no GMGN private key is read. Anything GMGN returns is treated as data:
token names and descriptions are never read, only numbers.

Quota: the free plan is a leaky bucket of 5 requests per 5 seconds. We make two
calls per token, cache a result 10 minutes, and cap at 20 calls a minute.
GMGN may restrict API access by IP; a 403 or 429 puts the module to sleep for
5 minutes and every lookup returns "unknown". The key is never logged or shown.

Flags only add caution (HALVE or BLOCK in PAPER). They are advisory when the key
is missing, because nothing is queried at all.
"""
from __future__ import annotations

import os
import time
import uuid

import requests

from desk.netutil import TIMEOUT_S, SlidingLimiter, TTLCache, unknown

HOST = "https://openapi.gmgn.ai"
CHAIN = "sol"
HEADERS = {"Accept": "application/json", "User-Agent": "solana-trading-desk/1.2"}
MAX_PER_MIN = 20
CACHE_OK_S = 600
CACHE_FAIL_S = 120
SLEEP_AFTER_DENY_S = 300

# thresholds (ratios 0-1 unless noted). Tune here only.
T = {
    "rug_ratio_block": 0.5,
    "rug_ratio_halve": 0.3,
    "bundler_halve": 0.25,
    "insider_halve": 0.20,        # rat_trader volume rate / suspected insider hold
    "fresh_wallet_halve": 0.45,
    "sniper_count_halve": 25,
    "bot_degen_halve": 0.35,
}

_limiter = SlidingLimiter(MAX_PER_MIN, 60)
_cache = TTLCache(512)
_sleep_until = 0.0
_last_ok: float | None = None


def configured() -> bool:
    return bool(os.environ.get("GMGN_API_KEY", "").strip())


def reset() -> None:
    global _sleep_until, _last_ok
    _limiter.reset()
    _cache.clear()
    _sleep_until = 0.0
    _last_ok = None


def health() -> dict:
    """Safe to show: never the key."""
    return {"configured": configured(), "sleeping": time.time() < _sleep_until,
            "last_ok": _last_ok}


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse(security: dict | None, info: dict | None) -> dict:
    """Merge the two GMGN payloads into rates and flags. Pure."""
    sec = security if isinstance(security, dict) else {}
    inf = info if isinstance(info, dict) else {}
    stat = inf.get("stat") if isinstance(inf.get("stat"), dict) else {}
    tags = inf.get("wallet_tags_stat") if isinstance(inf.get("wallet_tags_stat"), dict) else {}

    rates = {
        "rug_ratio": _num(sec.get("rug_ratio")),
        "wash_trading": bool(sec.get("is_wash_trading")) if sec.get("is_wash_trading") is not None else None,
        "bundler_rate": _num(sec.get("bundler_trader_amount_rate"),
                             _num(stat.get("top_bundler_trader_percentage"))),
        "insider_rate": _num(sec.get("suspected_insider_hold_rate"),
                             _num(sec.get("rat_trader_amount_rate"))),
        "fresh_wallet_rate": _num(stat.get("fresh_wallet_rate")),
        "bot_degen_rate": _num(stat.get("bot_degen_rate")),
        "sniper_count": _num(sec.get("sniper_count"), _num(tags.get("sniper_wallets"))),
        "smart_wallets": _num(tags.get("smart_wallets")),
        "kol_wallets": _num(tags.get("renowned_wallets")),
        "honeypot": (str(sec.get("is_honeypot", "")).lower() == "yes") or None,
    }
    block, halve = [], []
    rr = rates["rug_ratio"]
    if rr is not None:
        if rr >= T["rug_ratio_block"]:
            block.append("gmgn_rug_ratio")
        elif rr >= T["rug_ratio_halve"]:
            halve.append("gmgn_rug_ratio")
    if rates["honeypot"]:
        block.append("gmgn_honeypot")
    if rates["wash_trading"]:
        halve.append("gmgn_wash")
    if (rates["bundler_rate"] or 0) >= T["bundler_halve"]:
        halve.append("gmgn_bundler")
    if (rates["insider_rate"] or 0) >= T["insider_halve"]:
        halve.append("gmgn_insider")
    if (rates["fresh_wallet_rate"] or 0) >= T["fresh_wallet_halve"]:
        halve.append("gmgn_fresh")
    if (rates["sniper_count"] or 0) >= T["sniper_count_halve"]:
        halve.append("gmgn_sniper")
    if (rates["bot_degen_rate"] or 0) >= T["bot_degen_halve"]:
        halve.append("gmgn_bots")
    return {"available": True, "source": "gmgn", "rates": rates,
            "flags": block + halve, "block": block, "halve": halve}


def _get(path: str, mint: str, session) -> dict:
    """One read-only GET. The key goes in a header, never in the URL or a log line."""
    key = os.environ.get("GMGN_API_KEY", "").strip()
    r = (session or requests).get(
        HOST + path,
        params={"chain": CHAIN, "address": mint,
                "timestamp": int(time.time()), "client_id": str(uuid.uuid4())},
        headers={**HEADERS, "X-APIKEY": key}, timeout=TIMEOUT_S)
    if r.status_code in (401, 403, 429):
        raise PermissionError(f"http {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"http {r.status_code}")
    body = r.json()
    if isinstance(body, dict) and body.get("code") not in (0, None):
        raise RuntimeError("gmgn error code")
    return (body.get("data") if isinstance(body, dict) else None) or {}


def check(mint: str, session: requests.Session | None = None) -> dict:
    """Rates and flags for one mint. "unknown" when dormant, asleep or failing."""
    global _sleep_until, _last_ok
    if not configured():
        return unknown("gmgn", "dormant: GMGN_API_KEY not set")
    if not mint or mint.startswith("DEMO"):
        return unknown("gmgn", "demo or empty mint")
    if time.time() < _sleep_until:
        return unknown("gmgn", "sleeping after denial")
    hit, val = _cache.get(mint)
    if hit:
        return val
    if not _limiter.allow() or not _limiter.allow():      # two calls per token
        return unknown("gmgn", "rate limit, skipped")
    try:
        sec = _get("/v1/token/security", mint, session)
        inf = _get("/v1/token/info", mint, session)
        out = parse(sec, inf)
        _last_ok = time.time()
        _cache.set(mint, out, CACHE_OK_S)
        return out
    except PermissionError:
        _sleep_until = time.time() + SLEEP_AFTER_DENY_S
        return unknown("gmgn", "denied or rate limited, sleeping 5 min")
    except Exception as e:
        out = unknown("gmgn", type(e).__name__)
        _cache.set(mint, out, CACHE_FAIL_S)
        return out
