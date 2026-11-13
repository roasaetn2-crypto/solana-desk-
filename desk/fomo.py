"""FOMO evidence: warnings, dev/insider bags, and thesis sentiment.

fomo.family has no public developer API. This talks to fomoapi.io, an
independent third-party bridge, and it is entirely optional: with no
FOMO_API_KEY in the environment every function returns "unknown" and the desk
runs exactly as it did before.

What this seat adds that the rest of the stack cannot see:

  warnings   FOMO's own coin-page flags (sell disabled, insider holdings,
             few holders). These are hard kill signals, not score nudges.
  devs       The deployer and insiders with their cost basis and PnL, so a dev
             quietly closing his own bag shows up here before the price does.
  theses     What traders actually wrote about the coin, scored for conviction.

The theses are the reason this module exists, and they are also the reason it
is careful. Real FOMO thesis text looks like this:

    "Free"                                    "Sending"
    "big bossy"                               "ChatGPT said this should skyrocket"
    "go up please bro this is everything :sob:"

None of that is bullish conviction. A naive keyword counter would read the
last one as a strong buy, when it is a bagholder pleading. So sentiment here
is a deterministic lexicon score *weighted by whether the author is actually
making money*, and it shrinks toward neutral when the evidence is thin. If the
crowd is just noise, this returns 50 with low confidence and says so. The desk
treats "unknown" as unknown, never as a signal.

Money rules, because credits are real:

  * every call is metered from the x-credits-cost response header
  * a monthly budget is enforced in code before the request, not after
  * ETags are stored and replayed, so an unchanged answer costs 0 credits
  * 503 "did not answer in time" is retryable and costs nothing, so we retry
  * a failed or unaffordable lookup is "unknown", never "clean"

Nothing here raises into the trading loop.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests

from desk import netutil, ops

log = logging.getLogger("desk.fomo")

BASE = os.getenv("FOMO_BASE", "https://api.fomoapi.io").rstrip("/")
SOLANA_NETWORK_ID = 1399811149

# Free tier is 250,000 credits/month. Ordinary reads are 250, a thesis page is
# 1,250. Keep a reserve so a burst cannot starve the rest of the month.
MONTHLY_CREDITS = int(os.getenv("FOMO_MONTHLY_CREDITS", "250000"))
RESERVE_CREDITS = int(os.getenv("FOMO_RESERVE_CREDITS", "5000"))

COST_READ = 250          # warnings, devs, holders, stats
COST_THESIS = 1250       # per thesis page

TIMEOUT_S = float(os.getenv("FOMO_TIMEOUT_S", "20"))
RETRIES = int(os.getenv("FOMO_RETRIES", "4"))
CACHE_TTL_S = int(os.getenv("FOMO_CACHE_TTL_S", str(20 * 60)))

# Free keys get 20 requests/minute. allow() skips instead of sleeping.
_limiter = netutil.SlidingLimiter(int(os.getenv("FOMO_RPM", "20")), 60.0)
_cache = netutil.TTLCache(max_items=256)
_etags: dict[str, str] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- credentials


def api_key() -> str:
    return (os.getenv("FOMO_API_KEY") or "").strip()


def enabled() -> bool:
    """FOMO is opt-in. No key, or DESK_OFFLINE=1, means every answer is unknown."""
    return bool(api_key()) and not netutil.offline()


# ------------------------------------------------------------------- credits


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _usage_path():
    return ops.data_file("fomo_usage.json")


def _load_usage() -> dict:
    try:
        raw = json.loads(_usage_path().read_text())
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def _save_usage(u: dict) -> None:
    try:
        _usage_path().write_text(json.dumps(u, indent=1))
    except Exception:
        log.debug("fomo: could not persist usage", exc_info=True)


def usage() -> dict:
    """Credits spent this month, and what is left of the configured budget."""
    u = _load_usage()
    spent = int(u.get(_month(), 0) or 0)
    return {
        "month": _month(),
        "spent": spent,
        "budget": MONTHLY_CREDITS,
        "left": max(0, MONTHLY_CREDITS - spent),
        "reserve": RESERVE_CREDITS,
    }


def _charge(credits: int) -> None:
    if credits <= 0:
        return
    with _lock:
        u = _load_usage()
        u[_month()] = int(u.get(_month(), 0) or 0) + int(credits)
        # keep last 3 months only
        for k in sorted(u)[:-3]:
            u.pop(k, None)
        _save_usage(u)


def affordable(cost: int) -> bool:
    """Budget check happens *before* the call, so we never overspend into a 402."""
    return usage()["left"] - cost >= RESERVE_CREDITS


# --------------------------------------------------------------------- etags


def _etag_path():
    return ops.data_file("fomo_etags.json")


def _load_etags() -> None:
    global _etags
    try:
        raw = json.loads(_etag_path().read_text())
        if isinstance(raw, dict):
            _etags = {str(k): str(v) for k, v in raw.items()}
    except Exception:
        _etags = {}


def _save_etags() -> None:
    try:
        _etag_path().write_text(json.dumps(_etags, indent=0))
    except Exception:
        log.debug("fomo: could not persist etags", exc_info=True)


_load_etags()


# ------------------------------------------------------------------ transport


class FomoError(Exception):
    """A lookup that did not produce data. Callers turn this into 'unknown'."""


def _get(path: str, params: dict | None = None, cost: int = COST_READ,
         transport=None) -> dict:
    """One metered GET. Returns parsed JSON, or raises FomoError.

    `transport` is an injectable callable(url, headers, timeout) -> response
    object with .status_code, .headers, .json(), so tests never touch a socket.
    """
    if not enabled():
        raise FomoError("fomo disabled (no FOMO_API_KEY)")

    qs = ""
    if params:
        parts = [f"{k}={v}" for k, v in params.items() if v is not None]
        if parts:
            qs = "?" + "&".join(parts)
    url = f"{BASE}{path}{qs}"

    hit, cached = _cache.get(url)
    if hit:
        return cached

    if not affordable(cost):
        raise FomoError(f"fomo budget: {usage()['left']} credits left, need {cost}")

    if not _limiter.allow():
        raise FomoError("fomo rate limit: skipped this cycle")

    headers = {
        "authorization": f"Bearer {api_key()}",
        "accept": "application/json",
        "user-agent": "solana-trading-desk/1.0",
    }
    with _lock:
        tag = _etags.get(url)
    if tag:
        headers["if-none-match"] = tag

    last = None
    for attempt in range(max(1, RETRIES)):
        try:
            if transport is not None:
                r = transport(url, headers, TIMEOUT_S)
            else:
                r = requests.get(url, headers=headers, timeout=TIMEOUT_S)

            code = int(getattr(r, "status_code", 0) or 0)
            hdrs = getattr(r, "headers", {}) or {}

            # 304: unchanged, and explicitly free.
            if code == 304:
                _cache.set(url, cached if hit else {"available": False,
                                                    "reason": "not-modified"}, CACHE_TTL_S)
                if hit:
                    return cached
                raise FomoError("fomo: 304 with no cached body")

            spent = _int(hdrs.get("x-credits-cost"))
            _charge(spent)

            new_tag = hdrs.get("etag")
            if new_tag:
                with _lock:
                    _etags[url] = str(new_tag)
                    _save_etags()

            if code == 200:
                body = r.json()
                if not isinstance(body, dict):
                    raise FomoError("fomo: non-object body")
                _cache.set(url, body, CACHE_TTL_S)
                return body

            # 503 upstream timeout is retryable and costs nothing.
            if code in (502, 503, 504) or code == 429:
                retry_after = _int(hdrs.get("retry-after"))
                last = FomoError(f"fomo: HTTP {code}")
                time.sleep(min(8.0, (retry_after or 2) * (attempt + 1) * 0.5))
                continue

            if code == 401:
                raise FomoError("fomo: key rejected (401)")
            if code in (402, 403):
                raise FomoError(f"fomo: credits/permission (HTTP {code})")
            raise FomoError(f"fomo: HTTP {code}")

        except FomoError:
            raise
        except Exception as e:  # network, json, timeout
            last = FomoError(f"fomo: {type(e).__name__}")
            time.sleep(min(6.0, 0.5 * (attempt + 1)))

    raise last or FomoError("fomo: exhausted retries")


def _int(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _f(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ thesis lexicon
#
# Hand-tuned for how people actually write on FOMO: lowercase, emoji, slang,
# and a lot of pleading. Groups are ordered by how much they should be trusted.

_BULL = [
    "moon", "moonshot", "mooning", "send it", "sending it", "rip", "ripping",
    "pump", "pumping", "breakout", "breaking out", "ath", "all time high",
    "parabolic", "liftoff", "gem", "undervalued", "accumulate", "accumulating",
    "loaded", "loading", "diamond hands", "bullish", "bull", "uptrend",
    "up only", "higher", "long", "10x", "100x", "x10", "multiplier",
    "supercycle", "squeeze", "melt up", "catalyst", "listing", "binance",
    "coinbase", "partnership", "buyback", "burn", "deflationary", "locked",
    "renounced", "dev sold", "clean", "early", "frontrun", "next leg",
    "support holding", "reclaim", "inverse head", "cup and handle",
    "volume coming", "waking up", "run it", "let it run", "hold", "holding",
    "held up", "bag is heavy", "topped up", "bought more", "buy more",
    "adding", "accumulated", "accumulation", "loaded up", "still holding",
]
_BEAR = [
    "dump", "dumping", "dumped", "rug", "rugged", "rugpull", "rug pull",
    "exit liquidity", "sell", "selling", "sold", "short", "shorting",
    "bearish", "bear", "crash", "collapsing", "collapse", "dead", "dying",
    "jeet", "jeets", "bagholder", "bag hold", "rekt", "liquidity gone",
    "unlock", "dilution", "honeypot", "cant sell", "can't sell", "cannot sell",
    "scam", "ponzi", "insider", "bundled", "snipers", "slow rug",
    "distribution", "topped", "blow off", "fade", "fading", "weak",
    "no volume", "dead volume", "its over", "it's over", "game over",
    "finished", "down bad", "down only", "lower", "capitulate",
    "capitulation", "sell wall", "unload",
]
# Pleading is not conviction. It is the sound of a position already underwater.
_DOUBT = [
    "please", "pls", "plz", "plissss", "plsss", "come on", "cmon", "cmoon",
    "need this", "need it", "everything", "all in", "last chance", "help",
    "save us", "nooo", "noo", "why", "praying", "pray", "hope", "hoping",
    "copium", "surely", "eventually", "any day now", "still waiting",
    "trust me", "trust me bro", "believe", "manifest", "sending prayers",
    "dont let me down", "don't let me down", "i beg", "begging",
]
# Irony, jokes, and other people's homework: near-zero information.
_IRONY = [
    "chatgpt", "gpt said", "ai said", "source:", "trust me bro",
    "guaranteed", "100% sure", "definitely going", "free", "gm", "wagmi",
    "lfg", "lol", "lmao", "haha", "hehe", "kek", "big bossy", "fuck it",
    "yolo", "balled", "js said", "said fuck", "shi", "idk", "maybe",
]
_NEGATORS = ["not", "no", "never", "wont", "won't", "dont", "don't",
             "isnt", "isn't", "cant", "can't", "aint", "ain't", "stop"]

_EMOJI_BULL = ["\U0001F680", "\U0001F525", "\U0001F4C8", "\U0001F48E", "\U0001F7E2"]
_EMOJI_BEAR = ["\U0001F4C9", "\U0001F480", "\U0001FA78", "\U0001F534"]
_EMOJI_DOUBT = ["\U0001F62D", "\U0001F64F", "\U0001F622", "\U0001F613"]


def _hits(text: str, terms: list[str]) -> int:
    n = 0
    for t in terms:
        if " " in t:
            if t in text:
                n += 1
        elif re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text):
            n += 1
    return n


def thesis_sentiment(text: str) -> dict:
    """Score one thesis in [-1, 1]. Deterministic, offline, explainable.

    Returns the raw counts too, so a rejection can always be justified in the
    journal rather than being a black box.
    """
    raw = (text or "").strip()
    t = raw.lower()
    if not t:
        return {"score": 0.0, "bull": 0, "bear": 0, "doubt": 0, "irony": 0,
                "words": 0, "note": "empty"}

    bull = _hits(t, _BULL) + sum(raw.count(e) for e in _EMOJI_BULL)
    bear = _hits(t, _BEAR) + sum(raw.count(e) for e in _EMOJI_BEAR)
    doubt = _hits(t, _DOUBT) + sum(raw.count(e) for e in _EMOJI_DOUBT)
    irony = _hits(t, _IRONY)
    words = len(re.findall(r"[a-z']+", t))

    # Negation: "not going up", "won't pump" flips the nearest polarity.
    if _hits(t, _NEGATORS) and bull > bear:
        bull, bear = bear, bull

    total = bull + bear
    if total == 0:
        score = 0.0
    else:
        score = (bull - bear) / total

    note = []
    if doubt and bull:
        score *= 0.35
        note.append("pleading")
    if irony:
        score *= 0.15
        note.append("irony")
    if words < 3:
        score *= 0.30
        note.append("low-information")
    if total == 0 and not doubt and not irony:
        note.append("no-signal")

    return {"score": round(max(-1.0, min(1.0, score)), 4), "bull": bull,
            "bear": bear, "doubt": doubt, "irony": irony, "words": words,
            "note": ",".join(note) or "ok"}


def _credibility(row: dict) -> float:
    """How much should one trader's opinion move the needle?

    Equity is 0 on this route, so credibility comes from realised PnL, the size
    of the position the thesis sits on, and how many people agreed with it. A
    trader who is deeply underwater is rationalising, not calling it.
    """
    w = 1.0
    rpnl = _f(row.get("realizedPnlUsd"))
    if rpnl > 0:
        w *= 1.0 + min(1.5, math.log1p(rpnl) / 5.0)      # up to 2.5x
    elif rpnl < 0:
        w *= max(0.25, 1.0 - min(0.75, math.log1p(-rpnl) / 8.0))  # down to 0.25x

    trade = abs(_f(row.get("tradeUsd")))
    if trade >= 1000:
        w *= 1.6
    elif trade >= 100:
        w *= 1.3
    elif trade < 10:
        w *= 0.6

    likes = max(0, _int(row.get("likes")))
    w *= 1.0 + min(0.6, likes / 10.0)

    if row.get("isDev"):
        w *= 0.4   # a dev talking his own book is not independent evidence

    return max(0.05, w)


def score_theses(theses: list[dict]) -> dict:
    """Aggregate thesis rows into one 0-100 conviction score.

    Shrinks toward 50 when the evidence is thin, so a coin with three jokes
    reads as 'no opinion', not as 'bearish'.
    """
    rows = [t for t in (theses or []) if isinstance(t, dict)]
    if not rows:
        return {"available": True, "score": None, "confidence": "none",
                "count": 0, "bull": 0, "bear": 0, "neutral": 0,
                "why": ["no theses returned"]}

    num = den = 0.0
    bull = bear = neutral = 0
    dev_theses = 0
    samples = []
    for r in rows:
        s = thesis_sentiment(r.get("text", ""))
        w = _credibility(r)
        num += w * s["score"]
        den += w
        if s["score"] > 0.15:
            bull += 1
        elif s["score"] < -0.15:
            bear += 1
        else:
            neutral += 1
        if r.get("isDev"):
            dev_theses += 1
        if len(samples) < 5 and abs(s["score"]) > 0.15:
            samples.append({"handle": r.get("handle"),
                            "text": (r.get("text") or "")[:80],
                            "score": s["score"], "note": s["note"]})

    mean = (num / den) if den else 0.0
    raw = 50.0 + 50.0 * mean

    # Confidence from how much *credible* weight we actually have. A trader
    # counts as credible only if the numbers behind the opinion are real: a
    # meaningful realised result, real agreement, or a position worth caring
    # about. Otherwise every dust-position joke would read as high confidence.
    credible = sum(1 for r in rows
                   if abs(_f(r.get("realizedPnlUsd"))) >= 50
                   or _int(r.get("likes")) >= 3
                   or abs(_f(r.get("tradeUsd"))) >= 250)
    if credible >= 8:
        conf, shrink = "high", 1.0
    elif credible >= 3:
        conf, shrink = "medium", 0.6
    else:
        conf, shrink = "low", 0.3

    score = round(max(0.0, min(100.0, 50.0 + (raw - 50.0) * shrink)))

    why = [f"{len(rows)} theses: {bull} bullish, {bear} bearish, {neutral} neutral",
           f"credible authors: {credible} (confidence {conf})"]
    if dev_theses:
        why.append(f"{dev_theses} written by the dev")
    if conf == "low":
        why.append("thin evidence: score pulled toward neutral")

    return {"available": True, "score": score, "confidence": conf,
            "count": len(rows), "bull": bull, "bear": bear, "neutral": neutral,
            "dev_theses": dev_theses, "mean": round(mean, 3),
            "why": why, "samples": samples}


# ------------------------------------------------------------- endpoint calls


def warnings(mint: str, transport=None) -> dict:
    return _get(f"/v2/token/{mint}/warnings", None, COST_READ, transport)


def devs(mint: str, transport=None) -> dict:
    return _get(f"/v2/token/{mint}/devs", {"networkId": SOLANA_NETWORK_ID},
                COST_READ, transport)


def holders(mint: str, limit: int = 20, transport=None) -> dict:
    return _get(f"/token/{mint}/holders",
                {"network": SOLANA_NETWORK_ID, "limit": limit},
                COST_READ, transport)


def theses(mint: str, threshold: int = 10, sort: str = "pnl",
           limit: int = 50, transport=None) -> dict:
    """Theses on one coin. `threshold` is the minimum position size in USD.

    Default 10 rather than 0: at 0 the feed fills with dust-position jokes.
    """
    return _get(f"/v2/thesis/token/{mint}",
                {"threshold": threshold, "sort": sort, "limit": limit},
                COST_THESIS, transport)


# ---------------------------------------------------------------- the evidence


def token_evidence(mint: str, level: str = "signals", transport=None) -> dict:
    """Everything FOMO knows about one mint, normalised, never raising.

    level="signals"  warnings + devs              (500 credits)
    level="thesis"   the above, plus thesis score (+1,250 credits)

    Use "signals" for every candidate and "thesis" only for finalists. At the
    free tier that is the difference between ~500 and ~140 coins a month.
    """
    out = {
        "available": False,
        "source": "fomoapi.io",
        "why": [],
        "warnings": [],
        "severe": False,
        "buy_disabled": False,
        "sell_disabled": False,
        "trading_disabled": False,
        "dev_holdings": [],
        "dev_pnl_usd": 0.0,
        "thesis": None,
        "credits": usage(),
    }

    if not enabled():
        out["why"].append("FOMO_API_KEY not set")
        return out

    got = False
    try:
        w = warnings(mint, transport)
        got = True
        out["warnings"] = [
            {"type": x.get("type"), "name": x.get("name"),
             "severity": x.get("severity")}
            for x in (w.get("warnings") or []) if isinstance(x, dict)
        ]
        out["severe"] = bool(w.get("severe"))
        out["buy_disabled"] = bool(w.get("buyDisabled"))
        out["sell_disabled"] = bool(w.get("sellDisabled"))
        out["trading_disabled"] = bool(w.get("tradingDisabled"))
    except FomoError as e:
        out["why"].append(f"warnings: {e}")

    try:
        d = devs(mint, transport)
        got = True
        rows = [x for x in (d.get("devs") or []) if isinstance(x, dict)]
        out["dev_holdings"] = [
            {"handle": x.get("handle"), "is_dev": bool(x.get("isDev")),
             "value_usd": round(_f(x.get("valueUsd")), 2),
             "cost_basis_usd": round(_f(x.get("costBasisUsd")), 2),
             "realized_pnl_usd": round(_f(x.get("realizedPnlUsd")), 2),
             "thesis": (x.get("thesis") or "")[:200]}
            for x in rows
        ]
        out["dev_pnl_usd"] = round(sum(_f(x.get("realizedPnlUsd")) for x in rows), 2)
    except FomoError as e:
        out["why"].append(f"devs: {e}")

    if level == "thesis":
        try:
            th = theses(mint, transport=transport)
            if th.get("available") is False:
                # "we could not look" is not "there are none"
                out["thesis"] = {"available": False,
                                 "reason": th.get("reason") or "upstream-unavailable",
                                 "score": None, "confidence": "unknown"}
                out["why"].append(f"theses: {th.get('reason') or 'unavailable'}")
            else:
                sc = score_theses(th.get("theses") or [])
                sc["source"] = th.get("source")
                sc["stale"] = bool(th.get("stale"))
                sc["total_available"] = th.get("totalAvailable")
                out["thesis"] = sc
                got = True
        except FomoError as e:
            out["thesis"] = {"available": False, "reason": str(e),
                             "score": None, "confidence": "unknown"}
            out["why"].append(f"theses: {e}")

    out["available"] = got
    out["credits"] = usage()
    return out


def kill_reason(ev: dict) -> str | None:
    """Hard evidence that FOMO itself says you cannot get out. First wins.

    Only unambiguous, structural failures land here. 'severe' alone does not
    kill: it fires on young coins that are simply thin, and the desk already
    has holder-count rules for that.
    """
    if not ev or not ev.get("available"):
        return None
    if ev.get("trading_disabled"):
        return "fomo_trading_disabled"
    if ev.get("sell_disabled"):
        return "fomo_sell_disabled"
    if ev.get("buy_disabled"):
        return "fomo_buy_disabled"
    types = {str(w.get("type") or "").upper() for w in (ev.get("warnings") or [])}
    if "HIGH_TRANSFER_FEE" in types:
        return "fomo_transfer_fee"
    if "HONEYPOT" in types:
        return "fomo_honeypot"
    return None


def thesis_modifier(ev: dict) -> tuple[int, list[str]]:
    """A bounded nudge to the community score, in points, plus the reasons.

    Capped at +/-8 so a chatty crowd can never outvote the chain. Applied only
    when the thesis evidence is real and confident.
    """
    th = (ev or {}).get("thesis") or {}
    if not th.get("available") or th.get("score") is None:
        return 0, []
    score, conf = th["score"], th.get("confidence")
    if conf == "low":
        return 0, ["FOMO theses too thin to score"]
    delta = (score - 50.0) / 50.0 * 8.0
    delta = int(round(max(-8.0, min(8.0, delta))))
    why = [f"FOMO theses {score}/100 ({conf} confidence, n={th.get('count')})"]
    if th.get("dev_theses"):
        why.append(f"{th['dev_theses']} thesis from the dev")
    return delta, why


def status() -> dict:
    """For /api/state and the AI seat: is FOMO on, and what is left."""
    u = usage()
    return {
        "enabled": enabled(),
        "key_present": bool(api_key()),
        "base": BASE,
        "month": u["month"],
        "credits_spent": u["spent"],
        "credits_budget": u["budget"],
        "credits_left": u["left"],
        "cost_read": COST_READ,
        "cost_thesis": COST_THESIS,
        "affordable_read": affordable(COST_READ),
        "affordable_thesis": affordable(COST_THESIS),
        "cached_etags": len(_etags),
    }


def reset_cache() -> None:
    """Tests and the Reset button: forget cached bodies, keep the credit meter."""
    global _etags
    _cache.clear()
    with _lock:
        _etags = {}
        _save_etags()
    _limiter.reset()
