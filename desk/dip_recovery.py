"""Dip-recovery chart reader (PAPER only).

Romz: a coin whose chart drops 20-30% and bounces back within minutes on strong
buying is real community demand (paper hands sell, diamond hands buy the dip).
A manufactured pump does not recover: once the operator stops buying it just dies.

`classify()` reads the last ~30 one-minute candles (GeckoTerminal OHLCV, via the
same last-good cache, host cooldown and budget the chart uses) and returns one of:

  recovered_dip   drawdown >= dip_min_drop_pct from the local high, then within
                  dip_recover_window_min minutes of the low price won back at least
                  dip_min_recover_pct of the drop (and still holds half of that),
                  with buy pressure >= dip_min_buy_ratio during the bounce
  dead_after_dip  the same drawdown, no recovery once the window has passed, and
                  volume fading (last 3 minutes under half the selloff's pace)
  no_dip          no drawdown that deep in the lookback
  unknown         too little or stale data, a dip still inside its recovery window,
                  a recovery on weak buying, or no recovery but volume still active

Only `dead_after_dip` can block, and only when `rulebook.dip_recovery_gate` is ON
(default OFF). Everything else is information for the analyst, the log and Trader.
Live trading never reads any of this (the agent calls it only when mode != "live").
"""
from __future__ import annotations

import logging
import time

from config import RULEBOOK
from desk import chart, pricetape, scanner

log = logging.getLogger("desk.dip")

RECOVERED, DEAD, NO_DIP, UNKNOWN = "recovered_dip", "dead_after_dip", "no_dip", "unknown"
SKIP_REASON = "dip_dead"

LOOKBACK_MIN_DEFAULT = 30
MIN_REAL_BARS = 6              # fewer real candles than this -> unknown
MAX_DATA_AGE_S = 300.0         # candles fetched longer ago than this are stale -> unknown
FADE_RATIO = 0.5               # "fading": last 3 min avg volume <= 50% of the selloff's avg
RATIO_CAP = 999.0

# Our own cap on uncached 1m OHLCV reads (on top of the chart cache, per-pool retry
# gap, the shared GeckoTerminal bucket and the host cooldown).
_BUCKET = scanner.TokenBucket(4, 2)


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _cfg(key: str, default: float) -> float:
    v = _num(RULEBOOK.get(key))
    return default if v is None else v


def _result(cls: str, detail: str, **kw) -> dict:
    return {"class": cls, "detail": detail, **kw}


