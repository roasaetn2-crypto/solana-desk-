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

from config import CHAIN, HOLD_HORIZONS, MEME


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


HOLDERS_NEUTRAL = 15.0      # half of the 30 holder points, when the real count is unknown


def real_holder_count(facts: dict, dev: dict) -> int | None:
    """A real holder total (Helius DAS or RugCheck totalHolders), or None.

    The public RPC only shows the largest 20 accounts (facts["wallets_counted"]), and RugCheck's
    topHolders list is the same kind of sample. A sample size is not a holder count, so a
    number counts only when it is a positive total that is larger than the sample, or the sample
    was not full (then the coin really has that few holders).
    """
    sample = max(int(facts.get("wallets_counted") or 0), int(dev.get("holder_sample") or 0))
    full = CHAIN.get("largest_accounts_checked", 20)
    best = None
    for v in (facts.get("holder_count"), dev.get("holder_count")):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            continue
        best = int(v) if best is None else max(best, int(v))
    if best is None:
        return None
    if best > sample or sample < full:
        return best
    return None


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
    holders = real_holder_count(facts, dev)

    # ---------- community ----------
    community, c_why = 0.0, []
    if holders is None:
        # No trustworthy total (only the top-20 sample): neither credit nor penalty.
        community += HOLDERS_NEUTRAL; c_why.append("holder count unknown (neutral)")
    elif holders >= MEME["min_holders"]:
        community += 30; c_why.append(f"{holders} holders")
    else:
        community += min(30, holders / max(MEME["min_holders"], 1) * 30)
        c_why.append(f"only {holders} holders")
    if ds.get("has_x") or ds.get("has_telegram"):
        community += 20; c_why.append("socials published")
    else:
        c_why.append("no socials")
    if buys >= 200:
        community += 20; c_why.append(f"{buys} buys/h")
    elif buys >= 60:
        community += 10; c_why.append(f"{buys} buys/h")
    if buyers and buys:
        per = buys / max(buyers, 1)
        if per <= 2.5:
            community += 20; c_why.append(f"{buyers} distinct buyers")
        elif per > 4:
            c_why.append("few wallets doing many buys")
    if ds.get("boosts_active"):
        community += 5; c_why.append("paid boosts active")
    community = _clamp(community)

    # ---------- dev legitimacy ----------
    dev_legit = _clamp(dev.get("dev_score", 0))

    # ---------- narrative ----------
    narrative_s = {"celebrity": 80, "animal": 72, "community": 60,
                   "culture": 45, "utility": 55}.get(narrative["primary"], 40)
    if len(narrative["tags"]) > 1:
        narrative_s += 8
    if narrative.get("has_utility_story"):
        narrative_s += 5
    narrative_s = _clamp(narrative_s)

    # ---------- distribution ----------
    distribution, d_why = 60.0, []
    top1 = facts.get("top_wallet_pct")
    top10 = facts.get("top_10_pct")
    if top1 is not None:
        if top1 <= 0.03:
            distribution += 20; d_why.append(f"top wallet {top1:.1%}")
        elif top1 <= 0.08:
            distribution += 8; d_why.append(f"top wallet {top1:.1%}")
        else:
            distribution -= 30; d_why.append(f"top wallet {top1:.1%}")
    if top10 is not None:
        if top10 <= 0.25:
            distribution += 15; d_why.append(f"top10 {top10:.1%}")
        elif top10 > 0.40:
            distribution -= 25; d_why.append(f"top10 {top10:.1%}")
    sniper = dev.get("sniper_pct")
    if sniper is not None and sniper > MEME["max_sniper_pct"]:
        distribution -= 25; d_why.append(f"snipers {sniper:.1%}")
    distribution = _clamp(distribution)

    # ---------- utility ----------
    utility = 20.0
    if narrative.get("has_utility_story"):
        utility += 35
    if ds.get("has_website"):
        utility += 15
    if liq >= 50_000:
        utility += 10
    if ds.get("boosts_active"):
        utility += 5
    utility = _clamp(utility)

    parts = {"community": community, "dev_legit": dev_legit, "narrative": narrative_s,
             "distribution": distribution, "utility": utility}
    total = sum(parts[k] * MEME["weights"][k] for k in parts)
    total = round(_clamp(total))

    bands = MEME["bands"]
    if total >= bands["strong"] and dev.get("legit"):
        verdict = "community_coin"
    elif total >= bands["watch"]:
        verdict = "watch"
    else:
        verdict = "not_a_community_coin"

    return {
        "total": total,
        "verdict": verdict,
        "parts": {k: round(v) for k, v in parts.items()},
        "weights": MEME["weights"],
        "narrative": narrative,
        "reasons": {
            "community": c_why,
            "dev_legit": dev.get("reasons", []),
            "distribution": d_why,
        },
        "horizon": hold_horizon(token.get("age_minutes")),
        "volume_mcap": round(vol1 / mcap, 3) if mcap else None,
        "sell_ratio": round(sells / buys, 3) if buys else None,
    }


def meme_kill(sc: dict) -> str | None:
    """A scorecard can veto on its own: no community, no trade."""
    if sc["verdict"] == "not_a_community_coin":
        return "meme_score"
    if sc["parts"]["community"] < MEME["min_community_score"]:
        return "no_community"
    return None
