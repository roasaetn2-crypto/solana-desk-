"""Token age and contract-address display helpers. View-only, no trading logic."""
from __future__ import annotations

import time


def format_age(seconds: float | int | None) -> str:
    """Human age since pool/pair creation: 3m, 2h 14m, 5d."""
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    minutes = s // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        rem = minutes % 60
        return f"{hours}h {rem}m" if rem else f"{hours}h"
    days = hours // 24
    return f"{days}d"


def short_ca(mint: str | None) -> str:
    """Shorten a mint to AbCd…WxYz (first 4, last 4)."""
    if not mint:
        return ""
    if len(mint) <= 8:
        return mint
    return f"{mint[:4]}…{mint[-4:]}"


def created_at_of(token: dict | None, now: float | None = None) -> float | None:
    """Unix seconds of pool/pair creation, or a fallback from age_minutes."""
    if not token:
        return None
    raw = token.get("created_at")
    if raw is not None:
        try:
            val = float(raw)
            return val if val > 0 else None
        except (TypeError, ValueError):
            pass
    age = token.get("age_minutes")
    if age is None:
        return None
    try:
        return (now if now is not None else time.time()) - float(age) * 60.0
    except (TypeError, ValueError):
        return None
