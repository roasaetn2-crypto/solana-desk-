"""Backtest the signal engine: does the BUY/SELL read predict direction and win?

This module is pure and offline. It takes real (or synthetic) OHLCV candle paths
and, at a chosen entry candle, reconstructs the point-in-time features the signal
engine can actually see from price/volume alone (momentum + flow), asks
``signal_engine.evaluate`` for a BUY / SELL / HOLD / AVOID call, then:

  * measures whether the call's direction (bullish / bearish) matches the forward
    price move over a fixed look-ahead window  -> direction accuracy, and
  * replays the desk's own exit rules (``desk/exits.py``) forward from the entry
    with a given take-profit plan -> the realised win rate and P&L.

Because OHLCV history does not carry holders / narrative / security facts, those
three signal components are neutral here. That is honest: the backtest answers
"do momentum and flow alone carry a directional edge, and which take-profit plan
turns that edge into the most P&L?" — not a claim about the full live signal.

Nothing here changes a live setting or places an order.
"""
from __future__ import annotations

import config
from config import EXITS, PAPER
from desk import exits as exit_rules
from desk import signal_engine


# --------------------------------------------------------------------------- #
# Point-in-time features (only what price + volume can tell us)
# --------------------------------------------------------------------------- #
def _pct_change(candles: list[dict], i: int, back: int) -> float | None:
    if i - back < 0:
        return None
    a, b = candles[i - back]["close"], candles[i]["close"]
    if not a:
        return None
    return (b / a - 1.0) * 100.0


def _vol_sum(candles: list[dict], i: int, back: int) -> float:
    lo = max(0, i - back + 1)
    return sum(float(c.get("vol") or 0.0) for c in candles[lo:i + 1])


def features_from_candles(candles: list[dict], i: int, bar_minutes: int = 5) -> dict:
    """Reconstruct a token dict the signal engine understands, using only candles[:i+1]."""
    per_hour = max(1, int(round(60 / max(1, bar_minutes))))
    per_6h = per_hour * 6
    per_24h = per_hour * 24

    # buy/sell proxy: up candles count as buys, down as sells, weighted by volume
    lo = max(0, i - per_hour + 1)
    buys = sells = buyers = 0
    up_vol = down_vol = 0.0
    prev = candles[lo]["open"] if lo <= i else candles[i]["open"]
    for c in candles[lo:i + 1]:
        up = c["close"] >= c.get("open", prev)
        if up:
            buys += 1
            buyers += 1
            up_vol += float(c.get("vol") or 0.0)
        else:
            sells += 1
            down_vol += float(c.get("vol") or 0.0)
        prev = c["close"]

    return {
        "symbol": "BT",
        "price_usd": candles[i]["close"],
        "change_m5": _pct_change(candles, i, 1),
        "change_h1": _pct_change(candles, i, per_hour),
        "change_h6": _pct_change(candles, i, per_6h),
        "volume_m5": float(candles[i].get("vol") or 0.0),
        "volume_h1": _vol_sum(candles, i, per_hour),
        "volume_h6": _vol_sum(candles, i, per_6h),
        "volume_h24": _vol_sum(candles, i, per_24h),
        "buys_h1": buys,
        "sells_h1": sells,
        "buyers_h1": buyers,
        "age_minutes": bar_minutes * (i + 1),
        "source": "backtest",
    }


