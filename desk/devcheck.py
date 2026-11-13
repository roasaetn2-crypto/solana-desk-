"""Dev / creator investigation: is this launch legit, or is the dev the exit?

A memecoin's dev wallet is the single most informative account on the chain.
This seat answers four questions with evidence, not vibes:

  1. Does the dev still hold the float?      (creator balance % of supply)
  2. Has this creator rugged before?         (prior launches / risk flags)
  3. Is the launch bundled or sniped?        (insider / sniper wallet share)
  4. Can the dev still mint or freeze?       (authority, from chain facts)

Primary source is RugCheck's public report (creator + insider graph). If that
is unreachable we degrade to RPC-derived evidence and say so. Nothing here is
guessed: every field carries the source it came from.
"""
from __future__ import annotations

import hashlib
import random

import requests

from config import FOMO, MEME

RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"
HEADERS = {"Accept": "application/json", "User-Agent": "solana-trading-desk/1.0"}


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _demo_report(mint: str) -> dict:
    """Stable synthetic dev report for demo mints."""
    rng = random.Random(int(hashlib.sha256(mint.encode()).hexdigest(), 16))
    dev_pct = round(rng.uniform(0.0, 0.12), 4)
    prev = rng.choice([0, 0, 0, 0, 1, 2])
    sniper = round(rng.uniform(0.0, 0.30), 4)
    flags = []
    if dev_pct > MEME["max_dev_hold_pct"]:
        flags.append("dev holds a large share of supply")
    if prev > MEME["max_creator_prev_rugs"]:
        flags.append(f"creator has {prev} prior dead launch(es)")
    if sniper > MEME["max_sniper_pct"]:
        flags.append("sniper wallets hold a large share")
    return {
        "available": True, "source": "rugcheck-demo",
        "creator": f"DEMOdev{mint[-6:]}",
        "creator_hold_pct": dev_pct,
        "creator_prev_launches": prev + 1,
        "creator_prev_rugs": prev,
        "sniper_pct": sniper,
        "insider_pct": round(min(0.9, sniper + rng.uniform(0, 0.12)), 4),
        "holder_count": rng.randint(40, 2_400),
        "lp_locked": rng.random() < 0.7,
        "mint_renounced": rng.random() < 0.85,
        "freeze_renounced": rng.random() < 0.9,
        "flags": flags,
        "risk_score": rng.randint(0, 100),
    }


# RugCheck knownAccounts types that are not a seller in the usual sense: pools, lockers, vaults.
# CREATOR is deliberately NOT here: a dev wallet holding the float is exactly what we must see.
_NON_HOLDER_TYPES = {"AMM", "LOCKER", "VAULT"}


def _real_holder_count(body: dict) -> int | None:
    """RugCheck totalHolders, when it is a real positive count. 0 / missing = unknown, not zero."""
    n = body.get("totalHolders")
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return None
    n = int(n)
    return n if n > 0 else None


def _pool_accounts(body: dict) -> set[str]:
    """Every address RugCheck tells us is a pool / locker / vault, or a market's own account."""
    out: set[str] = set()
    for addr, info in (body.get("knownAccounts") or {}).items():
        if isinstance(info, dict) and str(info.get("type", "")).upper() in _NON_HOLDER_TYPES:
            out.add(addr)
    for m in body.get("markets") or []:
        if not isinstance(m, dict):
            continue
        for k in ("pubkey", "liquidityA", "liquidityB", "mintLP"):
            v = m.get(k)
            if isinstance(v, str) and v:
                out.add(v)
        for k in ("liquidityAAccount", "liquidityBAccount"):
            v = m.get(k)
            if isinstance(v, dict):
                for kk in ("pubkey", "address", "owner"):
                    if isinstance(v.get(kk), str) and v[kk]:
                        out.add(v[kk])
            elif isinstance(v, str) and v:
                out.add(v)
    return out


