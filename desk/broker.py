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

from config import EXITS, PAPER  # noqa: F401
from desk import exits

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

    def realized_equity(self) -> float:
        """Starting bankroll + closed-trade P&L. Unrealized gains are not sized against (compounding)."""
        return round(PAPER["starting_equity_usd"] + sum(c.get("pnl_usd", 0.0) for c in self.s["closed"]), 2)

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
    def buy(self, mint: str, symbol: str, ticket_usd: float, price: float,
            horizon: str = "swing") -> dict:
        fill = price * (1 + PAPER["slippage_pct"])
        fee = ticket_usd * PAPER["fee_pct"]
        qty = (ticket_usd - fee) / fill
        self.s["cash"] -= ticket_usd
        self.s["positions"][mint] = {"symbol": symbol, "qty": qty, "entry": fill,
                                     "last": fill, "opened": time.time(),
                                     "cost": ticket_usd, "half_taken": False}
        exits.init_state(self.s["positions"][mint], horizon)
        self._save()
        return {"side": "buy", "mint": mint, "symbol": symbol, "qty": qty, "fill": fill,
                "fee": fee, "ticket_usd": ticket_usd, "horizon": horizon}

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
        return exits.evaluate(p, q, now or time.time())

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
                 "opened": p["opened"], "half_taken": p["half_taken"],
                 "horizon": p.get("horizon"), "stage": p.get("stage", 0),
                 "stop_price": p.get("stop_price"), "peak": p.get("peak")}
                for m, p in self.s["positions"].items()
            ],
            "closed": list(reversed(self.s["closed"][-30:])),
        }
