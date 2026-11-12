"""Seat 5a: execution. Paper.

This file never touches a key and never signs anything. It fills against the
quoted price with a modelled fee and slippage, and writes a JSON ledger. That is
the whole point: it is the mode the desk boots into, and it cannot lose money.

Live execution lives in desk/live.py and is a subclass of this broker, so the
ledger shape, the exit rules and the dashboard are identical. If you are looking
for the signing code, it is there, and it is off by default.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from config import EXITS, PAPER

LEDGER = Path("paper_ledger.json")


class PaperBroker:
    def __init__(self, path: Path | str = LEDGER):
        self.path = Path(path)
        if self.path.exists():
            self.s = json.loads(self.path.read_text())
        else:
            eq = PAPER["starting_equity_usd"]
            self.s = {"cash": eq, "positions": {}, "day": str(date.today()), "day_start": eq,
                      "closed": []}
        self._roll_day()

    # ---------- state ----------
    def _save(self):
        self.path.write_text(json.dumps(self.s, indent=1))

    def _roll_day(self):
        if self.s["day"] != str(date.today()):
            self.s["day"], self.s["day_start"] = str(date.today()), self.equity()
            self._save()

    def equity(self, marks: dict[str, float] | None = None) -> float:
        marks = marks or {}
        held = sum(p["qty"] * marks.get(m, p["last"]) for m, p in self.s["positions"].items())
        return round(self.s["cash"] + held, 2)

    def day_pnl(self) -> float:
        return round(self.equity() - self.s["day_start"], 2)

    def open_count(self) -> int:
        return len(self.s["positions"])

    def reset(self):
        eq = PAPER["starting_equity_usd"]
        self.s = {"cash": eq, "positions": {}, "day": str(date.today()), "day_start": eq,
                  "closed": []}
        self._save()

    # ---------- orders ----------
    def buy(self, mint: str, symbol: str, ticket_usd: float, price: float) -> dict:
        fill = price * (1 + PAPER["slippage_pct"])
        fee = ticket_usd * PAPER["fee_pct"]
        qty = (ticket_usd - fee) / fill
        self.s["cash"] -= ticket_usd
        self.s["positions"][mint] = {"symbol": symbol, "qty": qty, "entry": fill,
                                     "last": fill, "opened": time.time(),
                                     "cost": ticket_usd, "half_taken": False}
        self._save()
        return {"side": "buy", "mint": mint, "symbol": symbol, "qty": qty, "fill": fill,
                "fee": fee, "ticket_usd": ticket_usd}

    def sell(self, mint: str, fraction: float, price: float, reason: str) -> dict:
        p = self.s["positions"][mint]
        qty = p["qty"] * fraction
        fill = price * (1 - PAPER["slippage_pct"])
        gross = qty * fill
        fee = gross * PAPER["fee_pct"]
        self.s["cash"] += gross - fee
        p["qty"] -= qty
        p["proceeds"] = p.get("proceeds", 0.0) + gross - fee
        out = {"side": "sell", "mint": mint, "symbol": p["symbol"], "qty": qty,
               "fill": fill, "fee": fee, "reason": reason}
        if fraction >= 1 or p["qty"] <= 1e-12:
            pnl = round(p["proceeds"] - p["cost"], 2)
            out["pnl_usd"] = pnl
            self.s["closed"].append({"mint": mint, "symbol": p["symbol"], "pnl_usd": pnl,
                                     "reason": reason, "closed": time.time()})
            del self.s["positions"][mint]
        self._save()
        return out

    # ---------- exits: pure rules, applied every cycle ----------
    def exit_signal(self, mint: str, q: dict | None, now: float | None = None) -> tuple[float, str] | None:
        """Return (fraction_to_sell, reason) or None. No model, no conversation."""
        sig = self._exit_signal(mint, q, now)
        self._save()
        return sig

    def _exit_signal(self, mint, q, now):
        p = self.s["positions"][mint]
        now = now or time.time()
        held_min = (now - p["opened"]) / 60
        if q is None:
            # A missing quote is usually a transient API failure, not a dead token.
            # The old code liquidated on the first miss, so a DexScreener hiccup
            # closed good positions at a loss. Tolerate misses; only give up when
            # they persist or the hold limit is reached.
            p["no_quote"] = p.get("no_quote", 0) + 1
            if p["no_quote"] >= EXITS.get("max_no_quote_cycles", 3):
                return (1.0, f"no quote after {p['no_quote']} cycles, close")
            if held_min >= EXITS["max_hold_hours"] * 60:
                return (1.0, "max hold time (no quote)")
            return None
        p["no_quote"] = 0
        price = q["price_usd"]
        p["last"] = price
        p["peak"] = max(p.get("peak", price), price)
        if price <= p["entry"] * (1 - EXITS["stop_pct"]):
            return (1.0, f"stop -{int(EXITS['stop_pct'] * 100)}%")
        if not p["half_taken"] and price >= p["entry"] * EXITS["take_half_at"]:
            p["half_taken"] = True
            return (0.5, f"take half at {EXITS['take_half_at']}x")
        # --- runner management ---
        # Selling half does NOT free the slot: the other half stays open. The old
        # code had no rule for it, so it sat there until max_hold_hours (24h) and
        # every slot stayed full -- the desk then rejected every new setup with
        # "max open positions". These three rules recycle the slot in minutes.
        if p["half_taken"]:
            if EXITS.get("breakeven_after_half", True) and price <= p["entry"]:
                return (1.0, "runner back to breakeven")
            trail = EXITS.get("runner_trail_pct")
            if trail and price <= p["peak"] * (1 - trail):
                return (1.0, f"runner trail -{int(trail * 100)}% from peak")
            if held_min >= EXITS.get("runner_max_minutes", 180):
                return (1.0, "runner time limit")
        if held_min >= EXITS["volume_decay_min_minutes"] and q.get("volume_h24", 0) > 0:
            ratio = q["volume_h6"] / (q["volume_h24"] / 4)
            if ratio < EXITS["volume_decay_ratio"]:
                return (1.0, f"volume decay {ratio:.2f}")
        if held_min >= EXITS["max_hold_hours"] * 60:
            return (1.0, "max hold time")
        return None

    def snapshot(self) -> dict:
        return {
            "cash": round(self.s["cash"], 2),
            "equity": self.equity(),
            "day_pnl": self.day_pnl(),
            "open_count": self.open_count(),
            "positions": [
                {"mint": m, "symbol": p["symbol"], "qty": p["qty"], "entry": p["entry"],
                 "last": p["last"], "cost": p["cost"],
                 "pnl_pct": round((p["last"] / p["entry"] - 1) * 100, 2),
                 "opened": p["opened"], "half_taken": p["half_taken"]}
                for m, p in self.s["positions"].items()
            ],
            "closed": list(reversed(self.s["closed"][-30:])),
        }
        