def rugcheck_concentration(body: dict) -> dict | None:
    """Top real wallet and top-10 share from RugCheck's topHolders, pools excluded.

    Used only when the RPC could not measure concentration (top_wallet_pct is None), so it can
    only ADD a block, never remove one. Pool / locker / vault / market accounts are excluded the
    same way the RPC path excludes program-owned accounts; the creator wallet is kept.
    """
    top = [h for h in (body.get("topHolders") or []) if isinstance(h, dict)]
    if not top:
        return None
    pools = _pool_accounts(body)
    wallets: dict[str, float] = {}
    excluded = 0.0
    for h in top:
        pct = _f(h.get("pct")) / 100.0
        addr, owner = h.get("address") or "", h.get("owner") or ""
        if addr in pools or owner in pools:
            excluded += pct
            continue
        key = owner or addr
        wallets[key] = wallets.get(key, 0.0) + pct
    ranked = sorted(wallets.values(), reverse=True)
    if not ranked:
        return {"top_wallet_pct": 0.0, "top_10_pct": 0.0, "excluded_pct": round(excluded, 4),
                "wallets_counted": 0, "source": "rugcheck"}
    return {"top_wallet_pct": round(ranked[0], 6), "top_10_pct": round(sum(ranked[:10]), 6),
            "excluded_pct": round(excluded, 4), "wallets_counted": len(ranked), "source": "rugcheck"}


def investigate(mint: str, chain_facts: dict | None = None,
                session: requests.Session | None = None) -> dict:
    """Return a normalised dev report. Always returns a dict, never raises."""
    if mint.startswith("DEMO"):
        return _finish(_demo_report(mint), chain_facts)

    s = session or requests.Session()
    try:
        body = s.get(f"{RUGCHECK}/{mint}/report", headers=HEADERS, timeout=20).json()
        if not isinstance(body, dict) or "error" in body:
            raise ValueError("rugcheck returned no report")
        top = body.get("topHolders") or []
        creator = body.get("creator") or ""
        # topHolders "address" is the token account; the wallet is "owner". Match either, so
        # the creator's share is actually seen (matching only "address" never found it).
        creator_row = next((h for h in top if creator and creator in (h.get("owner"), h.get("address"))),
                           None)
        insiders = [h for h in top if h.get("insider") or h.get("sniper")]
        token = body.get("token") or {}
        rep = {
            "available": True,
            "source": "rugcheck",
            "creator": creator or None,
            "creator_hold_pct": (_f(creator_row.get("pct")) / 100.0) if creator_row else 0.0,
            "creator_prev_launches": None,
            "creator_prev_rugs": None,
            "sniper_pct": round(sum(_f(h.get("pct")) for h in top if h.get("sniper")) / 100.0, 4),
            "insider_pct": round(sum(_f(h.get("pct")) for h in insiders) / 100.0, 4),
            # A real total from RugCheck, never the size of the top-holders sample.
            "holder_count": _real_holder_count(body),
            "holder_sample": len(top),
            "concentration": rugcheck_concentration(body),
            "lp_locked": any("lp" in str(r.get("name", "")).lower() and r.get("value")
                             for r in (body.get("risks") or [])),
            "mint_renounced": token.get("mintAuthority") in (None, ""),
            "freeze_renounced": token.get("freezeAuthority") in (None, ""),
            "flags": [r.get("name") for r in (body.get("risks") or []) if r.get("name")],
            "risk_score": body.get("score_normalised", body.get("score")),
        }
        return _finish(rep, chain_facts)
    except Exception:
        pass

    # ---- degraded path: RPC evidence only ----
    cf = chain_facts or {}
    rep = {
        "available": bool(cf),
        "source": "rpc-only",
        "creator": None,
        "creator_hold_pct": None,
        "creator_prev_launches": None,
        "creator_prev_rugs": None,
        "sniper_pct": None,
        "insider_pct": None,
        "holder_count": cf.get("holder_count"),     # a real count or None; never the top-20 sample
        "lp_locked": None,
        "mint_renounced": cf.get("mint_authority") is None,
        "freeze_renounced": cf.get("freeze_authority") is None,
        "flags": ["dev report unavailable: no creator graph, only holder concentration"],
        "risk_score": None,
        "top_wallet_pct": cf.get("top_wallet_pct"),
    }
    return _finish(rep, chain_facts)


