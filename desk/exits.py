"""Exit rules: pure functions, no I/O. Used by PaperBroker and by scripts/replay_exits.py.

MERGE: combines the old desk's hard stop + armed trail + timeout (agents/risk.py) with the new
desk's partial take-profit, and picks the profile by horizon (scalp vs swing, agents/timeframe.py).

Order each tick: stop / trail -> TP2 -> TP1 -> volume decay -> timeout / max hold.
The stop only ever moves up (to breakeven after TP1).
"""
from __future__ import annotations

from config import EXITS, PAPER, SCALP


def horizon_for(age_minutes: float) -> str:
    """Which exit ladder a coin of this age gets.

    When the fast-scalp lane is on, its own max age IS the horizon boundary.
    Otherwise a coin 90 minutes old would be classed "scalp" (because the legacy
    scalp_max_age_hours is 6) while the scalp *gate* refuses anything over 60
    minutes -- a dead zone where the exits and the entry filter disagreed.
    """
    age = age_minutes or 0
    if SCALP.get("enabled"):
        return "scalp" if age <= SCALP["max_age_minutes"] else "swing"
    return "scalp" if age < EXITS["scalp_max_age_hours"] * 60 else "swing"


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
    # The pullback package (meme_tactics) ships its own wider stop and slower
    # take-profit ladder. Apply it here so the broker, the backtests and the
    # signal plan all read the same numbers. Off by default.
    try:
        from desk import meme_tactics
        ov = meme_tactics.exit_overrides()
        if ov:
            base.update(ov)
    except Exception:
        pass
    # Fast-scalp lane. When SCALP["enabled"], the SCALP block is the single
    # source of truth for the scalp horizon, and it is applied LAST so it wins
    # over the pullback ladder. The swing horizon is untouched. This is what
    # turns a 4-hour scalp profile into a 1-10 minute in-and-out.
    if name == "scalp" and SCALP.get("enabled"):
        for k in ("stop_pct", "tp1_at", "tp1_frac", "tp2_at", "tp2_frac", "trail_pct"):
            if SCALP.get(k) is not None:
                base[k] = SCALP[k]
        if SCALP.get("timeout_minutes") is not None:
            base["timeout_minutes"] = SCALP["timeout_minutes"]
        if SCALP.get("max_hold_minutes") is not None:
            base["max_hold_minutes"] = SCALP["max_hold_minutes"]
        if SCALP.get("timeout_min_gain") is not None:
            base["timeout_min_gain"] = SCALP["timeout_min_gain"]
    return base


def runner_trail(pr: dict) -> float:
    """Trail for the runner after TP1: the profile's trail, capped by EXITS["runner_trail_pct"]."""
    cap = EXITS.get("runner_trail_pct")
    return min(pr["trail_pct"], cap) if cap else pr["trail_pct"]


def init_state(pos: dict, horizon: str) -> None:
    pr = profile(horizon)
    pos.update({"horizon": horizon, "stop_pct": pr["stop_pct"], "peak": pos["entry"],
                "stage": 0, "miss": 0, "stop_price": pos["entry"] * (1 - pr["stop_pct"])})
    # Cost basis per unit (ticket incl. the buy fee / units received). The exit
    # ladder needs it to know where "breakeven" really is; `entry` alone is the
    # slipped fill and ignores the buy fee.
    if pos.get("cost") and pos.get("qty"):
        pos.setdefault("qty0", pos["qty"])
        pos.setdefault("basis", pos["cost"] / pos["qty"])


def _sell_fee_usd(notional: float) -> float:
    """The paper sell fee for one leg (same model and 50% cap as the broker)."""
    try:
        from desk import fees
        if not PAPER.get("use_live_fee_model", True):
            return min(notional * float(PAPER.get("fee_pct") or 0.0), notional * 0.5)
        return fees.paper_fee_per_side_usd(notional)
    except Exception:
        return 0.0


def net_breakeven_mult(pos: dict, frac: float) -> float | None:
    """Price multiple of `entry` at which selling `frac` of what is left nets zero.

    Selling q units at quote P books q*P*(1-slip) - fee. That covers those
    units' share of the cost (q * basis) only when
        P >= (basis + fee/q) / (1 - slip).
    None when the position carries no cost basis (old rows, pure-rule tests),
    so callers fall back to the configured ladder unchanged.
    """
    entry = pos.get("entry") or 0.0
    qty = pos.get("qty") or 0.0
    basis = pos.get("basis")
    if basis is None and pos.get("cost") and pos.get("qty0"):
        basis = pos["cost"] / pos["qty0"]
    if not (entry > 0 and qty > 0 and basis and basis > 0 and frac > 0):
        return None
    slip = float(PAPER.get("slippage_pct") or 0.0)
    if not (0 <= slip < 1):
        return None
    q = qty * min(frac, 1.0)
    fee = _sell_fee_usd(q * basis)
    return ((basis + fee / q) / (1 - slip)) / entry