def _bars_per_minute(candles, end_s: float, lookback_s: float) -> tuple[list[dict], int]:
    """Real candles inside the lookback as one bar per minute up to `end_s`.

    GeckoTerminal leaves out minutes with no trades. A silent minute is a flat,
    zero-volume bar at the last close, so a coin that stopped trading reads as dead
    rather than as missing data. Returns (bars, number of real candles used)."""
    real: dict[int, dict] = {}
    for c in candles or []:
        if not isinstance(c, dict):
            continue
        ts = _num(c.get("t"))
        if ts is None:
            continue
        if ts > 1e12:
            ts /= 1000.0
        o, h, l, cl = (_num(c.get(k)) for k in ("o", "h", "l", "c"))
        if None in (o, h, l, cl) or min(o, h, l, cl) <= 0:
            continue
        minute = int(ts // 60)
        if minute * 60 < end_s - lookback_s - 60 or minute * 60 > end_s:
            continue
        real[minute] = {"o": o, "h": max(h, o, cl), "l": min(l, o, cl), "c": cl,
                        "v": max(0.0, _num(c.get("v")) or 0.0)}
    if not real:
        return [], 0
    first, last = min(real), int(end_s // 60)
    bars, prev = [], None
    for m in range(first, last + 1):
        b = real.get(m)
        if b is None:
            if prev is None:
                continue
            b = {"o": prev, "h": prev, "l": prev, "c": prev, "v": 0.0}
        bars.append(b)
        prev = b["c"]
    return bars, len(real)


def _worst_drawdown(bars):
    """(drop, peak_idx, low_idx, high, low): the deepest peak-to-trough fall, low after peak."""
    best = None
    peak, peak_i = 0.0, 0
    for i, b in enumerate(bars):
        # a single big red candle falls from its own open
        if b["o"] > peak:
            peak, peak_i = b["o"], i
        drop = 1.0 - b["l"] / peak if peak > 0 else 0.0
        if best is None or drop > best[0]:
            best = (drop, peak_i, i, peak, b["l"])
        if b["h"] > peak:
            peak, peak_i = b["h"], i
    return best


def classify(candles, *, now: float | None = None, fetched_at: float | None = None,
             buys_m5=None, sells_m5=None, volume_known: bool = True) -> dict:
    """Classify the recent 1m candles. Pure: no I/O. See the module docstring.

    volume_known=False means the candles are price-only (the Jupiter price tape): a recovery can still
    be read when 5m buy/sell counts are given, but "dead after a dip" needs the volume fade, so it
    is never called without volume."""
    now = time.time() if now is None else now
    min_drop = _cfg("dip_min_drop_pct", 0.20)
    window = max(1, int(_cfg("dip_recover_window_min", 5)))
    min_recover = _cfg("dip_min_recover_pct", 0.50)
    min_buy = _cfg("dip_min_buy_ratio", 1.3)
    lookback = max(10, int(_cfg("dip_lookback_min", LOOKBACK_MIN_DEFAULT)))

    if fetched_at is not None and now - fetched_at > MAX_DATA_AGE_S:
        return _result(UNKNOWN, "stale_candles")
    end_s = min(now, fetched_at + 60.0) if fetched_at is not None else now
    bars, real_n = _bars_per_minute(candles, end_s, lookback * 60.0)
    if real_n < MIN_REAL_BARS or len(bars) < MIN_REAL_BARS:
        return _result(UNKNOWN, "insufficient_candles", bars=real_n)

    drop, peak_i, low_i, high, low = _worst_drawdown(bars)
    if drop < min_drop:
        return _result(NO_DIP, "no_drawdown", drop_pct=round(drop, 4), bars=len(bars))

    span = high - low
    last_i = len(bars) - 1
    since_low = last_i - low_i
    win_closes = [b["c"] for b in bars[low_i:low_i + window + 1]]
    retrace_best = (max(win_closes) - low) / span if span > 0 else 0.0
    retrace_now = (bars[-1]["c"] - low) / span if span > 0 else 0.0
    info = {"drop_pct": round(drop, 4), "retrace_best": round(retrace_best, 3),
            "retrace_now": round(retrace_now, 3), "minutes_since_low": since_low,
            "bars": len(bars)}

    # buy pressure during the bounce: live 5m buy/sell counts when the low is recent,
    # else green vs red candle volume from the low to now
    b, s = _num(buys_m5), _num(sells_m5)
    ratio, basis = None, None
    if b is not None and s is not None and (b + s) > 0 and since_low <= 10:
        ratio, basis = (RATIO_CAP if s == 0 else b / s), "txns_m5"
    else:
        green = sum(x["v"] for x in bars[low_i + 1:] if x["c"] >= x["o"])
        red = sum(x["v"] for x in bars[low_i + 1:] if x["c"] < x["o"])
        if red > 0:
            ratio, basis = green / red, "candle_volume"
        elif green > 0:
            ratio, basis = RATIO_CAP, "candle_volume"
    if ratio is not None:
        info["buy_ratio"] = round(min(ratio, RATIO_CAP), 2)
        info["buy_basis"] = basis

    if retrace_best >= min_recover and retrace_now >= min_recover / 2.0:
        if ratio is not None and ratio >= min_buy:
            return _result(RECOVERED, "bounced_on_buying", **info)
        return _result(UNKNOWN, "recovered_weak_buys" if ratio is not None else "recovered_no_volume", **info)

    if since_low < window:
        return _result(UNKNOWN, "dip_pending", **info)

    if not volume_known:
        return _result(UNKNOWN, "dip_unresolved_no_volume", **info)

    drop_vols = [x["v"] for x in bars[peak_i + 1:low_i + 1]] or [bars[low_i]["v"]]
    drop_avg = sum(drop_vols) / len(drop_vols)
    tail = [x["v"] for x in bars[-3:]]
    tail_avg = sum(tail) / len(tail)
    fade = (tail_avg / drop_avg) if drop_avg > 0 else 0.0
    info["fade_ratio"] = round(fade, 2)
    if fade <= FADE_RATIO:
        return _result(DEAD, "no_recovery_volume_fading", **info)
    return _result(UNKNOWN, "dip_unresolved", **info)


# ---------- data (cache-first, rate-limited) ----------

def load_candles(mint: str, session=None, now: float | None = None) -> tuple[list[dict], float | None]:
    """(1m candles, time they were fetched). Spends at most one GeckoTerminal call, and only
    when the cached copy is older than the chart's 1m refresh, the pool's retry gap has
    passed, the host is not cooling and our own bucket allows. Otherwise serves the cache."""
    now = time.time() if now is None else now
    if chart._fetch_ohlcv is not None:                       # test hook
        return chart._network_ohlcv(mint, "1m", session), now
    if str(mint).startswith("DEMO"):
        return [], None
    pool = chart._pool_address(mint)
    if not pool:
        return [], None
    key = chart._ohlcv_key(pool, "1m")
    row = chart._OHLCV.get(key)
    if row and now - row["at"] < chart.REFRESH_S["1m"]:
        return row["candles"], row["at"]
    keyed = chart.cg_available(now)
    can_call = (chart.cg_ready(now) if keyed else not scanner.host_cooling("geckoterminal", now))
    scanner.note_finalist_wait("geckoterminal", now)       # a finalist needs GT: discovery leaves a reserve
    if (can_call and now - chart._ATTEMPT.get(key, 0.0) >= chart.MISS_RETRY_S
            and _BUCKET.allow(1, now=now)):
        try:
            cs = chart._fetch_and_store(pool, "1m", session, now, priority=True)
        except Exception as e:                               # never stall the cycle
            log.warning("dip ohlcv fetch failed: %s", type(e).__name__)
            cs = []
        if cs:
            return cs, now
    return (row["candles"], row["at"]) if row else ([], None)


def cached_tx_m5(t: dict, now: float | None = None) -> tuple[float | None, float | None]:
    """5m buys/sells already in the scanner's caches (GeckoTerminal pool counts, else a fresh
    Jupiter row). No request."""
    now = time.time() if now is None else now
    pool = t.get("pool")
    hit = scanner._TX_CACHE.get(pool) if pool else None
    if hit and hit.get("c") and hit["exp"] > now:
        return hit["c"][0], hit["c"][1]
    got, _ = scanner.jup_tx_m5(t.get("mint") or "", now)
    if got and (got["buys_m5"] + got["sells_m5"]) > 0:
        return got["buys_m5"], got["sells_m5"]
    return None, None


def candle_cause(mint: str, now: float | None = None) -> str:
    """Why load_candles had no GeckoTerminal 1m candles for `mint`, from the limiter state (no request)."""
    now = time.time() if now is None else now
    if str(mint).startswith("DEMO"):
        return "demo"
    pool = chart._pool_address(mint)
    if not pool:
        return "no_pool"
    ep_id = f"gt:ohlcv:{pool}:minute:1"
    st = scanner.last_status(ep_id)
    if st == 404:
        return "gt_404"
    keyed = chart.cg_available(now)
    if keyed:
        if not chart.cg_ready(now):
            return "gt_budget"
    elif scanner.host_cooling("geckoterminal", now):
        return "gt_cooling"
    if st:
        return f"gt_{st}"
    if now - chart._ATTEMPT.get(chart._ohlcv_key(pool, "1m"), 0.0) < chart.MISS_RETRY_S:
        return "gt_retry_wait"
    if _BUCKET.peek(now) < 1.0:
        return "gt_cap"
    return "gt_budget"


def read(t: dict, ds: dict | None = None, session=None, now: float | None = None) -> dict:
    """Classify one candidate. Never raises; any failure is `unknown`."""
    now = time.time() if now is None else now
    try:
        candles, at = load_candles(t.get("mint") or "", session, now)
        b = s = None
        if ds and ds.get("available"):
            b, s = ds.get("buys_m5"), ds.get("sells_m5")
        if _num(b) is None or _num(s) is None:
            b, s = cached_tx_m5(t, now)
        out = classify(candles, now=now, fetched_at=at, buys_m5=b, sells_m5=s)
        if out["class"] == UNKNOWN and out.get("detail") in ("insufficient_candles", "stale_candles"):
            # no usable GeckoTerminal candles: the price tape (Jupiter prices), price only, no volume
            tape = pricetape.candles_1m(t.get("mint") or "")
            if tape:
                alt = classify(tape, now=now, fetched_at=now, buys_m5=b, sells_m5=s, volume_known=False)
                if alt["class"] != UNKNOWN or alt.get("detail") not in ("insufficient_candles", "stale_candles"):
                    alt["source"] = "price_tape"
                    return alt
        return out
    except Exception as e:
        log.warning("dip read failed: %s", type(e).__name__)
        return _result(UNKNOWN, "error")


def gate(info: dict | None) -> str | None:
    """`dip_dead` for a dead_after_dip candidate when the gate is ON, else None.
    Unknown, no_dip and recovered_dip never block."""
    if not RULEBOOK.get("dip_recovery_gate", False):
        return None
    return SKIP_REASON if (info or {}).get("class") == DEAD else None


def describe(info: dict | None) -> str:
    i = info or {}
    bits = [i.get("class", UNKNOWN)]
    if i.get("drop_pct") is not None:
        bits.append(f"drop {i['drop_pct']:.0%}")
    if i.get("retrace_best") is not None:
        bits.append(f"retraced {i['retrace_best']:.0%}")
    if i.get("buy_ratio") is not None:
        bits.append(f"buy/sell {i['buy_ratio']:g}")
    if i.get("fade_ratio") is not None:
        bits.append(f"vol fade {i['fade_ratio']:g}")
    if i.get("detail"):
        bits.append(i["detail"])
    return ", ".join(bits)
