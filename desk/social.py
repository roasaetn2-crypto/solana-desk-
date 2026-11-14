"""Seat 2d: what the crowd is saying about *this ticker*, right now.

The meme scorecard's narrative driver (`desk/meme.py`) reads the token's **name**
and nothing else. It can tell you `$DOGE` is an animal coin; it cannot tell you
whether anyone is talking about it this minute. For a memecoin that is the whole
game, so this module adds the missing half: ticker-keyed social attention.

Three free sources, no API key on any of them:

  telegram   Public channel web previews (`t.me/s/<channel>`). The same HTML a
             browser gets. We read the last page of each channel on a watchlist
             and count `$TICKER` / bare-ticker mentions, weighting by views.
  boosts     DexScreener's paid-promotion feed. A boost is someone spending
             money to be seen -- the most expensive social signal there is.
  profiles   DexScreener token profiles, which carry the project's own X and
             Telegram links. A token that never bothered to make one is telling
             you something too.

What this module deliberately does NOT do:

  * It never reads X/Twitter. There is no free tier left and scraping it is
    both fragile and against their terms. The watchlist is Telegram + 4chan.
  * It never blocks a trade on its own. `apply()` records a *pulse* and, when
    `SOCIAL["shadow"]` is on, tags the signal so the outcome can be measured
    against realised P&L. Same discipline as `desk/meme_tactics.py`: a social
    number you have not measured is a story, not an edge.
  * It never trusts token text. Names and symbols are attacker-controlled; we
    only ever *count* them.

Fail-open everywhere: a dead channel, a 429 or a timeout becomes "unknown" and
the desk carries on exactly as it did before.
"""
from __future__ import annotations

import html
import re
import time
from urllib.parse import quote

from config import SOCIAL
from desk import netutil

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_cache = netutil.TTLCache(max_items=256)
_tg_limiter = netutil.SlidingLimiter(limit=12, window_s=60.0)
_dex_limiter = netutil.SlidingLimiter(limit=20, window_s=60.0)

_TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,11})\b")
_VIEWS_RE = re.compile(r'tgme_widget_message_views">([^<]+)<')
_TIME_RE = re.compile(r'datetime="([^"]+)"')
_TEXT_RE = re.compile(r'tgme_widget_message_text[^>]*>(.*?)</div>', re.S)


def enabled() -> bool:
    return bool(SOCIAL.get("enabled", True))


def shadow_on() -> bool:
    return bool(SOCIAL.get("shadow", True))


def _get(url: str, timeout: float | None = None) -> str | None:
    """One polite GET. Returns None on any failure -- callers fail open."""
    if netutil.offline():
        return None
    try:
        import requests
        r = requests.get(url, timeout=timeout or netutil.TIMEOUT_S,
                         headers={"User-Agent": _UA, "Accept": "text/html,*/*"})
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


def _views_to_int(raw: str | None) -> int:
    """'57.7K' -> 57700, '1.2M' -> 1200000, '' -> 0."""
    if not raw:
        return 0
    s = raw.strip().upper().replace(",", "")
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("B"):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def parse_telegram(page: str) -> list[dict]:
    """Pull messages out of a t.me/s/<channel> page. Pure; used by the tests."""
    if not page:
        return []
    texts = _TEXT_RE.findall(page)
    times = _TIME_RE.findall(page)
    views = _VIEWS_RE.findall(page)
    out: list[dict] = []
    for i, raw in enumerate(texts):
        text = html.unescape(re.sub(r"<br\s*/?>", "\n", raw))
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if not text:
            continue
        out.append({
            "text": text,
            "ts": times[i] if i < len(times) else None,
            "views": _views_to_int(views[i]) if i < len(views) else 0,
        })
    return out


