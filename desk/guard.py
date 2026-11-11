"""Discipline layer: pre-trade checklist, drawdown breaker, post-trade review.

Pure functions, no I/O, no model. Everything here can only make the paper desk
MORE careful: it adds blocks, size cuts and notes. It never loosens a gate, a
limit or a pause, and it never touches LIVE.

Ideas ported (MIT sources, rewritten for a paper memecoin desk; see docs/playbook.md):
  galleonlabs/hypergrok-trading-desk   desk-risk-limits, desk-trade-lifecycle,
                                       desk-post-trade-review, desk-monitoring
  tradermonty/claude-trading-skills    pre-trade-discipline-gate,
                                       drawdown-circuit-breaker, position-sizer,
                                       signal-postmortem, trade-performance-coach
"""
from __future__ import annotations

import time

# ---- circuit breaker (extra to the 3% daily-loss halt already in risk.py) ----
BREAKER = {
    "reduce_day_loss_pct": 0.02,     # day P&L <= -2% of start-of-day equity -> half size
    "streak_reduce": 2,              # 2 losses in a row -> half size
    "streak_pause": 3,               # 3 losses in a row -> pause new entries
    "cooldown_s": 30 * 60,           # ... for 30 minutes after the last loss
}
MIN_SIZE_MULT = 0.25                 # stacked cuts never go below a quarter


def loss_streak(closed: list[dict]) -> tuple[int, float | None]:
    """(consecutive losses at the end of the closed list, time of the last one).
    `closed` is oldest-first, as stored in the paper ledger."""
    n, last = 0, None
    for c in reversed(closed or []):
        pnl = c.get("pnl_usd")
        if pnl is None:
            continue
        if pnl < 0:
            n += 1
            last = last if last is not None else c.get("closed")
        else:
            break
    return n, last


def circuit_breaker(closed: list[dict], day_pnl: float, day_start: float,
                    now: float | None = None) -> dict:
    """{"state": ok|reduce|cooldown, "mult": 1|0.5|0, "reason": str}. Tighten-only."""
    now = time.time() if now is None else now
    streak, last = loss_streak(closed)
    if streak >= BREAKER["streak_pause"] and last is not None \
            and now - last < BREAKER["cooldown_s"]:
        left = int((BREAKER["cooldown_s"] - (now - last)) // 60) + 1
        return {"state": "cooldown", "mult": 0.0,
                "reason": f"{streak} losses in a row, new entries paused ~{left} more min"}
    reasons = []
    if day_start > 0 and day_pnl <= -BREAKER["reduce_day_loss_pct"] * day_start:
        reasons.append(f"day P&L ${day_pnl:.2f} past -{BREAKER['reduce_day_loss_pct']:.0%}")
    if streak >= BREAKER["streak_reduce"]:
        reasons.append(f"{streak} losses in a row")
    if reasons:
        return {"state": "reduce", "mult": 0.5, "reason": "; ".join(reasons) + ", half size"}
    return {"state": "ok", "mult": 1.0, "reason": "ok"}


def size_multiplier(cuts: list[tuple[str, float]]) -> tuple[float, list[str]]:
    """Combine named cuts (each 0 < m <= 1). Returns (mult, names). Never above 1."""
    mult, names = 1.0, []
    for name, m in cuts:
        if m < 1.0:
            mult *= max(0.0, m)
            names.append(name)
    if mult <= 0:
        return 0.0, names
    return max(MIN_SIZE_MULT, round(mult, 4)), names


def pretrade_checklist(*, stop_pct: float | None, ticket: float, max_ticket: float,
                       breaker: dict, block_flags: list[str], quote_confirmed: bool | None,
                       paper_only: bool = True) -> dict:
    """The gate before a fill: every line must be true. `quote_confirmed` None = not yet run."""
    items = [
        ("plan: stop and horizon set", bool(stop_pct and stop_pct > 0)),
        ("size within the per-trade cap", ticket <= max_ticket + 1e-9),
        ("breaker not tripped", breaker.get("state") != "cooldown"),
        ("no security block flags", not block_flags),
        ("paper mode", bool(paper_only)),
    ]
    if quote_confirmed is not None:
        items.append(("price confirmed on a second read", bool(quote_confirmed)))
    failed = [name for name, ok in items if not ok]
    return {"items": [{"name": n, "ok": ok} for n, ok in items],
            "passed": sum(1 for _, ok in items if ok), "total": len(items),
            "ok": not failed, "failed": failed}


def review_exit(*, symbol: str, reason: str, entry: float | None, exit_price: float | None,
                peak: float | None, held_s: float | None, pnl_usd: float | None,
                horizon: str | None, flags: list[str] | None, regime: str | None) -> dict:
    """One-line post-trade note, in the lifecycle's review shape: what happened, why, lesson."""
    mult = (exit_price / entry) if entry and exit_price else None
    mfe = (peak / entry - 1) if entry and peak else None      # best unrealised move
    held = "?" if held_s is None else (f"{int(held_s // 60)}m" if held_s < 7200
                                       else f"{held_s / 3600:.1f}h")
    won = pnl_usd is not None and pnl_usd > 0
    lesson = ""
    if won and mfe is not None and mult is not None and mfe > 0 and mult < 1 + mfe * 0.5:
        lesson = "gave back over half the peak gain: consider a tighter trail"
    elif not won and mfe is not None and mfe >= 0.30:
        lesson = "was up 30%+ and still lost: take-profit rule was not reached"
    elif not won and "stop" in (reason or "") and held_s is not None and held_s < 300:
        lesson = "stopped inside 5 min: entry may have been a local top"
    elif flags:
        lesson = "had entry flags (" + ", ".join(flags[:3]) + "): compare flagged vs clean trades"
    note = (f"{symbol}: {'win' if won else 'loss'} "
            f"{'' if pnl_usd is None else f'${pnl_usd:+.2f}'} via {reason}, held {held}"
            f"{'' if mult is None else f', {mult:.2f}x'}"
            f"{'' if mfe is None else f', peak {mfe:+.0%}'}"
            f"{'' if not regime or regime == 'UNKNOWN' else f', regime {regime}'}")
    return {"symbol": symbol, "won": won, "pnl_usd": pnl_usd, "reason": reason,
            "mult": None if mult is None else round(mult, 3),
            "peak_gain": None if mfe is None else round(mfe, 3), "held_s": held_s,
            "horizon": horizon, "flags": list(flags or []), "regime": regime,
            "lesson": lesson, "note": note + (f" | {lesson}" if lesson else "")}


def coach(reviews: list[dict]) -> dict:
    """Advisory next-session notes from recent exits. Never changes a setting."""
    n = len(reviews)
    if n < 5:
        return {"n": n, "rules": [], "summary": f"{n} exits so far, need 5 for a review"}
    wins = [r for r in reviews if r.get("won")]
    flagged = [r for r in reviews if r.get("flags")]
    stops = [r for r in reviews if "stop" in (r.get("reason") or "")]
    rules = []
    if len(stops) / n >= 0.6:
        rules.append("most exits are stops: entries may be late, review the entry filters")
    if flagged and len([r for r in flagged if not r.get("won")]) / len(flagged) >= 0.7:
        rules.append("flagged entries mostly lose: consider blocking those flags")
    gave_back = [r for r in reviews if "tighter trail" in (r.get("lesson") or "")]
    if len(gave_back) >= 2:
        rules.append("winners give back gains: review trail settings")
    return {"n": n, "win_rate": round(len(wins) / n, 2), "rules": rules,
            "summary": f"{len(wins)}/{n} wins; advisory only"}
