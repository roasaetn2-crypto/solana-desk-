"""Romz's scalping rulebook: PAPER-only entry gates and fast exits.

Every function here is pure (no I/O) apart from the small volume history, so
the agent can call it cheaply and the tests can pin each rule.

Entry gates return None (pass) or a short skip reason. Missing or unknown data
is ALWAYS a skip, never a pass:

  tx_ratio_low / tx_ratio_unknown   5m buys / sells must be > min_buy_sell_ratio
  lp_unlocked  / lp_unknown         LP burned or locked (RugCheck lpLockedPct)
  impact_high  / impact_unknown     Jupiter buy impact for the actual ticket
  vol_falling  / vol_unknown        5m volume above the previous 5m
  no_chase     / chase_unknown      current 5m candle already up > max_candle_up

Fast exits return a reason string (sell all) or None. Missing data means no
forced exit; the caller logs it.

Live trading never reads any of this: the agent only calls it in paper mode.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

from config import RULEBOOK

BAR_S = 300                     # 5m
PREV_MIN_S, PREV_MAX_S = 240, 480   # a volume sample this old counts as "the previous 5m"
_MAX_MINTS = 3000

_lock = threading.Lock()
_vol: "OrderedDict[str, deque]" = OrderedDict()      # mint -> deque[(ts, volume_m5, source)]


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def reset() -> None:
    with _lock:
        _vol.clear()


# ---------- 1. 5m buy / sell transaction ratio ----------
def tx_ratio_gate(ds: dict | None) -> str | None:
    if not RULEBOOK.get("tx_ratio_gate", True):
        return None
    ds = ds or {}
    if not ds.get("available"):
        return "tx_ratio_unknown"
    b, s = _num(ds.get("buys_m5")), _num(ds.get("sells_m5"))
    if b is None or s is None or (b + s) <= 0:
        return "tx_ratio_unknown"
    if s == 0:
        return None                 # buys only: the ratio is unbounded
    return None if b / s > float(RULEBOOK["min_buy_sell_ratio"]) else "tx_ratio_low"


# ---------- 2. LP burned or locked ----------
def lp_gate(dev: dict | None) -> str | None:
    if not RULEBOOK.get("lp_gate", True):
        return None
    pct = _num((dev or {}).get("lp_locked_pct"))
    if pct is None:
        return "lp_unknown"
    return None if pct >= float(RULEBOOK["min_lp_locked_pct"]) else "lp_unlocked"


# ---------- 3. Jupiter price impact for the actual ticket ----------
def impact_gate(jup: dict | None, quoted_ticket, ticket) -> str | None:
    """`jup` is the Jupiter quote taken for `quoted_ticket`. Impact grows with size,
    so a quote for a ticket at least as large as the real one is an upper bound."""
    if not RULEBOOK.get("impact_gate", True):
        return None
    jup = jup or {}
    qt, tk = _num(quoted_ticket), _num(ticket)
    if not jup.get("available") or not jup.get("route") or qt is None or tk is None or qt + 1e-9 < tk:
        return "impact_unknown"
    imp = _num(jup.get("impact"))
    if imp is None:
        return "impact_unknown"
    return None if abs(imp) < float(RULEBOOK["max_price_impact"]) else "impact_high"


# ---------- 4. 5m volume rising ----------
def record_volume(tokens, now: float | None = None) -> None:
    """Remember every scanned token's rolling 5m volume, once per cycle (no HTTP)."""
    now = time.time() if now is None else now
    with _lock:
        for t in tokens or []:
            m, v = t.get("mint"), _num(t.get("volume_m5"))
            if not m or v is None:
                continue
            dq = _vol.get(m)
            if dq is None:
                dq = _vol[m] = deque(maxlen=24)
            else:
                _vol.move_to_end(m)
            dq.append((now, v, t.get("source")))
        while len(_vol) > _MAX_MINTS:
            _vol.popitem(last=False)