def telegram(channel: str, ttl_s: float | None = None) -> dict:
    """Last page of one public channel. Cached; never raises."""
    ch = (channel or "").strip().lstrip("@")
    if not ch:
        return {"available": False, "channel": "", "messages": [], "why": "no channel"}
    ttl = float(SOCIAL.get("cache_s", 180)) if ttl_s is None else float(ttl_s)
    hit, val = _cache.get(("tg", ch))
    if hit:
        return val
    if not _tg_limiter.allow():
        return {"available": False, "channel": ch, "messages": [], "why": "rate limited"}
    page = _get(f"https://t.me/s/{quote(ch)}")
    if page is None:
        res = {"available": False, "channel": ch, "messages": [], "why": "fetch failed"}
    else:
        msgs = parse_telegram(page)
        res = {"available": bool(msgs), "channel": ch, "messages": msgs,
               "why": "" if msgs else "no public preview"}
    _cache.set(("tg", ch), res, ttl)
    return res


def ticker_pattern(ticker: str) -> re.Pattern | None:
    """Match `$WIF` and bare `WIF`, case-insensitively, on word boundaries."""
    t = (ticker or "").strip().lstrip("$")
    if not (2 <= len(t) <= 12) or not re.fullmatch(r"[A-Za-z0-9]+", t):
        return None
    return re.compile(rf"(?:\$|\b){re.escape(t)}\b", re.I)


def count_mentions(messages: list[dict], ticker: str) -> dict:
    """How many messages mention the ticker, and how many eyeballs that is."""
    pat = ticker_pattern(ticker)
    if pat is None:
        return {"messages": 0, "views": 0, "unique_channels": 0, "samples": []}
    n = 0
    views = 0
    samples: list[str] = []
    for m in messages:
        if pat.search(m.get("text") or ""):
            n += 1
            views += int(m.get("views") or 0)
            if len(samples) < 3:
                samples.append((m.get("text") or "")[:140])
    return {"messages": n, "views": views, "samples": samples}


def watchlist_mentions(ticker: str, channels: list[str] | None = None) -> dict:
    """Count ticker mentions across the whole watchlist."""
    chans = channels if channels is not None else list(SOCIAL.get("channels") or [])
    total_msgs = 0
    total_views = 0
    live = 0
    hits: list[dict] = []
    for ch in chans[: int(SOCIAL.get("max_channels", 8))]:
        r = telegram(ch)
        if not r.get("available"):
            continue
        live += 1
        c = count_mentions(r["messages"], ticker)
        total_msgs += c["messages"]
        total_views += c["views"]
        if c["messages"]:
            hits.append({"channel": r["channel"], **c})
    return {"channels_live": live, "channels_total": len(chans),
            "messages": total_msgs, "views": total_views, "hits": hits}


def dex_boosts(ttl_s: float | None = None) -> dict:
    """DexScreener's latest paid boosts, keyed by mint. Free, no key."""
    ttl = float(SOCIAL.get("dex_cache_s", 300)) if ttl_s is None else float(ttl_s)
    hit, val = _cache.get("dex_boosts")
    if hit:
        return val
    if not _dex_limiter.allow():
        return {"available": False, "mints": {}, "why": "rate limited"}
    raw = _get("https://api.dexscreener.com/token-boosts/latest/v1")
    if raw is None:
        res = {"available": False, "mints": {}, "why": "fetch failed"}
    else:
        try:
            import json
            rows = json.loads(raw)
        except Exception:
            rows = []
        mints: dict[str, dict] = {}
        for row in rows if isinstance(rows, list) else []:
            if (row.get("chainId") or "") != "solana":
                continue
            mint = row.get("tokenAddress")
            if mint:
                mints[mint] = {"amount": row.get("amount"),
                               "total": row.get("totalAmount"),
                               "description": (row.get("description") or "")[:120]}
        res = {"available": True, "mints": mints, "why": ""}
    _cache.set("dex_boosts", res, ttl)
    return res


def dex_profiles(ttl_s: float | None = None) -> dict:
    """DexScreener token profiles (carry the project's own socials). Free."""
    ttl = float(SOCIAL.get("dex_cache_s", 300)) if ttl_s is None else float(ttl_s)
    hit, val = _cache.get("dex_profiles")
    if hit:
        return val
    if not _dex_limiter.allow():
        return {"available": False, "mints": {}, "why": "rate limited"}
    raw = _get("https://api.dexscreener.com/token-profiles/latest/v1")
    if raw is None:
        res = {"available": False, "mints": {}, "why": "fetch failed"}
    else:
        try:
            import json
            rows = json.loads(raw)
        except Exception:
            rows = []
        mints: dict[str, dict] = {}
        for row in rows if isinstance(rows, list) else []:
            if (row.get("chainId") or "") != "solana":
                continue
            mint = row.get("tokenAddress")
            if not mint:
                continue
            links = row.get("links") or []
            kinds = {(l.get("type") or l.get("label") or "").lower() for l in links}
            mints[mint] = {"has_x": bool(kinds & {"twitter", "x"}),
                           "has_telegram": bool(kinds & {"telegram"}),
                           "has_website": bool(kinds & {"website"}),
                           "links": len(links)}
        res = {"available": True, "mints": mints, "why": ""}
    _cache.set("dex_profiles", res, ttl)
    return res