def _finish(rep: dict, chain_facts: dict | None) -> dict:
    """Attach a 0-100 dev-legitimacy score and the hard verdict."""
    cf = chain_facts or {}
    score = 50.0
    reasons: list[str] = []

    hold = rep.get("creator_hold_pct")
    if hold is not None:
        if hold <= 0.01:
            score += 22; reasons.append("dev holds almost nothing")
        elif hold <= MEME["max_dev_hold_pct"]:
            score += 12; reasons.append(f"dev holds {hold:.1%}")
        else:
            score -= 28; reasons.append(f"dev holds {hold:.1%} of supply")

    rugs = rep.get("creator_prev_rugs")
    if rugs is not None:
        if rugs <= MEME["max_creator_prev_rugs"]:
            score += 12; reasons.append("no prior rug on this creator")
        else:
            score -= 40; reasons.append(f"creator has {rugs} prior rug(s)")

    sniper = rep.get("sniper_pct")
    if sniper is not None:
        if sniper <= 0.05:
            score += 10; reasons.append("no meaningful sniper bundle")
        elif sniper <= MEME["max_sniper_pct"]:
            score += 3; reasons.append(f"sniper share {sniper:.1%}")
        else:
            score -= 22; reasons.append(f"sniper share {sniper:.1%}")

    if rep.get("mint_renounced") or cf.get("mint_authority") is None:
        score += 8; reasons.append("mint authority renounced")
    else:
        score -= 25; reasons.append("mint authority still live")
    if rep.get("freeze_renounced") or cf.get("freeze_authority") is None:
        score += 6; reasons.append("freeze authority renounced")
    else:
        score -= 25; reasons.append("freeze authority still live")

    if rep.get("lp_locked") is True:
        score += 6; reasons.append("LP locked")

    score = max(0, min(100, round(score)))

    hard_no = []
    if hold is not None and hold > MEME["max_dev_hold_pct"]:
        hard_no.append("dev_hold")
    if rugs is not None and rugs > MEME["max_creator_prev_rugs"]:
        hard_no.append("creator_rug_history")
    if sniper is not None and sniper > MEME["max_sniper_pct"]:
        hard_no.append("sniper_bundle")
    if cf.get("mint_authority") and not rep.get("mint_renounced"):
        hard_no.append("mint_authority")

    rep["dev_score"] = score
    rep["reasons"] = reasons
    rep["hard_no"] = hard_no
    rep["legit"] = score >= 55 and not hard_no
    return rep


def dev_kill(rep: dict) -> str | None:
    """Hard evidence that the dev is the exit. First failure wins."""
    return rep["hard_no"][0] if rep.get("hard_no") else None


def apply_fomo(rep: dict, ev: dict | None) -> dict:
    """Fold the FOMO evidence seat into a finished dev report, in place.

    Kept separate from investigate() on purpose: investigate() talks to RugCheck
    and must keep working when FOMO is off, unaffordable, or down. This only
    reads a dict that fomo.token_evidence() already produced, so it cannot
    block, cannot spend credits, and cannot raise.

    FOMO is allowed to add a hard veto (the coin page says you cannot sell, or
    the deployer is dumping). It is never allowed to clear one.
    """
    if not ev:
        return rep
    rep["fomo"] = {
        "available": bool(ev.get("available")),
        "severe": bool(ev.get("severe")),
        "warnings": [w.get("type") for w in (ev.get("warnings") or []) if w.get("type")],
        "dev_pnl_usd": ev.get("dev_pnl_usd"),
        "why": list(ev.get("why") or []),
    }
    if not ev.get("available"):
        return rep

    from desk import fomo as fomo_mod

    reasons = rep.setdefault("reasons", [])
    hard_no = rep.setdefault("hard_no", [])

    k = fomo_mod.kill_reason(ev)
    if k and k not in hard_no:
        hard_no.append(k)
        reasons.append(f"FOMO says the exit is closed ({k})")

    # The deployer selling into his own launch is the classic rug tell.
    dpnl = ev.get("dev_pnl_usd") or 0.0
    if dpnl <= -500:
        reasons.append(f"FOMO: deployer realised ${dpnl:,.0f} (dumping)")
        if "fomo_dev_dumping" not in hard_no:
            hard_no.append("fomo_dev_dumping")
    elif dpnl > 0:
        reasons.append(f"FOMO: deployer up ${dpnl:,.0f}")

    th = ev.get("thesis") or {}
    if th.get("available") and th.get("score") is not None:
        reasons.append(f"FOMO theses {th['score']}/100 "
                       f"({th.get('confidence')}, n={th.get('count')})")
        if (th["score"] <= FOMO["bearish_kill_score"]
                and th.get("confidence") in FOMO["bearish_kill_confidence"]):
            if "fomo_thesis_bearish" not in hard_no:
                hard_no.append("fomo_thesis_bearish")
            reasons.append("FOMO crowd is confidently bearish")

    rep["legit"] = rep.get("dev_score", 0) >= 55 and not hard_no
    return rep