# --------------------------------------------------------------------------- #
# Exit replay with an explicit take-profit plan
# --------------------------------------------------------------------------- #
def simulate(candles: list[dict], entry_idx: int, horizon: str,
             prof: dict | None = None) -> dict | None:
    """Enter at candles[entry_idx] close, walk forward with the desk's exit rules."""
    if entry_idx < 0 or entry_idx >= len(candles) - 1:
        return None
    entry = candles[entry_idx]["close"]
    if not entry or entry <= 0:
        return None
    prof = prof or exit_rules.profile(horizon)
    pos = {"entry": entry, "opened": candles[entry_idx]["ts"], "horizon": horizon,
           "stop_pct": prof["stop_pct"], "peak": entry, "stage": 0, "miss": 0,
           "stop_price": entry * (1 - prof["stop_pct"])}
    fee = PAPER["fee_pct"]
    slip = PAPER["slippage_pct"]
    cost_basis = 1.0 * (1 + fee + slip)      # $1 notional, fees/slippage on entry
    qty = 1.0 / entry                        # units bought for that $1
    remaining, cash, reasons, last = qty, 0.0, [], entry
    for c in candles[entry_idx + 1:]:
        price = c["close"]
        last = price
        q = {"price_usd": price, "volume_h6": c.get("vol"), "volume_h24": (c.get("vol") or 0) * 4}
        sig = exit_rules.evaluate(pos, q, now=c["ts"], prof=prof)
        if not sig:
            continue
        frac, why = sig
        sell_qty = remaining * frac
        cash += sell_qty * price * (1 - fee - slip)
        remaining -= sell_qty
        reasons.append(why)
        if remaining <= 1e-15:
            break
    if remaining > 1e-15:
        cash += remaining * last * (1 - fee - slip)
        reasons.append("data end close")
    pnl = cash - cost_basis
    return {"entry": entry, "exit_px": last, "pnl": pnl,
            "ret_pct": pnl / cost_basis * 100.0, "reasons": reasons}


# --------------------------------------------------------------------------- #
# One entry: signal read + forward outcome
# --------------------------------------------------------------------------- #
def evaluate_entry(candles: list[dict], entry_idx: int, horizon: str,
                   bar_minutes: int = 5, lookahead: int | None = None,
                   prof: dict | None = None) -> dict | None:
    """Signal at entry_idx, forward direction accuracy, and exit-rule outcome."""
    token = features_from_candles(candles, entry_idx, bar_minutes)
    sig = signal_engine.evaluate(token)
    trade = simulate(candles, entry_idx, horizon, prof=prof)
    if trade is None:
        return None

    # direction accuracy: forward close vs entry over a fixed look-ahead window
    lookahead = lookahead or max(1, int(round(60 / max(1, bar_minutes))))  # default 1h
    fwd_idx = min(len(candles) - 1, entry_idx + lookahead)
    fwd_ret = None
    if fwd_idx > entry_idx and trade["entry"]:
        fwd_ret = (candles[fwd_idx]["close"] / trade["entry"] - 1.0) * 100.0

    direction = sig["direction"]
    dir_correct = None
    if fwd_ret is not None and direction in ("bullish", "bearish"):
        dir_correct = (fwd_ret > 0) if direction == "bullish" else (fwd_ret < 0)

    return {
        "action": sig["action"], "direction": direction,
        "confidence": sig["confidence"], "win_probability": sig["win_probability"],
        "composite": sig["composite"], "score": sig["score"],
        "fwd_ret_pct": round(fwd_ret, 3) if fwd_ret is not None else None,
        "dir_correct": dir_correct,
        "pnl": round(trade["pnl"], 5), "ret_pct": round(trade["ret_pct"], 3),
        "won": trade["pnl"] > 0, "reasons": trade["reasons"],
    }


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _agg(records: list[dict]) -> dict:
    n = len(records)
    if not n:
        return {"trades": 0}
    wins = sum(1 for r in records if r.get("won"))
    dirs = [r.get("dir_correct") for r in records if r.get("dir_correct") is not None]
    gross_win = sum(r["ret_pct"] for r in records if r["ret_pct"] > 0)
    gross_loss = -sum(r["ret_pct"] for r in records if r["ret_pct"] <= 0)
    return {
        "trades": n,
        "win_rate": round(wins / n, 4),
        "wins": wins,
        "losses": n - wins,
        "avg_ret_pct": round(sum(r["ret_pct"] for r in records) / n, 3),
        "total_ret_pct": round(sum(r["ret_pct"] for r in records), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "direction_accuracy": round(sum(1 for d in dirs if d) / len(dirs), 4) if dirs else None,
        "direction_n": len(dirs),
    }


def bucket_report(records: list[dict]) -> dict:
    """Win rate and direction accuracy by confidence bucket, direction and action."""
    buckets = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 101)]
    by_conf = {}
    for lo, hi in buckets:
        sel = [r for r in records if lo <= r["confidence"] < hi]
        if sel:
            by_conf[f"{lo}-{hi - 1}"] = _agg(sel)
    by_dir = {}
    for d in ("bullish", "bearish", "neutral"):
        sel = [r for r in records if r["direction"] == d]
        if sel:
            by_dir[d] = _agg(sel)
    by_action = {}
    for a in ("BUY", "SELL", "HOLD", "AVOID"):
        sel = [r for r in records if r["action"] == a]
        if sel:
            by_action[a] = _agg(sel)
    return {"overall": _agg(records), "by_confidence": by_conf,
            "by_direction": by_dir, "by_action": by_action}


