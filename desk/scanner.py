"""Seat 1: the scanner. Pulls fresh Solana pools and kills everything a comparison can kill.

GeckoTerminal is free and needs no key, but allows about 10 calls a minute,
so one cycle spends `pages` calls here and nothing per token.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone

import requests

from config import SCAN

GT = "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com/latest/dex/tokens"
HEADERS = {"Accept": "application/json", "User-Agent": "solana-trading-desk/1.0"}


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _age_minutes(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        created = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 60)


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


def _get(s: requests.Session, path: str, params: dict, tries: int = 3):
    """GET with real 429 backoff. GeckoTerminal allows ~10 calls/min."""
    for i in range(tries):
        r = s.get(f"{GT}{path}", params=params, headers=HEADERS, timeout=20)
        if r.status_code == 429:
            if i == tries - 1:
                return None
            time.sleep(20 * (i + 1))   # 20s, then 40s
            continue
        r.raise_for_status()
        return r
    return None


def new_tokens(session: requests.Session | None = None) -> list[dict]:
    """Solana pools worth looking at, deduplicated by mint.

    Two sources, because neither alone can pass the desk's filters:

      new_pools       brand-new launches. Young (1-2 min) and the API returns
                      market_cap_usd = None for them, so they fail the age and
                      mcap checks on arrival.
      pools           top pools by volume. Mixed ages, and these DO carry a
                      real market cap. This is where the 5min-12h window lives.
    """
    s = session or requests.Session()
    out, seen = [], set()

    def absorb(body: dict) -> None:
        symbols = {i["id"]: i["attributes"].get("symbol", "?")
                   for i in body.get("included", []) if i.get("type") == "token"}
        for pool in body.get("data", []):
            t = parse_pool(pool, symbols)
            if not t or t["mint"] in seen:
                continue
            if SCAN["pump_only"] and t["dex"] not in SCAN["pump_dexes"]:
                continue
            seen.add(t["mint"])
            out.append(t)

    for path in ("/networks/solana/pools", "/networks/solana/new_pools"):
        for page in range(1, SCAN["pages"] + 1):
            r = _get(s, path, {"page": page, "include": "base_token,dex"})
            if r is None:
                break
            absorb(r.json())

    return out


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


def socials(mint: str, session: requests.Session | None = None) -> dict:
    """X / Telegram / site links as the project published them (DexScreener token info).
    Taken from the listing, never from a search: a search finds fan accounts."""
    s = session or requests.Session()
    try:
        pairs = s.get(f"{DEX}/{mint}", headers=HEADERS, timeout=20).json().get("pairs") or []
    except Exception:
        return {"x": None, "telegram": None, "website": None}
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
    s = session or requests.Session()
    try:
        pairs = s.get(f"{DEX}/{mint}", headers=HEADERS, timeout=20).json().get("pairs") or []
    except Exception:
        return None
    if not pairs:
        return None
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    vol = p.get("volume") or {}
    return {"price_usd": _f(p.get("priceUsd")),
            "liquidity_usd": _f((p.get("liquidity") or {}).get("usd")),
            "volume_h6": _f(vol.get("h6")), "volume_h24": _f(vol.get("h24"))}


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