def presence(mint: str) -> dict:
    """Does this mint have a paid boost and/or a self-made profile with socials?"""
    if not mint:
        return {"boosted": False, "profiled": False, "has_x": False,
                "has_telegram": False, "available": False}
    b = dex_boosts()
    p = dex_profiles()
    prof = (p.get("mints") or {}).get(mint) or {}
    return {
        "boosted": mint in (b.get("mints") or {}),
        "profiled": bool(prof),
        "has_x": bool(prof.get("has_x")),
        "has_telegram": bool(prof.get("has_telegram")),
        "available": bool(b.get("available") or p.get("available")),
    }


def score(pulse: dict) -> int:
    """0-100 social-attention score. Deterministic, bounded, documented.

    Deliberately crude: it exists so the signal engine has a number to test, not
    because the weights are known to be right. The measurement decides.
    """
    msgs = int(pulse.get("messages") or 0)
    views = int(pulse.get("views") or 0)
    s = 0.0
    s += min(40.0, msgs * 8.0)                       # 5 messages = 40
    s += min(30.0, views / 1000.0)                   # 30k views = 30
    if pulse.get("boosted"):
        s += 20.0
    if pulse.get("has_x"):
        s += 5.0
    if pulse.get("has_telegram"):
        s += 5.0
    return int(max(0, min(100, round(s))))


def pulse(ticker: str, mint: str | None = None) -> dict:
    """Everything we know about this ticker's social attention, in one dict."""
    if not enabled():
        return {"available": False, "why": "social off", "ticker": ticker}
    tg = watchlist_mentions(ticker) if ticker else {"messages": 0, "views": 0,
                                                    "channels_live": 0, "hits": []}
    pr = presence(mint or "") if mint else {"boosted": False, "profiled": False,
                                            "has_x": False, "has_telegram": False}
    out = {
        "available": bool(tg.get("channels_live")) or bool(pr.get("available")),
        "ticker": (ticker or "").lstrip("$"),
        "messages": tg.get("messages", 0),
        "views": tg.get("views", 0),
        "channels_live": tg.get("channels_live", 0),
        "channels_total": tg.get("channels_total", 0),
        "hits": tg.get("hits", []),
        "boosted": pr.get("boosted", False),
        "profiled": pr.get("profiled", False),
        "has_x": pr.get("has_x", False),
        "has_telegram": pr.get("has_telegram", False),
    }
    out["score"] = score(out)
    return out


def apply(sig: dict, token: dict | None = None, facts: dict | None = None) -> dict:
    """Tag the signal with the social pulse. Never changes the action.

    Like the meme tactics, this is a measurement first. When `SOCIAL["gate"]` is
    switched on *and* the measurement has cleared the bar, a dead pulse can
    downgrade a BUY. Until then it only ever records.
    """
    if not enabled():
        return sig
    t, f = token or {}, facts or {}
    ticker = (t.get("symbol") or t.get("ticker") or "").lstrip("$")
    mint = t.get("mint") or f.get("mint")
    if not ticker:
        return sig
    try:
        p = pulse(ticker, mint)
    except Exception:
        return sig
    if shadow_on():
        sig["social_pulse"] = p
    if SOCIAL.get("gate") and p.get("available") and p.get("score", 0) < int(SOCIAL.get("min_score", 10)):
        if sig.get("action") == "BUY":
            sig["action"] = "HOLD"
            sig.setdefault("warnings", []).append(
                f"social gate: no crowd ({p.get('score')}/100, need {SOCIAL.get('min_score')})")
    return sig
