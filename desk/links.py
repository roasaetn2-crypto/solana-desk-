"""All FOMO URLs go through here. Never drop r=romsq.

Links are view-only. The desk never places orders on fomo.family and never
logs in; Romz taps Buy himself. Never shorten these URLs.
"""
from __future__ import annotations

import logging

log = logging.getLogger("desk.links")

CHAIN_ID = {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}
SIGNUP_URL = "https://fomo.family/r/romsq"


def coin_url(address: str, chain: str = "solana") -> str:
    if chain not in CHAIN_ID:
        log.warning("unknown chain %r for coin_url; returning SIGNUP_URL", chain)
        return SIGNUP_URL
    return (
        f"https://fomo.family/coin?address={address}"
        f"&chainId={CHAIN_ID[chain]}&r=romsq&source=share_link"
    )


def profile_url(handle: str) -> str:
    h = handle.lstrip("@")
    return f"https://fomo.family/profile/{h}?source=share_link&r=romsq"


def with_url(obj: dict, chain: str = "solana") -> dict:
    """Copy a mint-bearing record and attach the FOMO coin URL."""
    out = dict(obj)
    mint = out.get("mint")
    if mint:
        out["fomo_url"] = coin_url(mint, chain)
    return out
