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

import logging
import time

from collections import OrderedDict

from config import LAST_GOOD
from desk import chart, dip_recovery, pricetape, scanner

log = logging.getLogger("desk.marketdata")


# ---------- last-good reuse for finalists ----------
# While every source is cooling (429s every cycle), a finalist's last good 5m counts and last good bars
# are reused for LAST_GOOD[...] seconds. They are marked source "cache" with their age so nothing reads
# them as a fresh sample. Bounded, in memory, paper only (the agent never calls this in live mode).
_LAST_TX: "OrderedDict[str, dict]" = OrderedDict()
_LAST_BARS: "OrderedDict[str, dict]" = OrderedDict()
_LAST_MAX = 600


def reset() -> None:
    _LAST_TX.clear()
    _LAST_BARS.clear()


def _keep_last(store: OrderedDict, mint: str, row: dict) -> None:
    store[mint] = row
    store.move_to_end(mint)
    while len(store) > _LAST_MAX:
        store.popitem(last=False)


def remember_tx(mint: str, buys, sells, source: str, now: float | None = None) -> None:
    """Record 5m counts that came from a real read (DexScreener, Jupiter, GeckoTerminal)."""
    try:
        b, s_ = int(buys), int(sells)
    except (TypeError, ValueError):
        return
    if mint and b + s_ > 0 and source != "cache":
        _keep_last(_LAST_TX, mint, {"at": time.time() if now is None else now, "buys_m5": b,
                                    "sells_m5": s_, "source": source})


def last_tx(mint: str, now: float | None = None) -> dict | None:
    """The last good 5m counts for `mint` if younger than LAST_GOOD["tx_m5_max_age_s"], tagged cache."""
    now = time.time() if now is None else now
    row = _LAST_TX.get(mint)
    age = None if row is None else now - row["at"]
    if row is None or age < 0 or age > float(LAST_GOOD.get("tx_m5_max_age_s", 0)):
        return None
    return {"available": True, "source": "cache", "via": row["source"], "age_s": round(age, 1),
            "buys_m5": row["buys_m5"], "sells_m5": row["sells_m5"]}


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
            remember_tx(mint, got["buys_m5"], got["sells_m5"], "jupiter", now)
            return {**got, "chain": chain + ["jup:ok"]}
        jc = jc or "jup_zero_txns"
    chain.append(f"jup:{jc}")
    causes.append(jc)

    gt = scanner.gt_tx_m5(t, session, now) if fetch else None
    if gt and (gt["buys_m5"] + gt["sells_m5"]) > 0:
        remember_tx(mint, gt["buys_m5"], gt["sells_m5"], "geckoterminal", now)
        return {**gt, "chain": chain + ["gt:ok"]}
    gc = "gt_zero_txns" if gt else scanner.gt_tx_cause(t, now)
    chain.append(f"gt:{gc}")
    causes.append(gc)
    old = last_tx(mint, now)                       # every source missed: the last good counts, if recent
    if old:
        return {**old, "chain": chain + [f"cache:age_{old['age_s']:.0f}s"]}
    if all(c.endswith("zero_txns") for c in causes):
        cause = "no_5m_txns"
    else:
        cause = "+".join(dict.fromkeys(causes))
    return {"available": False, "cause": cause, "chain": chain}


# ---------- candles for the scalp-entry rule and the confirm checklist ----------
BAR_STALE_S = {"1m": 240.0, "5m": 600.0}     # newest bar older than this is stale (same as the rules use)
GT_MAX_AGE_S = 300.0                         # GeckoTerminal candles fetched longer ago than this are stale
TAPE_MAX_AGE_S = 150.0                       # the newest tape price must be this fresh
MIN_TAPE_MINUTES = 3                         # real minutes of tape before it counts as candles


def bar_t(c):
    try:
        t = float(c.get("t", c.get("ts")))
    except (AttributeError, TypeError, ValueError):
        return None
    return t / 1000.0 if t > 1e12 else t


def fresh(candles, tf: str, now: float) -> bool:
    """True when the newest bar is recent enough for the rules that read `tf` candles."""
    if not candles:
        return False
    t = bar_t(candles[-1])
    return t is None or now - t <= BAR_STALE_S.get(tf, 600.0)


