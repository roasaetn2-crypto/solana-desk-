"""Preset A/B measurement: which shipped package actually wins on YOUR fills?

WR-60 and WR-65 were measured on historical candles. That is a backtest, and a
backtest is a hypothesis. This module makes the live paper desk answer the
question directly: every BUY is tagged with the preset that was active when it
was taken, and every close journals a `preset_pnl` row carrying that tag.

Nothing here changes a trade. It is a label, exactly like the pullback shadow
tag -- the only thing it can do is let scripts/preset_ab_report.py compare two
groups of real, fully-gated, fully-costed paper trades after enough of them.

Tag values:
    "wr60"  -> 15% pullback, single TP 1.15 all, 10% trail, 30% stop, no vol gate
    "wr65"  -> same, plus entry-bar volume >= 1.5x the 20-bar average
    "other" -> anything else the operator has dialled in by hand

The tag is derived from config, not stored state, so it is always truthful about
what the desk was actually doing at entry time.
"""
from __future__ import annotations

from config import EXITS, MEME

_EPS = 1e-9


def _close(a, b, eps: float = _EPS) -> bool:
    try:
        return abs(float(a) - float(b)) <= eps
    except (TypeError, ValueError):
        return False


def active_preset() -> str:
    """Classify the live config as wr60 / wr65 / pf11 / other.

    Only the levers that distinguish the packages are checked. If the operator
    has changed anything else that matters, it is honestly reported as "other"
    so the A/B never silently compares apples to oranges.
    """
    single = EXITS.get("single") or {}
    scalp = (EXITS.get("profiles") or {}).get("scalp", {}) or {}
    plan = str(EXITS.get("take_profit_mode") or "profile")

    if not MEME.get("pullback_entry"):
        return "other"

    # ---- pf11: deep dip, big target, volume gate, patient ladder ----
    if (_close(MEME.get("pullback_min_drop"), 0.25) and plan == "profile"
            and _close(scalp.get("stop_pct"), 0.30) and MEME.get("vol_confirm")
            and _close(MEME.get("vol_confirm_k"), 1.5)):
        try:
            from desk import meme_tactics
            if meme_tactics.current_ladder() == "patient":
                return "pf11"
        except Exception:
            pass
        return "other"

    # ---- wr60 / wr65: 15% dip, single 1.15 TP, 30% stop ----
    if not _close(MEME.get("pullback_min_drop"), 0.15):
        return "other"
    if plan != "single":
        return "other"
    if not (_close(single.get("tp_at"), 1.15) and _close(single.get("tp_frac"), 1.0)
            and _close(single.get("trail_pct"), 0.10)):
        return "other"
    if not _close(scalp.get("stop_pct"), 0.30):
        return "other"

    if MEME.get("vol_confirm"):
        if _close(MEME.get("vol_confirm_k"), 1.5):
            return "wr65"
        return "other"          # volume gate at a non-shipped k
    return "wr60"


def tag() -> dict:
    """The meta fragment to attach to a BUY so the close path can journal it."""
    return {"preset": active_preset()}