def _prev_sample(mint: str, source, now: float):
    with _lock:
        rows = list(_vol.get(mint) or ())
    best = None
    for ts, v, src in rows:
        age = now - ts
        if PREV_MIN_S <= age <= PREV_MAX_S and src == source:
            if best is None or abs(age - BAR_S) < abs(now - best[0] - BAR_S):
                best = (ts, v)
    return best


def _closed_bars(candles, now: float) -> list[dict]:
    out = []
    for c in candles or []:
        if not isinstance(c, dict):
            continue
        ts = _num(c.get("t"))
        if ts and ts > 1e12:
            ts /= 1000.0
        if ts and ts + BAR_S <= now:
            out.append({**c, "t": ts})
    return out


def volume_state(t: dict, candles=None, now: float | None = None) -> dict:
    """{'cur', 'prev', 'basis'}: rolling 5m volume now vs ~5 min ago (own scan history,
    same source), else the last two CLOSED 5m candles. Missing -> cur/prev None."""
    now = time.time() if now is None else now
    cur = _num(t.get("volume_m5"))
    prev = _prev_sample(t.get("mint") or "", t.get("source"), now)
    if cur is not None and prev is not None:
        return {"cur": cur, "prev": prev[1], "basis": "scan"}
    bars = _closed_bars(candles, now)
    if len(bars) >= 2 and now - (bars[-1]["t"] + BAR_S) <= 2 * BAR_S:
        a, b = _num(bars[-1].get("v")), _num(bars[-2].get("v"))
        if a is not None and b is not None:
            return {"cur": a, "prev": b, "basis": "candles"}
    return {"cur": cur, "prev": None, "basis": None}


def volume_gate(t: dict, candles=None, now: float | None = None) -> str | None:
    if not RULEBOOK.get("volume_rising_gate", True):
        return None
    st = volume_state(t, candles, now)
    if st["cur"] is None or st["prev"] is None:
        return "vol_unknown"
    return None if st["cur"] > st["prev"] else "vol_falling"


# ---------- 5. no-chase ----------
def candle_up(candles, now: float | None = None, max_stale_s: float = 600.0):
    """(close / open - 1) of the latest 5m bar if it is fresh, else None."""
    now = time.time() if now is None else now
    if not candles:
        return None
    c = candles[-1] if isinstance(candles[-1], dict) else {}
    ts = _num(c.get("t"))
    if ts and ts > 1e12:
        ts /= 1000.0
    o, cl = _num(c.get("o", c.get("open"))), _num(c.get("c", c.get("close")))
    if not ts or now - ts > max_stale_s or not o or o <= 0 or cl is None:
        return None
    return cl / o - 1.0


def chase_gate(candles, t: dict | None = None, ds: dict | None = None,
               now: float | None = None) -> str | None:
    """Latest fresh 5m candle first; else the rolling 5m price change (DexScreener / scan)."""
    if not RULEBOOK.get("no_chase_gate", True):
        return None
    up = candle_up(candles, now)
    if up is None:
        for src in (ds or {}, t or {}):
            ch = _num(src.get("change_m5"))
            if ch is not None:
                up = ch / 100.0
                break
    if up is None:
        return "chase_unknown"
    return "no_chase" if up > float(RULEBOOK["max_candle_up"]) else None


# ---------- 6. fast exits ----------
def entry_liq_for(pos: dict, basis: str):
    """The entry liquidity read on `basis` (dexscreener / jupiter / ...), or None. Positions opened before
    the sources were recorded have none: their entry number can't be compared with anything."""
    pos = pos or {}
    by = pos.get("entry_liqs")
    if isinstance(by, dict) and by.get(basis) is not None:
        return _num(by.get(basis))
    if pos.get("entry_liq_src") == basis:
        return _num(pos.get("entry_liq"))
    return None