def effective_ladder(pos: dict, pr: dict) -> tuple[float, float]:
    """TP1/TP2 after the cost floor: a take-profit may never book a loss."""
    tp1, tp2 = float(pr["tp1_at"]), float(pr["tp2_at"])
    if not EXITS.get("tp_cost_floor", True) or pos.get("stage", 0) >= 1:
        return tp1, tp2
    be = net_breakeven_mult(pos, float(pr.get("tp1_frac") or 1.0))
    if be is None:
        return tp1, tp2
    floor = be * (1 + float(EXITS.get("tp_min_net_edge", 0.01) or 0.0))
    if floor > tp1:
        gap = max(tp2 - tp1, 0.0)
        tp1 = round(floor, 4)
        tp2 = max(tp2, round(tp1 + gap, 4))
    return tp1, tp2


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
        # A stop is a promise, not a hope. Fill at the stop level, not at whatever
        # the next print happens to be: on a memecoin one tick can gap far past the
        # stop (a rug), and filling there turns a 1.5%-risk trade into a 20%-of-book
        # loss. That is exactly how a desk advertising a 3% daily stop ended a day
        # at -80%. EXITS["stop_fill_at_stop"] = False restores raw market fills.
        mode = str(EXITS.get("stop_fill_mode")
                   or ("stop" if EXITS.get("stop_fill_at_stop", True) else "market")).lower()
        if mode == "stop":
            pos["fill_at"] = pos["stop_price"]
        elif mode == "realistic":
            # Past the stop like a real market sell, but capped: a gap print far
            # under the stop is mostly quote lag, not the price a 15s seller gets.
            cap = float(EXITS.get("stop_gap_cap_pct", 0.05) or 0.0)
            pos["fill_at"] = min(pos["stop_price"], max(price, pos["stop_price"] * (1 - cap)))
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
    if pos["stage"] == 0:
        tp1_at, tp2_at = effective_ladder(pos, pr)
        if (tp1_at, tp2_at) != (pr["tp1_at"], pr["tp2_at"]):
            # Remembered so TP2 uses the same floored ladder after TP1 fires.
            pos["tp1_eff"], pos["tp2_eff"] = tp1_at, tp2_at
    else:
        tp1_at = pos.get("tp1_eff", pr["tp1_at"])
        tp2_at = pos.get("tp2_eff", pr["tp2_at"])
    if pos["stage"] == 1 and price >= entry * tp2_at:
        pos["stage"] = 2
        return (pr["tp2_frac"], f"TP2 at {tp2_at:g}x")
    if pos["stage"] == 0 and price >= entry * tp1_at:
        pos["stage"] = 1
        pos["half_taken"] = True
        pos["half_at"] = now
        if EXITS.get("breakeven_after_half", True):
            be_px = entry
            if EXITS.get("cost_aware_breakeven", True):
                # The runner is what is left AFTER this TP1 sell.
                runner = dict(pos, qty=(pos.get("qty") or 0.0) * (1 - min(pr["tp1_frac"], 1.0)))
                m = net_breakeven_mult(runner, 1.0)
                if m:
                    be_px = entry * m * 1.002     # a hair above zero so it books green
            pos["stop_price"] = max(pos["stop_price"], be_px)     # only ever up
        floored = "" if tp1_at == pr["tp1_at"] else f" (cost floor; set {pr['tp1_at']:g}x)"
        return (pr["tp1_frac"], f"TP1 at {tp1_at:g}x{floored}")
    if held_min >= EXITS["volume_decay_min_minutes"] and q.get("volume_h24", 0) > 0:
        ratio = q.get("volume_h6", 0) / (q["volume_h24"] / 4)
        if ratio < EXITS["volume_decay_ratio"]:
            return (1.0, f"volume decay {ratio:.2f}")
    # Minute-level clock for a true scalp. A profile may set `timeout_minutes`
    # and/or `max_hold_minutes`; when present they win over the hour keys, so a
    # 1-10 minute strategy never has to express its clock in fractions of an hour.
    timeout_min = pr.get("timeout_minutes")
    if timeout_min is None:
        timeout_min = pr["timeout_hours"] * 60
    max_hold_min = pr.get("max_hold_minutes")
    if max_hold_min is None:
        max_hold_min = pr["max_hold_hours"] * 60
    if held_min >= timeout_min and price < entry * (1 + pr["timeout_min_gain"]):
        label = f"{int(timeout_min)}m" if pr.get("timeout_minutes") is not None else f"{pr['timeout_hours']:g}h"
        return (1.0, f"timeout {label} under +{int(pr['timeout_min_gain'] * 100)}%")
    if held_min >= max_hold_min:
        return (1.0, "max hold time")
    return None
