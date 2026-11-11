"""Market regime tag from free, keyless CoinGecko prices. Description only.

  GET https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,solana&include_24hr_change=true

Idea from tradermonty/claude-trading-skills (crypto-regime-analyzer, MIT):
look at the market's posture before sizing single coins. This is a deliberately
small version: BTC and SOL 24h change only (no funding, breadth or dominance).

  RISK_ON   SOL 24h >= +4% and BTC 24h >= 0%       -> no change
  NEUTRAL   anything in between                    -> no change
  RISK_OFF  SOL 24h <= -5% or BTC 24h <= -3%       -> paper tickets halved

The tag can only make the desk more careful. Refreshed every 10 minutes
(6 calls an hour, far below CoinGecko's free limit); any failure gives UNKNOWN
and no size change.
"""
from __future__ import annotations

import threading
import time

import requests

from desk.netutil import TIMEOUT_S, SlidingLimiter

URL = "https://api.coingecko.com/api/v3/simple/price"
HEADERS = {"Accept": "application/json", "User-Agent": "solana-trading-desk/1.2"}
REFRESH_S = 600
RETRY_S = 120
STALE_S = 3600               # an hour-old reading is treated as unknown

_limiter = SlidingLimiter(5, 60)
_lock = threading.Lock()
_state: dict = {"tag": "UNKNOWN", "sol_24h": None, "btc_24h": None, "at": None,
                "tried": 0.0}


def reset() -> None:
    _limiter.reset()
    with _lock:
        _state.update(tag="UNKNOWN", sol_24h=None, btc_24h=None, at=None, tried=0.0)


def classify(sol_24h: float | None, btc_24h: float | None) -> str:
    """Pure. UNKNOWN unless we have SOL; BTC only adds the RISK_OFF / RISK_ON conditions."""
    if sol_24h is None:
        return "UNKNOWN"
    if sol_24h <= -5.0 or (btc_24h is not None and btc_24h <= -3.0):
        return "RISK_OFF"
    if sol_24h >= 4.0 and (btc_24h is None or btc_24h >= 0.0):
        return "RISK_ON"
    return "NEUTRAL"


def refresh(session: requests.Session | None = None, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        wait = REFRESH_S if _state["at"] else RETRY_S
        if now - _state["tried"] < wait:
            return
        _state["tried"] = now
    if not _limiter.allow(now):
        return
    try:
        r = (session or requests).get(
            URL, params={"ids": "bitcoin,solana", "vs_currencies": "usd",
                         "include_24hr_change": "true"},
            headers=HEADERS, timeout=TIMEOUT_S)
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        body = r.json()
        sol = float(body["solana"]["usd_24h_change"])
        btc = float(body["bitcoin"]["usd_24h_change"])
        with _lock:
            _state.update(sol_24h=round(sol, 2), btc_24h=round(btc, 2), at=now,
                          tag=classify(sol, btc))
    except Exception:
        pass


def current(session: requests.Session | None = None, now: float | None = None) -> dict:
    """The current tag, refreshing if due. Never raises, never blocks long."""
    now = time.time() if now is None else now
    try:
        refresh(session, now)
    except Exception:
        pass
    with _lock:
        st = dict(_state)
    if st["at"] is None or now - st["at"] > STALE_S:
        return {"tag": "UNKNOWN", "sol_24h": None, "btc_24h": None, "halve": False}
    return {"tag": st["tag"], "sol_24h": st["sol_24h"], "btc_24h": st["btc_24h"],
            "halve": st["tag"] == "RISK_OFF"}
