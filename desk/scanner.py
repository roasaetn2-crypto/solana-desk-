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


SCAN_BUDGET_SECONDS = 45   # a whole scan must finish inside one cycle
_page_cursor = 0           # rotates the page we read, so the feed keeps moving


def _get(s: requests.Session, path: str, params: dict, tries: int = 2,
         budget: float = SCAN_BUDGET_SECONDS):
    """GET with a HARD time budget.

    GeckoTerminal allows ~10 calls/min and answers 429 under load. The old
    version slept 20s then 40s per call, so six calls could block a cycle for
    six minutes -- the desk looked frozen at "source: idle" while it waited.
    Now: short backoff, and if the budget runs out we return None and let the
    caller move on. A missing page is better than a stalled desk.
    """
    start = time.monotonic()
    for i in range(tries):
        left = budget - (time.monotonic() - start)
        if left <= 2:
            return None
        try:
            r = s.get(f"{GT}{path}", params=params, headers=HEADERS,
                      timeout=min(12, left))
        except requests.RequestException:
            return None
        if r.status_code == 429:
            if i == tries - 1:
                return None
            time.sleep(min(5 * (i + 1), max(0, budget - (time.monotonic() - start) - 1)))
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

    # What to ask GeckoTerminal for. The default /pools feed is "trending", which
    # mixes 3-year-old blue chips with dead $1.5k pools, and /new_pools is almost
    # all under 2 minutes old with no market cap yet. Measured on a live scan:
    # 90 pools in, 3 passed the filters -- the two feeds sat on either side of the
    # window the desk actually trades. Sorting by 24h volume and by 24h tx count
    # puts the 2h-48h, $5k+ liquidity, $8k+/h volume pools on page one.
    feeds = [
        ("/networks/solana/pools", {"sort": "h24_volume_usd_desc"}),
        ("/networks/solana/pools", {"sort": "h24_tx_count_desc"}),
        ("/networks/solana/new_pools", {}),
    ]
    # Walk down the lists instead of re-reading page 1 forever. With a static
    # page the same ~60 pools came back every cycle, so once they were bought or
    # benched the desk had nothing left to look at and sat idle for hours.
    global _page_cursor
    _page_cursor = (_page_cursor + 1) % 3
    for path, extra in feeds:
        for i in range(SCAN["pages"]):
            page = 1 + (_page_cursor + i) % 3
            r = _get(s, path, {"page": page, "include": "base_token,dex", **extra})
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
    if t["buys_h1"] and t["sells_h1"] / t["buys_h1"] < SCAN["min_sells_ratio_h1"]:
        return "no_sells"
    # Young + thin is where rugs live. Measured in paper: both losing trades
    # (WINU, GTA 6) were minutes old with ~$20k liquidity, and both gapped to
    # near-zero inside one 30s cycle -- too fast for a stop to help. A token
    # under `young_minutes` has to prove it has real depth before we touch it.
    if (t["age_minutes"] < SCAN.get("young_minutes", 10)
            and t["liquidity_usd"] < SCAN.get("young_min_liquidity_usd", 30_000)):
        return "young_thin"
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
  
