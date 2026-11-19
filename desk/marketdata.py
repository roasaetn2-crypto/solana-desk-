"""Second data source for the PAPER gate finalists, independent of GeckoTerminal.

Why it exists: on Render, GeckoTerminal (and often DexScreener and Jupiter) sit in a 429 cooldown most of
the time, so a coin that passed the scanner died on MISSING data (tx_ratio_unknown, trigger_unknown), not
on a threshold. What is reachable without a key:

  Jupiter tokens v2 rows   stats5m.numBuys / numSells / buyVolume / sellVolume and stats1h volume, plus a
                           price. The scanner already holds these rows (discovery + the PumpPortal
                           hydrate), so most finalists need no request at all. A finalist whose row is
                           stale gets ONE targeted search on its own capped, prioritised lane.
  price tape               Jupiter prices recorded per mint (desk/pricetape.py): price only, no volume.
  PumpPortal trade stream  NOT usable: subscribeTokenTrade needs a PumpPortal API key and a funded wallet
                           (metered per event). The desk does not use paid streams or add keys.
  Birdeye                  needs a key; none is configured.

tx_m5() chains Jupiter then GeckoTerminal. Every miss carries a cause so the skip reason can name it, e.g.
tx_ratio_unknown:jup_cooling+gt_cooling. Missing data is still a skip. PAPER only: the agent never calls
this in live mode.
"""
from __future__ import annotations

import time

from desk import scanner


def ensure_jup(mint: str, session=None, now: float | None = None, fetch: bool = True) -> str | None:
    """None once a fresh Jupiter row for `mint` is cached (after one targeted refresh if needed),
    else the cause it could not be had."""
    now = time.time() if now is None else now
    row, cause = scanner._jup_row_fresh(mint, now)
    if row is not None:
        return None
    if not fetch:
        return cause
    c = scanner.jup_refresh([mint], session, now)
    if c:
        return c
    row, _ = scanner._jup_row_fresh(mint, now)
    return None if row is not None else "jup_unlisted"


def _ds_cause(ds: dict | None, now: float) -> str:
    if not (ds or {}).get("available"):
        return "ds_cooling" if scanner.host_cooling("dexscreener", now) else "ds_no_pair"
    return "ds_zero_txns"


def tx_m5(t: dict, ds: dict | None, session=None, now: float | None = None, fetch: bool = True) -> dict:
    """5m buy / sell counts from the sources after DexScreener: Jupiter, then GeckoTerminal.

    Returns {"available": True, "buys_m5", "sells_m5", "source", "chain"} or
    {"available": False, "cause": "jup_cooling+gt_cooling", "chain": [...]}."""
    now = time.time() if now is None else now
    mint = t.get("mint") or ""
    chain = [f"ds:{_ds_cause(ds, now)}"]
    causes: list[str] = []

    jc = ensure_jup(mint, session, now, fetch)
    if jc is None:
        got, jc = scanner.jup_tx_m5(mint, now)
        if got and (got["buys_m5"] + got["sells_m5"]) > 0:
            return {**got, "chain": chain + ["jup:ok"]}
        jc = jc or "jup_zero_txns"
    chain.append(f"jup:{jc}")
    causes.append(jc)

    gt = scanner.gt_tx_m5(t, session, now) if fetch else None
    if gt and (gt["buys_m5"] + gt["sells_m5"]) > 0:
        return {**gt, "chain": chain + ["gt:ok"]}
    gc = "gt_zero_txns" if gt else scanner.gt_tx_cause(t, now)
    chain.append(f"gt:{gc}")
    causes.append(gc)
    if all(c.endswith("zero_txns") for c in causes):
        cause = "no_5m_txns"
    else:
        cause = "+".join(dict.fromkeys(causes))
    return {"available": False, "cause": cause, "chain": chain}
