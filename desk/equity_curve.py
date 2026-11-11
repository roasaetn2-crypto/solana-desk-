"""In-memory equity curve and trade marks. Record only real snapshots; never invent points."""
from __future__ import annotations

from collections import deque
from typing import Iterable

WINDOWS_S = {
    "LIVE": 15 * 60,
    "1H": 60 * 60,
    "4H": 4 * 60 * 60,
    "1D": 24 * 60 * 60,
    "1W": 7 * 24 * 60 * 60,
    "ALL": None,
}
TABS = ("LIVE", "1H", "4H", "1D", "1W", "ALL")
MAX_POINTS = 2000
MAX_MARKS = 200


def record_point(series: deque, t: float, equity: float, day_pnl: float | None = None) -> dict:
    pt = {"t": float(t), "equity": round(float(equity), 2)}
    if day_pnl is not None:
        pt["day_pnl"] = round(float(day_pnl), 2)
    series.append(pt)
    return pt


def record_mark(marks: deque, t: float, kind: str, symbol: str, **extra) -> dict:
    mark = {"t": float(t), "kind": kind, "symbol": symbol}
    for key in ("usd", "pnl_usd", "fraction", "reason", "mint", "entry", "fill", "mult", "mcap_usd"):
        if extra.get(key) is not None:
            mark[key] = extra[key]
    marks.append(mark)
    return mark


def window(points: Iterable[dict], marks: Iterable[dict], tf: str, now: float) -> tuple[list[dict], list[dict]]:
    """Filter to a timeframe. If the window is empty, return the history that exists."""
    pts = list(points)
    mks = list(marks)
    span = WINDOWS_S.get(tf, WINDOWS_S["ALL"])
    if span is None or not pts:
        return pts, mks
    cut = now - span
    w_pts = [p for p in pts if p["t"] >= cut]
    w_mks = [m for m in mks if m["t"] >= cut]
    if not w_pts:
        return pts, mks
    return w_pts, w_mks
