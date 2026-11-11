"""Exit rules: pure functions, no I/O. Used by PaperBroker and by scripts/replay_exits.py.

MERGE: combines the old desk's hard stop + armed trail + timeout (agents/risk.py) with the new
desk's partial take-profit, and picks the profile by horizon (scalp vs swing, agents/timeframe.py).

Order each tick: stop / trail -> TP2 -> TP1 -> volume decay -> timeout / max hold.
The stop only ever moves up (to breakeven after TP1).
"""
from __future__ import annotations

from config import EXITS


def horizon_for(age_minutes: float) -> str:
    return "scalp" if (age_minutes or 0) < EXITS["scalp_max_age_hours"] * 60 else "swing"


def profile(name: str | None, overrides: dict | None = None) -> dict:
    base = dict(EXITS["profiles"].get(name or "swing", EXITS["profiles"]["swing"]))
    if overrides:
        base.update(overrides)
    return base


def init_state(pos: dict, horizon: str) -> None:
    pr = profile(horizon)
    pos.update({"horizon": horizon, "stop_pct": pr["stop_pct"], "peak": pos["entry"],
                "stage": 0, "miss": 0, "stop_price": pos["entry"] * (1 - pr["stop_pct"])})


def evaluate(pos: dict, q: dict | None, now: float, prof: dict | None = None) -> tuple[float, str] | None:
    """Return (fraction of REMAINING qty to sell, reason) or None. Mutates pos state."""
    entry = pos["entry"]
    if "stage" not in pos:   # position opened before profiles existed
        init_state(pos, "swing")
        pos["stop_price"] = entry * (1 - EXITS["stop_pct"])
        pos["stage"] = 1 if pos.get("half_taken") else 0
    pr = prof or profile(pos.get("horizon"))
    held_min = (now - pos["opened"]) / 60

    if q is None or not q.get("price_usd"):
        pos["miss"] = pos.get("miss", 0) + 1
        if pos["miss"] >= EXITS["missing_quote_closes"]:
            return (1.0, f"no quote x{pos['miss']}, cannot measure, close")
        return None
    pos["miss"] = 0
    price = q["price_usd"]
    pos["last"] = price
    pos["peak"] = max(pos.get("peak", entry), price)

    if price <= pos["stop_price"]:
        tag = "breakeven stop" if pos["stop_price"] >= entry else f"stop -{int(pr['stop_pct'] * 100)}%"
        return (1.0, tag)
    if pos["stage"] >= 1 and price <= pos["peak"] * (1 - pr["trail_pct"]):
        return (1.0, f"trail -{int(pr['trail_pct'] * 100)}% from peak")
    if pos["stage"] == 1 and price >= entry * pr["tp2_at"]:
        pos["stage"] = 2
        return (pr["tp2_frac"], f"TP2 at {pr['tp2_at']}x")
    if pos["stage"] == 0 and price >= entry * pr["tp1_at"]:
        pos["stage"] = 1
        pos["half_taken"] = True
        pos["stop_price"] = max(pos["stop_price"], entry)
        return (pr["tp1_frac"], f"TP1 at {pr['tp1_at']}x")
    if held_min >= EXITS["volume_decay_min_minutes"] and q.get("volume_h24", 0) > 0:
        ratio = q.get("volume_h6", 0) / (q["volume_h24"] / 4)
        if ratio < EXITS["volume_decay_ratio"]:
            return (1.0, f"volume decay {ratio:.2f}")
    if held_min >= pr["timeout_hours"] * 60 and price < entry * (1 + pr["timeout_min_gain"]):
        return (1.0, f"timeout {pr['timeout_hours']:g}h under +{int(pr['timeout_min_gain'] * 100)}%")
    if held_min >= pr["max_hold_hours"] * 60:
        return (1.0, "max hold time")
    return None
