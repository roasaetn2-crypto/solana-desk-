"""Confirmed entry checklist and momentum-break exit for the fast scalper (PAPER only).

Romz asked for "sure entry call and exit". The checklist turns the scalp
rulebook (Market ANALYSIS, 2026-11-19) into five yes/no checks, each built only
from data the desk already has: the scan row, DexScreener's 5m counts, the 5m
candles the rulebook already fetched, and 1m candles from the chart cache.

  trend     9 EMA >= 21 EMA on 1m AND on 5m (shorter EMAs on a young coin)
  buy       5m buys / sells >= confirm.min_buy_sell
  turnover  1h volume / liquidity >= confirm.min_turnover (not a dead pool)
  knife     not 3 falling 1m closes in a row, not a 1m bar down > knife_drop
  extended  not already up > max_run_5m in 5m or > max_run_1m on the last 1m bar

A check with no data is "n/a" and does NOT block: guessing is worse than
skipping that one check, and a 3-minute-old launch would otherwise never trade.
Romz wants the scalper to keep trading, so the thresholds catch clear red flags
(downtrend, sellers in control, dead pool, falling knife, vertical candle), not
"perfect" setups.

Everything here is pure (no I/O), so the tests pin each rule.
"""
from __future__ import annotations

from config import CONFIRM

CHECKS = ("trend", "buy", "turnover", "knife", "extended")


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _bars(candles, now: float | None = None, bar_s: int = 60, closed_only: bool = False) -> list[tuple]:
    """(t, o, h, l, c) from either candle spelling, oldest first."""
    out = []
    for c in candles or []:
        if not isinstance(c, dict):
            continue
        t = _num(c.get("t", c.get("ts")))
        if t and t > 1e12:
            t /= 1000.0
        o = _num(c.get("o", c.get("open")))
        h = _num(c.get("h", c.get("high")))
        lo = _num(c.get("l", c.get("low")))
        cl = _num(c.get("c", c.get("close")))
        if not o or not cl or o <= 0 or cl <= 0:
            continue
        if closed_only and now is not None and t is not None and t + bar_s > now:
            continue
        out.append((t, o, h if h else max(o, cl), lo if lo else min(o, cl), cl))
    out.sort(key=lambda b: b[0] or 0)
    return out


def _fresh(bars: list[tuple], now: float | None, max_stale_s: float) -> bool:
    if not bars or now is None:
        return bool(bars)
    t = bars[-1][0]
    return t is None or now - t <= max_stale_s


