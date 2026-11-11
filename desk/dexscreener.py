"""DexScreener enrichment: the second, independent market view.

GeckoTerminal tells us a pool exists. DexScreener tells us how the market
actually treats it: which pair is deepest, how many buys vs sells happened in
the last 5m/1h/6h/24h, whether the project paid for boosts, when the pair was
created, and what socials the listing carries.

Everything here is public, keyless and read-only.
"""
from __future__ import annotations

import time

import requests

BASE = "https://api.dexscreener.com"
HEADERS = {"Accept": "application/json", "User-Agent": "solana-trading-desk/1.0"}


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


def best_pair(mint: str, session: requests.Session | None = None) -> dict | None:
    """The deepest Solana pair for a mint, or None.

    Reuses the scanner's short-lived pair cache (filled by discovery or by
    quote_many) so a cycle does not re-hit DexScreener for the same mint.
    """
    try:
        from desk import scanner as _scanner
        cached = _scanner.cached_raw_pairs(mint)
    except Exception:
        cached = None
    if cached:
        pairs = [p for p in cached if (p.get("chainId") or "solana") == "solana"]
        if pairs:
            return max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))
    from desk import scanner as _scanner
    if _scanner.host_cooling("dexscreener"):
        return None                      # shared 429 cooldown: skip quietly
    s = session or requests.Session()
    try:
        r = s.get(f"{BASE}/tokens/v1/solana/{mint}", headers=HEADERS, timeout=20)
        if r.status_code == 429:
            _scanner.note_429("dexscreener", "tokens/v1 pair lookup", r.headers.get("Retry-After"))
            return None
        payload = r.json()
    except Exception:
        return None
    raw = payload.get("pairs") if isinstance(payload, dict) else payload
    pairs = [p for p in (raw or []) if (p.get("chainId") or "solana") == "solana"]
    if not pairs:
        return None
    return max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))


def facts(mint: str, session: requests.Session | None = None) -> dict:
    """A normalised market snapshot. Missing data is None, never invented."""
    if mint.startswith("DEMO"):
        return _demo_facts(mint)

    p = best_pair(mint, session)
    if not p:
        return {"available": False, "source": "dexscreener"}

    txns = p.get("txns") or {}
    vol = p.get("volume") or {}
    chg = p.get("priceChange") or {}
    liq = p.get("liquidity") or {}
    info = p.get("info") or {}
    socials = {x.get("type"): x.get("url") for x in (info.get("socials") or [])}
    websites = [w.get("url") for w in (info.get("websites") or []) if w.get("url")]
    created_ms = p.get("pairCreatedAt")
    age_min = round((time.time() * 1000 - created_ms) / 60000, 1) if created_ms else None

    def txn(window: str) -> tuple[int, int]:
        row = txns.get(window) or {}
        return _i(row.get("buys")), _i(row.get("sells"))

    b5, s5 = txn("m5")
    b1, s1 = txn("h1")
    b6, s6 = txn("h6")
    b24, s24 = txn("h24")
    boosts = p.get("boosts") if isinstance(p.get("boosts"), dict) else {}

    return {
        "available": True,
        "source": "dexscreener",
        "pair": p.get("pairAddress"),
        "dex": p.get("dexId"),
        "url": p.get("url"),
        "price_usd": _f(p.get("priceUsd")),
        "liquidity_usd": _f(liq.get("usd")),
        "fdv_usd": _f(p.get("fdv")),
        "mcap_usd": _f(p.get("marketCap")),
        "volume_m5": _f(vol.get("m5")), "volume_h1": _f(vol.get("h1")),
        "volume_h6": _f(vol.get("h6")), "volume_h24": _f(vol.get("h24")),
        "buys_m5": b5, "sells_m5": s5,
        "buys_h1": b1, "sells_h1": s1,
        "buys_h6": b6, "sells_h6": s6,
        "buys_h24": b24, "sells_h24": s24,
        "change_m5": _f(chg.get("m5")), "change_h1": _f(chg.get("h1")),
        "change_h6": _f(chg.get("h6")), "change_h24": _f(chg.get("h24")),
        "pair_age_minutes": age_min,
        "boosts_active": _i(boosts.get("active")),
        "socials": socials,
        "websites": websites,
        "has_x": bool(socials.get("twitter")),
        "has_telegram": bool(socials.get("telegram")),
        "has_website": bool(websites),
    }


def _demo_facts(mint: str) -> dict:
    """Stable synthetic DexScreener view for demo mints."""
    import hashlib
    import random

    rng = random.Random(int(hashlib.sha256(mint.encode()).hexdigest(), 16))
    b1 = rng.randint(40, 700)
    s1 = rng.randint(10, int(b1 * 0.85) + 1)
    return {
        "available": True, "source": "dexscreener-demo",
        "pair": f"DEMOpair{mint[-4:]}", "dex": "pumpswap",
        "url": f"https://dexscreener.com/solana/{mint}",
        "price_usd": round(rng.uniform(1e-6, 4e-3), 9),
        "liquidity_usd": round(rng.uniform(5_000, 120_000), 2),
        "fdv_usd": round(rng.uniform(20_000, 8_000_000), 2),
        "mcap_usd": round(rng.uniform(20_000, 8_000_000), 2),
        "volume_m5": round(rng.uniform(200, 8_000), 2),
        "volume_h1": round(rng.uniform(2_000, 250_000), 2),
        "volume_h6": round(rng.uniform(8_000, 900_000), 2),
        "volume_h24": round(rng.uniform(20_000, 2_000_000), 2),
        "buys_m5": rng.randint(2, 90), "sells_m5": rng.randint(1, 60),
        "buys_h1": b1, "sells_h1": s1,
        "buys_h6": rng.randint(100, 3_000), "sells_h6": rng.randint(50, 2_000),
        "buys_h24": rng.randint(200, 9_000), "sells_h24": rng.randint(100, 6_000),
        "change_m5": round(rng.uniform(-25, 45), 2),
        "change_h1": round(rng.uniform(-55, 190), 2),
        "change_h6": round(rng.uniform(-70, 420), 2),
        "change_h24": round(rng.uniform(-85, 900), 2),
        "pair_age_minutes": round(rng.uniform(6, 2_000), 1),
        "boosts_active": rng.choice([0, 0, 0, 1, 5]),
        "socials": {"twitter": f"https://x.com/{mint[:8]}"} if rng.random() < 0.6 else {},
        "websites": [f"https://{mint[:8]}.xyz"] if rng.random() < 0.4 else [],
        "has_x": rng.random() < 0.6,
        "has_telegram": rng.random() < 0.45,
        "has_website": rng.random() < 0.4,
    }
