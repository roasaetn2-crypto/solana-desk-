"""Equity curve and trade marks -- persisted across restarts.

The curve used to be two in-memory deques, so every restart wiped the chart and
the 1H/4H/1D/1W/ALL tabs had nothing to replay. It is now mirrored to
`equity_curve.json` in the desk data dir and reloaded on boot.

Two rules are kept from the original module:

  1. Record only real snapshots; never invent a point.
  2. If a timeframe window is empty, return the history that exists rather than
     an empty chart (a $46 book that traded twice today has no 1W window, but it
     still has something honest to show).

The one addition that is *derived* rather than recorded is `backfill_from_ledger`:
when the store is missing (first boot after this change, or a wiped data dir) the
marks are rebuilt from the ledger's own `closed` rows and the open positions'
`opened` timestamps. Those are real fills, not guesses -- it is the same data the
journal already holds, just reshaped for the chart.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
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
MAX_POINTS = 20000
MAX_MARKS = 4000

STORE_NAME = "equity_curve.json"


def store_path() -> Path:
    """Honour DESK_DATA_DIR the same way desk.ops.data_file does."""
    raw = (os.environ.get("DESK_DATA_DIR") or "").strip()
    return (Path(raw) if raw else Path(".")) / STORE_NAME


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


# ---------------------------------------------------------------- persistence
def save(points: Iterable[dict], marks: Iterable[dict], path: Path | None = None) -> bool:
    """Write the curve atomically. Never raises: a chart must not kill the desk."""
    p = path or store_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps({
            "version": 1,
            "saved_at": time.time(),
            "points": list(points),
            "marks": list(marks),
        })
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(blob)
        os.replace(tmp, p)
    except Exception:
        return False
    _maybe_sync(p)
    return True


_SYNC_EVERY_S = 120.0
_last_sync = [0.0]


def _maybe_sync(path: Path) -> None:
    """Push to the remote KV store at most every 2 minutes.

    The curve is rewritten on every cycle, so an unthrottled push would be a
    network write per cycle. Throttled, a crash loses at most two minutes of
    chart, and the local file is always current.
    """
    now = time.time()
    if now - _last_sync[0] < _SYNC_EVERY_S:
        return
    _last_sync[0] = now
    try:
        from desk.store import sync_kv_file
        sync_kv_file("equity_curve", path)
    except Exception:
        pass


def load(points: deque, marks: deque, path: Path | None = None) -> int:
    """Fill the deques from disk. Returns how many points were restored."""
    p = path or store_path()
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text())
    except Exception:
        # a truncated write must not stop the boot; start clean instead
        return 0
    pts = data.get("points") or []
    mks = data.get("marks") or []
    for pt in pts:
        if isinstance(pt, dict) and "t" in pt and "equity" in pt:
            points.append(pt)
    for m in mks:
        if isinstance(m, dict) and "t" in m and "kind" in m:
            marks.append(m)
    _dedupe(points)
    _dedupe(marks)
    return len(points)


def _dedupe(series: deque) -> None:
    """Drop repeated (t, kind/symbol) rows so a double-save cannot double-draw."""
    seen = set()
    keep = []
    for row in series:
        key = (round(float(row.get("t", 0)), 3), row.get("kind"), row.get("symbol"))
        if key in seen:
            continue
        seen.add(key)
        keep.append(row)
    series.clear()
    for row in keep:
        series.append(row)


def backfill_from_ledger(state: dict) -> tuple[list[dict], list[dict]]:
    """Rebuild a chart history from the ledger's own fills.

    Used only when the store is empty, so an existing book is not shown as a
    blank chart after this change. Everything here comes from real rows:

      - each closed trade  -> a "sell" mark at its `closed` timestamp
      - each open position -> a "buy"  mark at its `opened` timestamp

    Equity points are the ledger basis plus the realised P&L accumulated up to
    each fill, which is exactly `realized_equity()` at that moment. No price is
    invented and no unrealised gain is back-projected (there is no history of
    marks to do that honestly).
    """
    closed = state.get("closed") or []
    positions = state.get("positions") or {}
    start = float(state.get("start_equity") or 0.0)

    marks: list[dict] = []
    for mint, p in positions.items():
        opened = p.get("opened")
        if opened:
            marks.append({
                "t": float(opened), "kind": "buy", "symbol": p.get("symbol") or "?",
                "usd": round(float(p.get("cost") or 0.0), 2),
                "mint": mint, "entry": p.get("entry"), "backfilled": True,
            })
    for c in closed:
        ts = c.get("closed")
        if not ts:
            continue
        marks.append({
            "t": float(ts), "kind": "sell", "symbol": c.get("symbol") or "?",
            "pnl_usd": c.get("pnl_usd"), "reason": c.get("reason"),
            "mint": c.get("mint"), "backfilled": True,
        })
    marks.sort(key=lambda m: m["t"])

    points: list[dict] = []
    if marks:
        points.append({"t": marks[0]["t"] - 1.0, "equity": round(start, 2), "backfilled": True})
        running = 0.0
        for m in marks:
            if m.get("kind") == "sell" and m.get("pnl_usd") is not None:
                running += float(m["pnl_usd"])
            points.append({"t": m["t"], "equity": round(start + running, 2), "backfilled": True})
    return points, marks
