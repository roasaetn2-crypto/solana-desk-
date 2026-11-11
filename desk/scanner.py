"""Seat 1: the scanner. Pulls fresh Solana pools and kills everything a comparison can kill.

GeckoTerminal is free and needs no key, but allows about 10 calls a minute,
so one cycle spends `pages` calls here and nothing per token.

DexScreener is the fallback and a second source: token profiles, boosts, and
a couple of searches, hydrated into the same pool shape and deduped by mint.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

import requests

from config import SCAN

log = logging.getLogger("desk.scanner")

GT = "https://api.geckoterminal.com/api/v2"
DS = "https://api.dexscreener.com"
DEX = "https://api.dexscreener.com/latest/dex/tokens"
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

_gt_skip_until = 0.0
_GT_COOLDOWN_S = 60
_MAX_BACKOFF_S = 15  # cap so a 429 cannot stall a cycle for minutes


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
    """Record a visible, secret-free failure and log it."""
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


def _retry_after(r: requests.Response, default: float) -> float:
    raw = r.headers.get("Retry-After") or r.headers.get("retry-after")
    if not raw:
        return default
    try:
        return max(1.0, min(float(raw), _MAX_BACKOFF_S))
    except ValueError:
        return default


def _get(s: requests.Session, url: str, params: dict | None = None,
         tries: int = 3, source: str = "geckoterminal", label: str = ""):
    """GET with logged status codes and capped 429/5xx backoff. Never raises on HTTP errors."""
    tag = label or url
    last_exc = None
    for i in range(tries):
        LAST[source]["calls"] += 1
        try:
            r = s.get(url, params=params, headers=HEADERS, timeout=20)
        except requests.RequestException as e:
            last_exc = e
            _note(source, f"{tag} request failed ({type(e).__name__})")
            if i == tries - 1:
                return None
            time.sleep(min(2 * (i + 1), _MAX_BACKOFF_S))
            continue
        if r.status_code == 429:
            wait = _retry_after(r, default=min(8 * (i + 1), _MAX_BACKOFF_S))
            _note(source, f"{tag} -> 429 {r.reason or 'Too Many Requests'}, backoff {wait:.0f}s")
            if i == tries - 1:
                return None
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            _note(source, f"{tag} -> {r.status_code} {r.reason or 'server error'}")
            if i == tries - 1:
                return None
            time.sleep(min(3 * (i + 1), _MAX_BACKOFF_S))
            continue
        if r.status_code != 200:
            _note(source, f"{tag} -> {r.status_code} {r.reason or 'error'}")
            return None
        return r
    if last_exc is not None:
        return None
    return None


def _keep(t: dict | None, seen: set[str]) -> dict | None:
    if not t or t["mint"] in seen:
        return None
    if SCAN["pump_only"] and t["dex"] not in SCAN["pump_dexes"]:
        return None
    return t


def _from_geckoterminal(s: requests.Session) -> list[dict]:
    """Existing two GeckoTerminal paths. Stops a path on a hard failure; 429 cools the source down."""
    global _gt_skip_until
    out, seen = [], set()
    now = time.time()
    if now < _gt_skip_until:
        wait = _gt_skip_until - now
        _note("geckoterminal", f"cooling down {wait:.0f}s after 429")
        return out

    hit_429 = False

    def absorb(body: dict) -> int:
        symbols = {i["id"]: i["attributes"].get("symbol", "?")
                   for i in body.get("included", []) if i.get("type") == "token"}
        added = 0
        for pool in body.get("data", []):
            t = _keep(parse_pool(pool, symbols), seen)
            if not t:
                continue
            seen.add(t["mint"])
            out.append(t)
            added += 1
        return added

    for path in ("/networks/solana/pools", "/networks/solana/new_pools"):
        for page in range(1, SCAN["pages"] + 1):
            r = _get(s, f"{GT}{path}", {"page": page, "include": "base_token,dex"},
                     source="geckoterminal", label=f"{path} page={page}")
            if r is None:
                if any("429" in e for e in LAST["geckoterminal"]["errors"]):
                    hit_429 = True
                break
            try:
                body = r.json()
            except ValueError:
                _note("geckoterminal", f"{path} page={page} -> 200 but body was not JSON")
                break
            absorb(body)

    if hit_429:
        _gt_skip_until = time.time() + _GT_COOLDOWN_S
    LAST["geckoterminal"]["pools"] = len(out)
    return out


def _ds_json(s: requests.Session, path: str, params: dict | None = None, label: str = ""):
    r = _get(s, f"{DS}{path}", params, source="dexscreener", label=label or path)
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        _note("dexscreener", f"{label or path} -> 200 but body was not JSON")
        return None


def _best_solana_pairs_by_mint(pairs: list[dict]) -> list[dict]:
    """Deepest Solana pair per mint, already parsed and pump-filtered."""
    best: dict[str, dict] = {}
    for raw in pairs or []:
        t = parse_dex_pair(raw)
        if not t:
            continue
        if SCAN["pump_only"] and t["dex"] not in SCAN["pump_dexes"]:
            continue
        prev = best.get(t["mint"])
        if prev is None or t["liquidity_usd"] > prev["liquidity_usd"]:
            best[t["mint"]] = t
    return list(best.values())


def _from_dexscreener(s: requests.Session) -> list[dict]:
    """Profiles + boosts + a couple of searches, normalized and deduped by mint."""
    by_mint: dict[str, dict] = {}

    def absorb(tokens: list[dict]) -> None:
        for t in tokens:
            prev = by_mint.get(t["mint"])
            if prev is None or t["liquidity_usd"] > prev["liquidity_usd"]:
                by_mint[t["mint"]] = t

    for path, label in (
        ("/latest/dex/search", "search q=SOL"),
        ("/latest/dex/search", "search q=pump"),
    ):
        q = "SOL" if "SOL" in label else "pump"
        body = _ds_json(s, path, {"q": q}, label=label)
        if isinstance(body, dict):
            absorb(_best_solana_pairs_by_mint(body.get("pairs") or []))

    # token-profiles / boosts are mint lists; hydrate via the batch tokens endpoint.
    mints: list[str] = []
    seen_mints = set(by_mint)
    for path, label in (
        ("/token-profiles/latest/v1", "token-profiles/latest"),
        ("/token-boosts/latest/v1", "token-boosts/latest"),
        ("/token-boosts/top/v1", "token-boosts/top"),
    ):
        body = _ds_json(s, path, label=label)
        rows = body if isinstance(body, list) else []
        for row in rows:
            if (row.get("chainId") or "").lower() != "solana":
                continue
            addr = row.get("tokenAddress") or ""
            if addr and addr not in seen_mints and addr not in _QUOTE_MINTS:
                seen_mints.add(addr)
                mints.append(addr)

    # Batch, max 30 addresses per call (DexScreener limit). Cap at 2 batches / 60 mints.
    for i in range(0, min(len(mints), 60), 30):
        chunk = mints[i:i + 30]
        body = _ds_json(s, f"/latest/dex/tokens/{','.join(chunk)}",
                        label=f"tokens batch {i // 30 + 1} n={len(chunk)}")
        if isinstance(body, dict):
            absorb(_best_solana_pairs_by_mint(body.get("pairs") or []))

    out = list(by_mint.values())
    LAST["dexscreener"]["pools"] = len(out)
    return out


def new_tokens(session: requests.Session | None = None) -> list[dict]:
    """Solana pools worth looking at, deduplicated by mint.

    Two sources, because neither alone can pass the desk's filters — and because
    GeckoTerminal rate-limits (and sometimes blocks) cloud IPs with a 429 that
    used to be swallowed as "0 pools":

      geckoterminal  /networks/solana/pools (volume) and /new_pools (launches)
      dexscreener    token-profiles, boosts, and search (SOL, pump), hydrated
                     to the same shape. Used as a merged fallback, not a demo feed.
    """
    s = session or requests.Session()
    _reset_last()
    out, seen = [], set()

    for t in _from_geckoterminal(s) + _from_dexscreener(s):
        if t["mint"] in seen:
            continue
        seen.add(t["mint"])
        out.append(t)
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