def aggregate(candles, bar_s: int) -> list[dict]:
    """1m bars -> bars of `bar_s` seconds (open of the first, high/low across, close of the last,
    volume summed). The newest bar is the candle still forming."""
    out: dict[int, dict] = {}
    for c in sorted((c for c in candles or [] if bar_t(c) is not None), key=bar_t):
        k = int(bar_t(c) // bar_s) * bar_s
        b = out.get(k)
        if b is None:
            out[k] = {"t": k, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c.get("v") or 0.0}
        else:
            b["h"], b["l"], b["c"] = max(b["h"], c["h"]), min(b["l"], c["l"]), c["c"]
            b["v"] += c.get("v") or 0.0
    return [out[k] for k in sorted(out)]


def _tape_1m(mint: str, now: float):
    """Price-only 1m bars (silent minutes carried flat) from the price tape plus the desk's own quote
    ticks. (bars, real minutes, ticks in the newest 5m bucket, None) or (None, n, 0, cause)."""
    cs = pricetape.candles_1m(mint)
    if not cs:
        return None, 0, 0, "no_tape"
    if now - cs[-1]["t"] > TAPE_MAX_AGE_S:
        return None, len(cs), 0, "stale_ticks"
    if len(cs) < MIN_TAPE_MINUTES:
        return None, len(cs), 0, "thin_tape"
    real = {int(c["t"]): c for c in cs}
    bars, prev = [], None
    for ts in range(min(real), max(real) + 60, 60):
        c = real.get(ts)
        if c is None:
            c = {"t": ts, "o": prev, "h": prev, "l": prev, "c": prev, "v": 0.0}
        bars.append(c)
        prev = c["c"]
    bucket = int(cs[-1]["t"] // 300) * 300
    ticks = sum(1 for t, _p, _v in pricetape.merged_ticks(mint) if t >= bucket)
    return bars[-100:], len(real), ticks, None


def finalist_bars(mint: str, session=None, now: float | None = None, fetch: bool = True) -> dict:
    """1m and 5m candles for a gate finalist without a dedicated 5m GeckoTerminal call.

    1. GeckoTerminal 1m candles through the dip reader's cache and finalist-priority lane (the 5m bars
       are those candles aggregated, so one call serves both timeframes);
    2. else the price tape (Jupiter prices + the desk's quote ticks) as price-only bars (volume 0).
    {"1m", "5m", "source": "geckoterminal"|"price_tape"|"none", "cause", "tape_ticks_5m"}; a miss names
    the cause (gt_cooling, gt_budget, no_pool, stale_candles, stale_ticks, thin_tape ...)."""
    now = time.time() if now is None else now
    out = {"1m": [], "5m": [], "source": "none", "cause": None, "tape_ticks_5m": 0}
    gt_cause = None
    try:
        if fetch or chart._fetch_ohlcv is not None:
            cs, at = dip_recovery.load_candles(mint, session, now)
        else:
            pool = chart._pool_address(mint)
            row = chart._OHLCV.get(chart._ohlcv_key(pool, "1m")) if pool else None
            cs, at = (row["candles"], row["at"]) if row else ([], None)
        if cs and at is not None and now - at <= GT_MAX_AGE_S:
            out.update({"1m": cs, "5m": aggregate(cs, 300), "source": "geckoterminal"})
            _keep_last(_LAST_BARS, mint, {"at": at, **{k: out[k] for k in ("1m", "5m", "source", "tape_ticks_5m")}})
            return out
        gt_cause = "stale_candles" if cs else dip_recovery.candle_cause(mint, now)
    except Exception as e:                                   # never stall the cycle
        gt_cause = "gt_error"
        log.warning("finalist candles failed: %s", type(e).__name__)
    bars, _n, ticks, tape_cause = _tape_1m(mint, now)
    if bars is not None:
        out.update({"1m": bars, "5m": aggregate(bars, 300), "source": "price_tape", "tape_ticks_5m": ticks})
        _keep_last(_LAST_BARS, mint, {"at": now, **{k: out[k] for k in ("1m", "5m", "source", "tape_ticks_5m")}})
        return out
    row = _LAST_BARS.get(mint)                     # both sources missed: the last good bars, if recent
    if row and 0 <= now - row["at"] <= float(LAST_GOOD.get("bars_max_age_s", 0)):
        out.update({"1m": row["1m"], "5m": row["5m"], "source": "cache", "via": row["source"],
                    "age_s": round(now - row["at"], 1), "tape_ticks_5m": row["tape_ticks_5m"]})
        return out
    out["cause"] = "+".join(dict.fromkeys(c for c in (gt_cause, tape_cause) if c)) or "no_data"
    return out