def liq_check(pos: dict, quote: dict | None) -> dict:
    """One tick of the liquidity-drop exit, like for like.

    status: "off" | "missing" | "mismatch" | "ok" | "breach" (under the threshold, not yet confirmed)
    | "exit". Only an entry read and an exit read from the SAME source are compared: DexScreener's
    liquidity.usd and Jupiter's `liquidity` are different numbers (Jupiter's is about half on pump pools),
    so a cross-source "drop" is an artefact. A breach needs LIQ_EXIT["confirm_reads"] distinct reads in a
    row; a drop of LIQ_EXIT["instant_drop"] or more on the same source exits on one. Never exits on missing data."""
    from config import LIQ_EXIT
    out = {"status": "off", "reason": None, "note": None}
    if not RULEBOOK.get("exit_on_liq_drop", True):
        return out
    pos = pos if pos is not None else {}
    q = quote or {}
    now = _num(q.get("liquidity_usd"))
    now_src = q.get("liq_source") or "unknown"
    entry_any = _num(pos.get("entry_liq"))
    out.update({"now": now, "now_src": now_src, "entry_src": pos.get("entry_liq_src") or "unknown"})
    if not entry_any or entry_any <= 0:
        out.update({"status": "missing", "note": "no entry liquidity recorded"})
        return out
    if now is None:
        out.update({"status": "missing", "note": "no current liquidity"})
        return out
    entry = entry_liq_for(pos, now_src) if now_src != "unknown" else None
    if not entry or entry <= 0:
        out.update({"status": "mismatch", "entry": entry_any,
                    "note": (f"liquidity sources differ (entry {entry_any:,.0f} [{out['entry_src']}] vs now "
                             f"{now:,.0f} [{now_src}]): drop check skipped")})
        return out
    drop = round(1.0 - now / entry, 6)
    out.update({"entry": entry, "entry_src": now_src, "drop": drop})
    if drop < float(RULEBOOK["liq_drop_exit"]):
        pos.pop("liq_breach", None)
        out["status"] = "ok"
        return out
    st = pos.get("liq_breach") if isinstance(pos.get("liq_breach"), dict) else {"n": 0, "at": None}
    at = _num(q.get("at"))
    if at is None or st.get("at") is None or at > float(st["at"]):      # a new read, not the cached one again
        st = {"n": int(st.get("n") or 0) + 1, "at": at if at is not None else time.time()}
    pos["liq_breach"] = st
    out["reads"] = st["n"]
    if drop >= float(LIQ_EXIT["instant_drop"]) or st["n"] >= int(LIQ_EXIT["confirm_reads"]):
        out.update({"status": "exit", "reason": f"rulebook: liquidity -{drop:.0%} since entry"})
    else:
        out["status"] = "breach"
    return out


def liq_exit(pos: dict, quote: dict | None) -> tuple[str | None, str | None]:
    """(sell reason, missing-data note). Never forces an exit on missing data."""
    r = liq_check(pos, quote)
    return r["reason"], (r["note"] if r["status"] in ("missing", "mismatch") else None)


def holder_exit(pos: dict, snap: dict | None) -> tuple[str | None, str | None]:
    """Dev or top-10 wallets sold since entry, from a fresh RugCheck holder snapshot."""
    if not RULEBOOK.get("exit_on_holder_sell", True):
        return None, None
    pos = pos or {}
    if not snap or not snap.get("available"):
        return None, "holder data unavailable"
    notes = []
    d0, d1 = _num(pos.get("entry_dev_hold")), _num(snap.get("creator_hold_pct"))
    if d0 is not None and d1 is not None and d0 > 0.001:
        sold = round(1.0 - d1 / d0, 6)
        if sold >= float(RULEBOOK["dev_sell_exit"]):
            return f"rulebook: dev sold {sold:.0%} of his bag", None
    elif d0 is None or d1 is None:
        notes.append("dev hold unknown")
    t0, t1 = _num(pos.get("entry_top10")), _num(snap.get("top_10_pct"))
    if t0 is not None and t1 is not None:
        if round(t0 - t1, 6) >= float(RULEBOOK["top10_drop_exit"]):
            return f"rulebook: top-10 wallets sold ({t0:.0%} -> {t1:.0%} of supply)", None
    else:
        notes.append("top-10 share unknown")
    return None, ("; ".join(notes) or None)
