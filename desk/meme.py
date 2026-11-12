"""What gives a memecoin value: a scorecard, not a vibe.

Five things actually carry a memecoin, and the desk scores each one from
evidence it already collected:

    community     real distributed holders, socials that talk back, buy breadth
    dev_legit     the creator investigation: holdings, history, snipers, authority
    narrative     animal / celebrity / culture / utility: does it have a story
    distribution  top-holder and insider concentration
    utility       does it do anything beyond the ticker

The weights live in config.MEME. The output is a 0-100 score with the reasons
attached, so a human can disagree with the number and see exactly why.

Also here: the hold-horizon rule. A fresh launch and a week-old token are not
traded on the same chart, so the desk states which timeframe the setup belongs
to before it ever sizes a ticket.
"""
from __future__ import annotations

import re

from config import HOLD_HORIZONS, MEME


def classify_narrative(symbol: str, name: str = "", website: str | None = None) -> dict:
    """Tag the story. A token can carry more than one tag; the strongest wins."""
    blob = f"{symbol or ''} {name or ''} {website or ''}".lower()
    words = set(re.findall(r"[a-z0-9]+", blob))

    hits: dict[str, list[str]] = {}
    for tag, keys in MEME["narratives"].items():
        found = [k for k in keys
                 if k in words
                 or (len(k) >= 3 and k in blob)
                 or (len(k) == 2 and any(w.startswith(k) or w.endswith(k) for w in words))]
        if found:
            hits[tag] = found

    # priority: an explicit celebrity/animal beats generic "community"
    order = ["celebrity", "animal", "utility", "community"]
    primary = next((t for t in order if t in hits), "culture")
    return {
        "primary": primary,
        "tags": list(hits),
        "matched": sorted({w for ws in hits.values() for w in ws}),
        "is_meme": primary in ("animal", "celebrity", "community", "culture"),
        "has_utility_story": "utility" in hits,
    }


def hold_horizon(age_minutes: float | None) -> dict:
    """Match the timeframe to how long the position is meant to live."""
    age = age_minutes if age_minutes is not None else 10**9
    for h in HOLD_HORIZONS:
        if age <= h["max_age_minutes"]:
            return dict(h)
    return dict(HOLD_HORIZONS[-1])


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def scorecard(token: dict, facts: dict, ds: dict, dev: dict,
              narrative: dict | None = None) -> dict:
    """Score the five value drivers 0-100 each, then weight them."""
    narrative = narrative or classify_narrative(
        token.get("symbol", ""), token.get("name", ""),
        (ds.get("websites") or [None])[0])

    liq = token.get("liquidity_usd") or ds.get("liquidity_usd") or 0
    mcap = token.get("mcap_usd") or ds.get("mcap_usd") or 0
    vol1 = token.get("volume_h1") or ds.get("volume_h1") or 0
    buys = token.get("buys_h1") or ds.get("buys_h1") or 0
    sells = token.get("sells_h1") or ds.get("sells_h1") or 0
    buyers = token.get("buyers_h1") or 0
    # The public RPC can only read the top 20 token accounts, so `wallets_counted`
    # is a SAMPLE SIZE, not a holder count. The old code fed it into the community
    # score as if it were the population: every token looked like it had exactly
    # 20 holders, scored 4/30 on community, and was rejected as "no community".
    # Only trust a count that is bigger than the sample it came from.
    sampled = facts.get("wallets_counted") or 0
    raw = dev.get("holder_count") or facts.get("holder_count") or 0
    holders = raw if (raw and (not sampled or raw > sampled)) else 0

    # ---------- community ----------
    community, c_why = 0.0, []
    if holders >= MEME["min_holders"]:
        community += 30; c_why.append(f"{holders} holders")
    elif holders:
        community += min(30, holders / max(MEME["min_holders"], 1) * 30)
        c_why.append(f"only {holders} holders")
    else:
        # Unknown is not zero. Give the neutral middle and let the observable
        # signals (socials, buy breadth) decide
        
