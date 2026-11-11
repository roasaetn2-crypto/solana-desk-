"""DexScreener paid-promotion flag. A flag, never a quality score.

  GET https://api.dexscreener.com/token-profiles/latest/v1   (profile bought / updated)
  GET https://api.dexscreener.com/token-boosts/latest/v1     (boost bought)

Both are keyless, 60 requests a minute. We poll each at most once every
2 minutes (about 1 call a minute in total) and keep the Solana mints in memory.
A token that paid for a profile or boost is "promoted": that says someone paid
for attention, not that the token is good or bad, so it only shows as a chip
and in the analyst's notes. It never changes a score and never blocks.
"""
from __future__ import annotations

import threading
import time

import requests

from desk.netutil import TIMEOUT_S, SlidingLimiter

BASE = "https://api.dexscreener.com"
PATHS = {"profile": "/token-profiles/latest/v1", "boost": "/token-boosts/latest/v1"}
HEADERS = {"Accept": "application/json", "User-Agent": "solana-trading-desk/1.2"}
REFRESH_S = 120
MAX_PER_MIN = 20             # far below DexScreener's 60

_lock = threading.Lock()
_limiter = SlidingLimiter(MAX_PER_MIN, 60)
_sets: dict[str, set] = {"profile": set(), "boost": set()}
_boost_amount: dict[str, float] = {}
_fetched_at: dict[str, float] = {"profile": 0.0, "boost": 0.0}
_ok = {"profile": None, "boost": None}


def reset() -> None:
    with _lock:
        for k in _sets:
            _sets[k] = set()
            _fetched_at[k] = 0.0
            _ok[k] = None
        _boost_amount.clear()
    _limiter.reset()


def _solana_mints(rows) -> list[tuple[str, float]]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("chainId") == "solana" and r.get("tokenAddress"):
            try:
                amt = float(r.get("totalAmount") or r.get("amount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            out.append((r["tokenAddress"], amt))
    return out


def refresh(session: requests.Session | None = None, now: float | None = None) -> None:
    now = time.time() if now is None else now
    from desk import scanner
    if scanner.host_cooling("dexscreener"):
        return                           # shared 429 cooldown: skip quietly
    for kind, path in PATHS.items():
        if now - _fetched_at[kind] < REFRESH_S:
            continue
        if not _limiter.allow(now):
            continue
        _fetched_at[kind] = now          # also throttles retries after a failure
        try:
            r = (session or requests).get(BASE + path, headers=HEADERS, timeout=TIMEOUT_S)
            if r.status_code == 429:
                scanner.note_429("dexscreener", f"promo {path}", r.headers.get("Retry-After"))
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code}")
            rows = _solana_mints(r.json())
            with _lock:
                _sets[kind] = {m for m, _ in rows}
                if kind == "boost":
                    for m, a in rows:
                        _boost_amount[m] = a
                _ok[kind] = True
        except Exception:
            with _lock:
                _ok[kind] = False


def check(mint: str, session: requests.Session | None = None) -> dict:
    """{"promoted": bool|None, "kinds": [...]}. None = unknown (feed unreachable)."""
    if not mint or mint.startswith("DEMO"):
        return {"available": False, "promoted": None, "kinds": [], "flags": []}
    try:
        refresh(session)
    except Exception:
        pass
    with _lock:
        kinds = [k for k in _sets if mint in _sets[k]]
        known = any(v for v in _ok.values())
    if not known and not kinds:
        return {"available": False, "promoted": None, "kinds": [], "flags": []}
    flags = ["paid_promo"] if kinds else []
    return {"available": True, "promoted": bool(kinds), "kinds": kinds, "flags": flags,
            "boost_amount": _boost_amount.get(mint)}
