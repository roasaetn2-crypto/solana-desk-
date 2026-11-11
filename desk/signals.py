"""One place that gathers the optional free signals for a token and turns them
into chips, blocks and size cuts. Every source fails open to "unknown".

Sources: GoPlus (token-2022 traps), GMGN (read-only, needs a key), PumpPortal
(launch facts), DexScreener (paid promotion), Jupiter (price impact, quotes).
Chips have a severity:  block  (paper entry refused),  halve  (paper ticket halved),
info  (shown only). Nothing here can loosen anything.
"""
from __future__ import annotations

import requests

from desk import goplus, gmgn, jupiter, promo, pumpportal
from desk.netutil import offline

LABEL = {
    "non_transferable": "non-transferable", "default_frozen": "frozen by default",
    "transfer_hook": "transfer hook", "balance_mutable": "balances editable",
    "closable": "closable", "malicious_creator": "bad creator",
    "transfer_fee": "transfer fee", "gmgn_rug_ratio": "GMGN rug risk",
    "gmgn_honeypot": "honeypot", "gmgn_wash": "wash trading", "gmgn_bundler": "bundlers",
    "gmgn_insider": "insiders", "gmgn_fresh": "fresh wallets", "gmgn_sniper": "snipers",
    "gmgn_bots": "bot wallets", "dev_buy_big": "dev bought big",
    "serial_launcher": "serial launcher", "paid_promo": "paid promo",
    "regime_risk_off": "risk-off market", "breaker_reduce": "breaker: half size",
    "jup_impact_high": "high price impact", "jup_no_route": "no Jupiter route",
}


def chip(flag: str, sev: str, source: str) -> dict:
    return {"t": flag, "label": LABEL.get(flag, flag), "sev": sev, "src": source}


def security(mint: str, session: requests.Session | None = None) -> dict:
    """Run the per-token lookups. {"chips": [...], "block": [...], "halve": [...], "unknown": [...]}."""
    out = {"chips": [], "block": [], "halve": [], "unknown": [], "parts": {}}
    if offline() or not mint or mint.startswith("DEMO"):
        return out
    for name, fn in (("goplus", goplus.check), ("gmgn", gmgn.check),
                     ("pumpportal", lambda m, s: pumpportal.lookup(m)),
                     ("dexscreener", promo.check)):
        try:
            r = fn(mint, session)
        except Exception:
            r = {"available": False, "flags": [], "block": [], "halve": []}
        out["parts"][name] = {k: r.get(k) for k in ("available", "flags", "why")}
        if not r.get("available"):
            out["unknown"].append(name)
        for f in r.get("block") or []:
            out["block"].append(f)
            out["chips"].append(chip(f, "block", name))
        for f in r.get("halve") or []:
            out["halve"].append(f)
            out["chips"].append(chip(f, "halve", name))
        for f in r.get("flags") or []:
            if f not in (r.get("block") or []) and f not in (r.get("halve") or []):
                out["chips"].append(chip(f, "info", name))
    return out


def entry_impact(mint: str, ticket_usd: float, session=None) -> dict:
    """Jupiter buy-side impact for the ticket. Same shape as a part of security()."""
    if offline():
        return {"available": False, "flags": [], "block": [], "halve": []}
    try:
        return jupiter.entry_impact(mint, ticket_usd, session)
    except Exception:
        return {"available": False, "flags": [], "block": [], "halve": []}


def impact_chips(r: dict) -> list[dict]:
    chips = []
    for f in r.get("halve") or []:
        chips.append(chip(f, "halve", "jupiter"))
    for f in r.get("flags") or []:
        if f not in (r.get("halve") or []):
            chips.append(chip(f, "info", "jupiter"))
    return chips


def exit_impact(mint: str, qty: float, decimals, session=None) -> dict:
    """Jupiter sell-side impact for an exit. Advisory: it never delays or blocks the exit."""
    if offline():
        return {"available": False, "flags": [], "block": [], "halve": []}
    try:
        return jupiter.exit_impact(mint, qty, decimals, session)
    except Exception:
        return {"available": False, "flags": [], "block": [], "halve": []}