def gate_lift(records: list[dict], min_confidence: int = 60,
              min_win_probability: float = 0.52) -> dict:
    """Compare 'take every entry' vs 'only take signal BUY entries'."""
    def passes(r):
        return (r["action"] == "BUY" and r["direction"] == "bullish"
                and r["confidence"] >= min_confidence
                and r["win_probability"] >= min_win_probability)
    gated = [r for r in records if passes(r)]
    return {"all_entries": _agg(records), "signal_buys_only": _agg(gated),
            "kept_pct": round(len(gated) / len(records), 4) if records else None}


def sweep_take_profit(records: list[dict], plans: dict[str, dict]) -> dict:
    """Re-simulate every BUY entry under alternative take-profit plans.

    ``records`` must carry the forward ``_candles``/``_idx``/``_horizon`` needed to
    replay (see ``run_backtest``). Returns per-plan win rate / avg return / PF.
    """
    out = {}
    for name, overrides in plans.items():
        sims = []
        for r in records:
            if r.get("action") != "BUY":
                continue
            prof = exit_rules.profile(r["_horizon"], overrides=overrides)
            t = simulate(r["_candles"], r["_idx"], r["_horizon"], prof=prof)
            if t:
                sims.append({"ret_pct": t["ret_pct"], "won": t["pnl"] > 0})
        out[name] = _agg(sims) if sims else {"trades": 0}
    return out


DEFAULT_TP_PLANS = {
    "profile (default)": {},
    "global 1.5x/2x": {"tp1_at": 1.5, "tp1_frac": 0.33, "tp2_at": 2.0, "tp2_frac": 0.5, "trail_pct": 0.20},
    "single 1.35x": {"tp1_at": 1.35, "tp1_frac": 0.5, "tp2_at": 1e9, "tp2_frac": 0.0, "trail_pct": 0.18},
    "quick 1.25x half": {"tp1_at": 1.25, "tp1_frac": 0.5, "tp2_at": 1.6, "tp2_frac": 0.5, "trail_pct": 0.15},
    "patient 2x/3x": {"tp1_at": 2.0, "tp1_frac": 0.33, "tp2_at": 3.0, "tp2_frac": 0.5, "trail_pct": 0.25},
    "tight trail 1.4x": {"tp1_at": 1.4, "tp1_frac": 0.4, "tp2_at": 2.2, "tp2_frac": 0.5, "trail_pct": 0.12},
}


def run_backtest(paths: list[dict], bar_minutes: int = 5, lookahead: int | None = None,
                 step: int = 1, min_history: int = 24) -> dict:
    """Run the signal backtest over many candle paths.

    Each path: {"candles": [...], "horizon": "scalp"|"swing", "name": str?}. We take
    an entry every ``step`` candles once there is enough history to compute features.
    """
    records: list[dict] = []
    for p in paths:
        candles = p.get("candles") or []
        horizon = p.get("horizon") or "scalp"
        for i in range(min_history, len(candles) - 1, max(1, step)):
            rec = evaluate_entry(candles, i, horizon, bar_minutes=bar_minutes, lookahead=lookahead)
            if rec is None:
                continue
            rec["_candles"], rec["_idx"], rec["_horizon"] = candles, i, horizon
            rec["pool"] = p.get("name")
            records.append(rec)
    report = bucket_report(records)
    report["gate"] = gate_lift(records)
    report["tp_sweep"] = sweep_take_profit(records, DEFAULT_TP_PLANS)
    report["paths"] = len(paths)
    report["entries"] = len(records)
    return report