def ema(values: list[float], span: int) -> float | None:
    if not values or span < 1:
        return None
    k = 2.0 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def trend_state(candles, now: float | None = None, bar_s: int = 60) -> dict:
    """Fast EMA vs slow EMA on closes. 9/21 when 21+ bars, else a scaled pair
    (fast = half the bars, slow = all bars) once there are min_trend_bars."""
    bs = _bars(candles)
    if not _fresh(bs, now, 3 * bar_s + 60):
        return {"ok": None, "why": "no fresh candles"}
    n = len(bs)
    need = int(CONFIRM.get("min_trend_bars", 5))
    if n < need:
        return {"ok": None, "why": f"{n} bars < {need}"}
    closes = [b[4] for b in bs]
    slow = min(21, n)
    fast = min(9, max(2, slow // 2))
    ef, es = ema(closes, fast), ema(closes, slow)
    ok = ef >= es
    return {"ok": ok, "fast": fast, "slow": slow, "ema_fast": ef, "ema_slow": es,
            "why": f"EMA{fast} {'>=' if ok else '<'} EMA{slow}"}


def knife_state(candles_1m, now: float | None = None) -> dict:
    bs = _bars(candles_1m)
    if len(bs) < 2 or not _fresh(bs, now, 240):
        return {"ok": None, "why": "no fresh 1m candles"}
    n = int(CONFIRM.get("knife_bars", 3))
    closes = [b[4] for b in bs]
    if len(closes) >= n + 1 and all(closes[-i] < closes[-i - 1] for i in range(1, n + 1)):
        return {"ok": False, "why": f"{n} falling 1m closes"}
    drop = float(CONFIRM.get("knife_drop", 0.08))
    o, cl = bs[-1][1], bs[-1][4]
    if cl / o - 1 <= -drop:
        return {"ok": False, "why": f"1m bar {cl / o - 1:+.0%}"}
    return {"ok": True, "why": "no falling knife"}


def extended_state(t: dict, ds: dict | None, candles_1m, now: float | None = None) -> dict:
    ch = None
    for src in (ds or {}, t or {}):
        ch = _num(src.get("change_m5"))
        if ch is not None:
            break
    bs = _bars(candles_1m)
    up1 = None
    if bs and _fresh(bs, now, 240):
        up1 = bs[-1][4] / bs[-1][1] - 1
    if ch is None and up1 is None:
        return {"ok": None, "why": "no 5m change or 1m candle"}
    if ch is not None and ch / 100.0 > float(CONFIRM.get("max_run_5m", 0.40)):
        return {"ok": False, "why": f"up {ch:.0f}% in 5m (vertical)"}
    if up1 is not None and up1 > float(CONFIRM.get("max_run_1m", 0.15)):
        return {"ok": False, "why": f"last 1m bar {up1:+.0%} (chasing)"}
    return {"ok": True, "why": "not overextended"}


def buy_state(ds: dict | None, t: dict | None = None, tx: dict | None = None) -> dict:
    """5m buys / sells vs confirm.min_buy_sell. Sources, first with counts wins: DexScreener (ds),
    the scan row (t), then `tx`, the counts the tx-ratio gate used from Jupiter / GeckoTerminal when
    DexScreener had none. One set of counts for both checks."""
    for name, src in (("dexscreener", ds or {}), ("scan", t or {}), ((tx or {}).get("source") or "tx", tx or {})):
        b, s = _num(src.get("buys_m5")), _num(src.get("sells_m5"))
        if b is not None and s is not None and b + s > 0:
            r = b / s if s else float("inf")
            need = float(CONFIRM.get("min_buy_sell", 1.2))
            return {"ok": r >= need, "ratio": round(r, 2) if s else None, "source": name,
                    "why": f"buy/sell {('%.2f' % r) if s else 'all buys'} vs {need:g}"
                           + (f" ({name})" if src is tx else "")}
    return {"ok": None, "why": "no 5m buy/sell counts"}


def turnover_state(t: dict | None, ds: dict | None = None) -> dict:
    """1h volume / liquidity. A coin younger than an hour is judged pro rata on
    its age, so a 10-minute launch is not failed for an hour it has not lived."""
    for src in (ds or {}, t or {}):
        v, liq = _num(src.get("volume_h1")), _num(src.get("liquidity_usd"))
        if v is not None and liq and liq > 0:
            need = float(CONFIRM.get("min_turnover", 0.15))
            age = _num((t or {}).get("age_minutes"))
            if age is not None and 0 < age < 60:
                need *= max(age, 5.0) / 60.0
            r = v / liq
            return {"ok": r >= need, "ratio": round(r, 3),
                    "why": f"1h vol/liq {r:.2f} vs {need:.2f}"}
    return {"ok": None, "why": "no 1h volume or liquidity"}


def checklist(t: dict, ds: dict | None, candles_1m, candles_5m, now: float | None = None,
              tx: dict | None = None) -> dict:
    """{'ok', 'fail', 'checks': {name: {'ok', 'why'}}, 'passed', 'known'}.
    ok is False when any enabled, known check fails; n/a checks never block."""
    tr1 = trend_state(candles_1m, now, 60)
    tr5 = trend_state(candles_5m, now, 300)
    if tr1["ok"] is False or tr5["ok"] is False:
        bad = [f"{tf} {s['why']}" for tf, s in (("1m", tr1), ("5m", tr5)) if s["ok"] is False]
        trend = {"ok": False, "why": "; ".join(bad)}
    elif tr1["ok"] is None and tr5["ok"] is None:
        trend = {"ok": None, "why": "no trend data"}
    else:
        trend = {"ok": True, "why": " / ".join(
            f"{tf} {s['why']}" for tf, s in (("1m", tr1), ("5m", tr5)) if s["ok"])}
    checks = {
        "trend": trend,
        "buy": buy_state(ds, t, tx),
        "turnover": turnover_state(t, ds),
        "knife": knife_state(candles_1m, now),
        "extended": extended_state(t, ds, candles_1m, now),
    }
    enabled = CONFIRM.get("checks") or {}
    fail = None
    for name in CHECKS:
        c = checks[name]
        if not enabled.get(name, True):
            c["ok"], c["why"] = None, "off"
        if c["ok"] is False and fail is None:
            fail = name
    known = sum(1 for c in checks.values() if c["ok"] is not None)
    passed = sum(1 for c in checks.values() if c["ok"] is True)
    return {"ok": fail is None, "fail": fail, "reason": f"confirm_{fail}" if fail else None,
            "checks": checks, "passed": passed, "known": known}


def entry_candle_low(candles_1m, candles_5m, now: float | None = None) -> float | None:
    """Low of the candle we buy into (1m if fresh, else 5m), for the momentum exit."""
    for cs, stale in ((candles_1m, 240), (candles_5m, 900)):
        bs = _bars(cs)
        if bs and _fresh(bs, now, stale):
            return bs[-1][3]
    return None


def entry_candle_low_5m(candles_5m, now: float | None = None) -> float | None:
    """Low of the 5m candle the entry is in (the newest bar, if fresh), for the 5m momentum exit."""
    bs = _bars(candles_5m)
    if bs and _fresh(bs, now, 600):
        return bs[-1][3]
    return None


def momentum_exit_5m(pos: dict, candles_5m, now: float) -> str | None:
    """5m flavour of the entry-candle-low check: the last CLOSED 5m bar (never the forming one) ended under
    the low of the 5m candle we bought into. The anchor is pos["entry_candle_low_5m"], else computed from the
    5m bar that contains the entry; no anchor, no closed fresh bar, or no data = no exit (never forced)."""
    if not CONFIRM.get("momentum_exit", True) or not pos:
        return None
    if int(pos.get("stage") or 0) >= 1:
        return None
    opened = _num(pos.get("opened"))
    if opened is None or now - opened < float(CONFIRM.get("min_hold_s", 90)):
        return None
    allb = _bars(candles_5m, now, 300, closed_only=True)
    if not allb or now - allb[-1][0] > 600 or allb[-1][0] + 300 <= opened:
        return None                       # nothing closed yet that is fresh and at/after the entry bar
    low = _num(pos.get("entry_candle_low_5m"))
    if not low:
        for t0, _o, _h, lo, _c in reversed(_bars(candles_5m)):
            if t0 is not None and t0 <= opened < t0 + 300:
                low = lo
                entry = _num(pos.get("entry"))
                if low and entry and low > entry:
                    low = entry
                break
    if low and allb[-1][4] < low:
        return "momentum: 5m closed under the entry candle low"
    return None


def momentum_exit(pos: dict, candles_1m, now: float, candles_5m=None, timeframe: str = "1m") -> str | None:
    """Before TP1 only: sell when the move that got us in has clearly broken.

      * a CLOSED 1m bar ends under the entry candle's low, or
      * a CLOSED 1m bar ends under the 1m 9 EMA while the trade is red.
    Needs `min_hold_s` in the trade first (no shake-outs on the entry tick).
    Missing candles = no forced exit; the normal stop still protects."""
    if not CONFIRM.get("momentum_exit", True) or not pos:
        return None
    if timeframe == "5m":
        # the entry-low clause reads 5m bars; the 1m 9-EMA clause below is unchanged
        why = momentum_exit_5m(pos, candles_5m, now)
        if why:
            return why
    if int(pos.get("stage") or 0) >= 1:
        return None                         # after TP1 the cost-aware breakeven + trail own it
    opened = _num(pos.get("opened"))
    if opened is None or now - opened < float(CONFIRM.get("min_hold_s", 90)):
        return None
    bs = [b for b in _bars(candles_1m, now, 60, closed_only=True) if b[0] is None or b[0] + 60 > opened]
    allb = _bars(candles_1m, now, 60, closed_only=True)
    if not bs or not _fresh(allb, now, 180):
        return None
    last_close = allb[-1][4]
    low = _num(pos.get("entry_candle_low"))
    if low and last_close < low and timeframe != "5m":
        return "momentum: 1m closed under the entry candle low"
    entry = _num(pos.get("entry"))
    if len(allb) >= 9 and entry and last_close < entry:
        e9 = ema([b[4] for b in allb], 9)
        if e9 and last_close < e9 * (1 - float(CONFIRM.get("ema_break_buffer", 0.005))):
            return "momentum: 1m closed under the 9 EMA while red"
    return None
