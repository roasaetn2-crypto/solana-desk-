"""Exit rules: pure functions, no I/O. Used by PaperBroker and by scripts/replay_exits.py.

MERGE: combines the old desk's hard stop + armed trail + timeout (agents/risk.py) with the new
desk's partial take-profit, and picks the profile by horizon (scalp vs swing, agents/timeframe.py).

Order each tick: stop / trail -> TP2 -> TP1 -> volume decay -> timeout / max hold.
The stop only ever moves up (to breakeven after TP1).
"""
from __future__ import annotations

from config import EXITS, PAPER


def horizon_for(age_minutes: float) -> str:
    return "scalp" if (age_minutes or 0) < EXITS["scalp_max_age_hours"] * 60 else "swing"


def profile(name: str | None, overrides: dict | None = None) -> dict:
    """The exit profile for a horizon, after the operator's take-profit mode.

    mode "profile" (default) keeps the per-horizon ladder exactly as written.
    mode "global"  replaces the take-profit rungs with EXITS["global"].
    mode "single"  collapses to one take-profit + a trail; TP2 is disabled.
    """
    base = dict(EXITS["profiles"].get(name or "swing", EXITS["profiles"]["swing"]))
    mode = str(EXITS.get("take_profit_mode") or "profile").lower()
    if mode == "global":
        g = EXITS.get("global") or {}
        for k in ("tp1_at", "tp1_frac", "tp2_at", "tp2_frac", "trail_pct"):
            if g.get(k) is not None:
                base[k] = g[k]
    elif mode == "single":
        s = EXITS.get("single") or {}
        base.update({
            "tp1_at": float(s.get("tp_at", 1.35)),
            "tp1_frac": float(s.get("tp_frac", 0.5)),
            "tp2_at": 1e9,          # never reached: one rung only
            "tp2_frac": 0.0,
            "trail_pct": float(s.get("trail_pct", 0.18)),
        })
    if overrides:
        base.update(overrides)
    return base


def runner_trail(pr: dict) -> float:
    """Trail for the runner after TP1: the profile's trail, capped by EXITS["runner_trail_pct"]."""
    cap = EXITS.get("runner_trail_pct")
    return min(pr["trail_pct"], cap) if cap else pr["trail_pct"]


def init_state(pos: dict, horizon: str) -> None:
    pr = profile(horizon)
    pos.update({"horizon": horizon, "stop_pct": pr["stop_pct"], "peak": pos["entry"],
                "stage": 0, "miss": 0, "stop_price": pos["entry"] * (1 - pr["stop_pct"])})


def plausible(pos: dict, price: float) -> bool:
    """Is this quote close enough to the last accepted one to be believed?

    The desk marks a position from a ~15s quote. A print that jumps more than
    `quote_max_jump_x` (or falls further than `quote_max_drop_x`) in one tick is
    almost never a real move: it is a decimals mismatch, a different pool for the
    same mint, or a duplicated pair. Trusting it is what turned a $160 paper book
    into a $549,195,436 equity on 2026-11-14.

    A rejected print is not thrown away: it is remembered in `pos["odd_px"]`, and
    if the NEXT read lands within 25% of it, the new level is accepted. A real
    gap-up therefore costs one extra tick, while a one-off bad print never
    touches `last`, `peak`, the equity or a sell fill.
    """
    if price <= 0:
        return False
    entry = pos.get("entry") or 0.0
    hi = float(PAPER.get("max_mark_mult", 1000.0))
    lo = float(PAPER.get("min_mark_mult", 0.001))
    # Hard band against the ENTRY price. No number of agreeing reads makes a
    # 1e9x print real, so this one is absolute: a memecoin bought at 1e-4 can
    # never trade at 1e5. This is what actually kills the $549M equity bug.
    if entry and not (entry * lo <= price <= entry * hi):
        return False
    last = pos.get("last") or 0.0
    if last <= 0 or (entry and not (entry * lo <= last <= entry * hi)):
        pos["last"] = entry or price          # a corrupt last is not a baseline
        last = pos["last"]
    if last <= 0:
        return True
    up = float(EXITS.get("quote_max_jump_x") or 25.0)
    down = float(EXITS.get("quote_max_drop_x") or 25.0)
    if last / down <= price <= last * up:
        pos["odd"] = 0
        pos["odd_px"] = None
        return True
    prev = pos.get("odd_px")
    pos["odd"] = int(pos.get("odd") or 0) + 1
    need = int(EXITS.get("quote_confirm_reads") or 2)
    if prev and 0.8 <= price / prev <= 1.25 and pos["odd"] >= need:
        pos["odd"] = 0
        pos["odd_px"] = None
        return True
    pos["odd_px"] = price
    return False


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
    price = q["price_usd"]
    if not plausible(pos, price):
        # A print this far from the last accepted one is data, not a move. Treat
        # it like a missing quote: never mark the book from it, never sell into
        # it. After `missing_quote_closes` such reads the position is closed at
        # the last GOOD price, which is the honest exit.
        pos["miss"] = pos.get("miss", 0) + 1
        if pos["miss"] >= EXITS["missing_quote_closes"]:
            return (1.0, f"bad quote x{pos['miss']}, cannot measure, close")
        return None
    pos["miss"] = 0
    pos["last"] = price
    pos["peak"] = max(pos.get("peak", entry), price)

    if price <= pos["stop_price"]:
        tag = "breakeven stop" if pos["stop_price"] >= entry else f"stop -{int(pr['stop_pct'] * 100)}%"
        return (1.0, tag)
    if pos["stage"] >= 1:
        # Runner (what is left after TP1). It trails no looser than runner_trail_pct and
        # has its own clock, so a sleepy runner frees its slot instead of holding it for days.
        trail = runner_trail(pr)
        if price <= pos["peak"] * (1 - trail):
            return (1.0, f"trail -{int(round(trail * 100))}% from peak")
        cap_min = EXITS.get("runner_max_minutes")
        if cap_min:
            half_at = pos.setdefault("half_at", now)   # older runners: the clock starts now
            if (now - half_at) / 60 >= cap_min:
                return (1.0, f"runner time cap {int(cap_min)}m after TP1")
    if pos["stage"] == 1 and price >= entry * pr["tp2_at"]:
        pos["stage"] = 2
        return (pr["tp2_frac"], f"TP2 at {pr['tp2_at']}x")
    if pos["stage"] == 0 and price >= entry * pr["tp1_at"]:
        pos["stage"] = 1
        pos["half_taken"] = True
        pos["half_at"] = now
        if EXITS.get("breakeven_after_half", True):
            pos["stop_price"] = max(pos["stop_price"], entry)     # only ever up
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
