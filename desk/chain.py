"""Seat 2: chain facts. Straight from a Solana RPC node, no model involved.

Facts kill before judgements do: an open mint authority is not an opinion,
so it never reaches the analyst.
"""
from __future__ import annotations

import hashlib
import os
import random
import time

import requests

from config import CHAIN
from desk import helius

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


def rpc_url() -> str:
    """Helius if a key is set, else SOLANA_RPC_URL, else the public node.

    Resolved per call, not at import, so the desk can be pointed at a better
    node without a restart and so a missing key is never a crash.
    """
    return helius.rpc_url() or os.getenv("SOLANA_RPC_URL", PUBLIC_RPC)


def rpc_source() -> str:
    return "helius" if helius.configured() else (
        "custom" if os.getenv("SOLANA_RPC_URL") else "public")


def rpc(method: str, params: list, session: requests.Session | None = None):
    """One JSON-RPC call, with backoff.

    The free public node allows only a handful of requests per second and
    answers 429 under any real load. A single 429 used to abort the whole
    token, which is why nothing ever reached the analyst. Retry instead.
    """
    s = session or requests.Session()
    last = None
    for attempt in range(4):
        r = s.post(rpc_url(),
                   json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                   timeout=20)
        if r.status_code == 429:
            last = r
            time.sleep(1.5 * (attempt + 1))   # 1.5s, 3s, 4.5s
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]
    if last is not None:
        last.raise_for_status()
    raise RuntimeError(f"{method}: retries exhausted")


def _demo_facts(mint: str) -> dict:
    """Synthetic but stable facts for demo mints (hash-seeded, so a mint keeps its shape)."""
    h = int(hashlib.sha256(mint.encode()).hexdigest(), 16)
    rng = random.Random(h)
    open_mint = rng.random() < 0.12
    open_freeze = rng.random() < 0.08
    top1 = round(rng.uniform(0.01, 0.14), 4)
    top10 = round(min(0.95, top1 + rng.uniform(0.05, 0.42)), 4)
    return {
        "mint_authority": "DemoMintAuth111" if open_mint else None,
        "freeze_authority": "DemoFreezeAuth111" if open_freeze else None,
        "supply": rng.uniform(1e6, 1e9),
        "top_wallet_pct": top1,
        "top_10_pct": top10,
        "program_held_pct": round(rng.uniform(0.1, 0.75), 4),
        "wallets_counted": rng.randint(6, 20),
        "holder_count": rng.randint(40, 900),
        "source": "demo",
    }


def facts(mint: str, session: requests.Session | None = None) -> dict:
    """Mint/freeze authority, supply, and holder concentration of REAL wallets.

    The bonding curve and the AMM pool hold huge balances by design. Their token
    accounts belong to program-owned addresses, not to wallets, so we look up who
    owns each top account and drop anything not owned by the System Program.
    """
    if mint.startswith("DEMO"):
        return _demo_facts(mint)

    info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], session)
    if not info or not info.get("value"):
        raise RuntimeError(f"{mint}: account not found")
    parsed = info["value"]["data"]["parsed"]["info"]
    decimals = int(parsed["decimals"])
    supply = int(parsed["supply"]) / 10 ** decimals

    base = {
        "mint_authority": parsed.get("mintAuthority"),
        "freeze_authority": parsed.get("freezeAuthority"),
        "supply": supply,
        "top_wallet_pct": None,
        "top_10_pct": None,
        "program_held_pct": None,
        "wallets_counted": 0,
        "holder_count": None,
        "source": rpc_source(),
        "partial": True,          # holder distribution not available
    }

    # getTokenLargestAccounts is the expensive call. The free public node
    # throttles it to 429 almost always, and a single 429 used to throw away
    # the whole token even though the authority checks above had succeeded.
    # Fall back to authority-only facts and mark them partial.
    try:
        largest = rpc("getTokenLargestAccounts", [mint], session)["value"]
    except Exception:
        # The public RPC throttled us. That used to leave top_wallet_pct = None,
        # and chain_kill treats None as "no evidence" and lets the token through
        # -- so the concentration gate silently never ran. RugCheck publishes the
        # same top-holder percentages for free, so ask it instead of trading blind.
        return _rugcheck_distribution(mint, base, session)

    largest = largest[: CHAIN["largest_accounts_checked"]]
    token_accts = [x["address"] for x in largest]

    # token account -> its owner address
    try:
        accts = rpc("getMultipleAccounts", [token_accts, {"encoding": "jsonParsed"}],
                    session)["value"]
    except Exception:
        return base

    owners = [((a or {}).get("data", {}).get("parsed", {}).get("info", {}) or {}).get("owner")
              for a in accts]

    # owner address -> which program owns it (System Program = a normal wallet)
    uniq = [o for o in dict.fromkeys(owners) if o]
    try:
        owner_accts = rpc("getMultipleAccounts", [uniq, {"encoding": "base64"}],
                          session)["value"] if uniq else []
    except Exception:
        return base
    program_of = {o: (acc or {}).get("owner") for o, acc in zip(uniq, owner_accts)}

    wallets: dict[str, float] = {}
    excluded = 0.0
    for row, owner in zip(largest, owners):
        amount = float(row.get("uiAmount") or 0)
        prog = program_of.get(owner)
        if owner and prog not in (None, SYSTEM_PROGRAM):
            excluded += amount            # curve, pool, vault: not a seller in the usual sense
            continue
        wallets[owner or row["address"]] = wallets.get(owner or row["address"], 0) + amount

    ranked = sorted(wallets.values(), reverse=True)
    share = lambda xs: (sum(xs) / supply) if supply else None

    # The public RPC only exposes the top 20 accounts. With Helius we can ask
    # how many accounts actually hold a balance, which is the number the
    # community score means by "holders".
    holders = helius.holder_count(mint, session) if helius.configured() else None

    return {
        "mint_authority": parsed.get("mintAuthority"),
        "freeze_authority": parsed.get("freezeAuthority"),
        "supply": supply,
        "top_wallet_pct": share(ranked[:1]),
        "top_10_pct": share(ranked[:10]),
        "program_held_pct": share([excluded]),
        "wallets_counted": len(ranked),
        "holder_count": holders,
        "source": rpc_source(),
        "partial": False,
    }


RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"


# RugCheck labels every account it recognises (AMM pools, lockers, the creator).
# Its `topHolders` list is the raw token-account list, so the pool's own token
# vault shows up as the biggest "holder" -- a brand new token often reads as
# 80-99% top wallet when the truth is "the liquidity is sitting in the pool".
# Counting that as a whale rejected perfectly good tokens. AMM and LOCKER
# balances are not sell pressure, so they are excluded from concentration;
# CREATOR is kept, because a dev bag is real risk.
_NOT_SELL_PRESSURE = {"AMM", "LOCKER", "LOCKER_STAKE", "VAULT"}


def _rugcheck_distribution(mint: str, base: dict, session=None) -> dict:
    """Top-holder concentration from RugCheck, for when the RPC will not answer."""
    s = session or requests.Session()
    try:
        body = s.get(f"{RUGCHECK}/{mint}/report", headers={"Accept": "application/json"},
                     timeout=15).json()
    except Exception:
        return base
    known = body.get("knownAccounts") or {}
    # Also collect the market/pool account addresses directly, in case a pool is
    # missing from `knownAccounts`.
    pool_addrs = set()
    for m in body.get("markets") or []:
        for k in ("pubkey", "mintLP", "liquidityA", "liquidityB",
                  "mintAAccount", "mintBAccount", "mintLPAccount",
                  "liquidityAAccount", "liquidityBAccount"):
            v = m.get(k)
            if isinstance(v, str) and v:
                pool_addrs.add(v)

    pcts, excluded = [], 0
    for h in body.get("topHolders") or []:
        addr = h.get("address")
        owner = h.get("owner")
        meta = known.get(addr) or known.get(owner) or {}
        if (meta.get("type") in _NOT_SELL_PRESSURE
                or addr in pool_addrs or owner in pool_addrs):
            excluded += 1
            continue
        try:
            pcts.append(float(h.get("pct") or 0) / 100.0)
        except (TypeError, ValueError):
            continue
    if not pcts:
        return base
    pcts.sort(reverse=True)
    return {
        **base,
        "top_wallet_pct": round(pcts[0], 4),
        "top_10_pct": round(sum(pcts[:10]), 4),
        "wallets_counted": len(pcts),
        "holder_count": body.get("totalHolders") or None,
        "pool_accounts_excluded": excluded,
        "source": base.get("source", "public") + "+rugcheck",
        "partial": False,
    }


def chain_kill(f: dict) -> str | None:
    """Plain facts against plain limits. First failure wins."""
    if f["mint_authority"] and not CHAIN["allow_mint_authority"]:
        return "mint_authority"
    if f["freeze_authority"] and not CHAIN["allow_freeze_authority"]:
        return "freeze_authority"
    if f["top_wallet_pct"] is not None and f["top_wallet_pct"] > CHAIN["max_top_wallet"]:
        return "top_wallet"
    if f["top_10_pct"] is not None and f["top_10_pct"] > CHAIN["max_top_10"]:
        return "top_10"
    return None
    
