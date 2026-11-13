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

from config import EXITS, PAPER, RISK  # noqa: F401
from desk import exits
from desk.ops import data_file

LEDGER = Path("paper_ledger.json")


class PaperBroker:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else data_file("paper_ledger.json")
        if self.path.exists():
            self.s = json.loads(self.path.read_text())
            # A ledger written before `start_equity` existed (or written when the
            # bankroll was a different number) must not silently re-base itself on
            # whatever config.py says today. Sizing would use the new number while
            # `cash` still held the old one, and the book would spend money it
            # never had. Keep the ledger's own basis and say so once.
            if "start_equity" not in self.s:
                implied = round(self.s.get("cash", 0.0) + sum(
                    p.get("cost", 0.0) for p in self.s.get("positions", {}).values()), 2)
                self.s["start_equity"] = implied
                if abs(implied - PAPER["starting_equity_usd"]) > 0.01:
                    print(f"[desk] paper ledger predates start_equity: keeping its own basis "
                          f"${implied:.2f} (config says ${PAPER['starting_equity_usd']:.2f}). "
                          f"Tap Reset to re-base the book.")
                self._save()
        else:
            eq = PAPER["starting_equity_usd"]
            self.s = {"cash": eq, "positions": {}, "day": str(date.today()), "day_start": eq,
                      "start_equity": eq, "closed": []}
        self._repair()
        self._roll_day()

    # ---------- state ----------
    def _repair(self) -> list[dict]:
        """Heal a ledger a bad quote already corrupted, and say what was fixed.

        Two invariants are exact in this broker, so a breach is corruption and
        not a trading result:

          cash = start + sum(closed pnl) + sum(open proceeds - open cost)
          cost * min_mark_mult <= qty * last <= cost * max_mark_mult

        The first one is just double-entry: every buy takes `cost` out of cash and
        every sell puts `proceeds` back. The second one is the band a real
        memecoin cannot leave. Repairing here (rather than only guarding new
        writes) means a book that is already broken comes back sane on the next
        boot instead of needing a Reset that throws away the trade history.
        """
        fixes: list[dict] = []
        start = self.s.get("start_equity", PAPER["starting_equity_usd"])
        hi = float(PAPER.get("max_mark_mult", 1000.0))
        lo = float(PAPER.get("min_mark_mult", 0.001))

        # 1. a mark that cannot be a price goes back to the entry price
        for mint, p in list(self.s["positions"].items()):
            entry = p.get("entry") or 0.0
            qty = p.get("qty") or 0.0
            cost = p.get("cost") or 0.0
            last = p.get("last") or 0.0
            bad = (not entry or not qty or last <= 0
                   or not (entry * lo <= last <= entry * hi)
                   or not (cost * lo <= qty * last <= cost * hi))
            if bad:
                p["last"] = entry
                if p.get("peak") and not (entry * lo <= p["peak"] <= entry * hi):
                    p["peak"] = entry
                if p.get("stop_price") and not (0 < p["stop_price"] <= entry):
                    p["stop_price"] = entry * (1 - (p.get("stop_pct") or 0.3))
                fixes.append({"kind": "mark", "mint": mint, "symbol": p.get("symbol"),
                              "was": last, "now": entry})

        # 2. a closed trade cannot have made more than the whole book could hold.
        #    The largest honest single trade is max_position_pct of the book, up
        #    max_mark_mult times. Anything past that is a bad fill, not a win, so
        #    the row is dropped instead of being clamped to a fake profit.
        cap = abs(start) * hi * float(RISK.get("max_position_pct", 0.25))
        if cap > 0 and self.s.get("closed"):
            keep = []
            for c in self.s["closed"]:
                pnl = c.get("pnl_usd")
                if pnl is not None and abs(pnl) > cap:
                    fixes.append({"kind": "closed_drop", "mint": c.get("mint"),
                                  "symbol": c.get("symbol"), "was": pnl})
                    continue
                keep.append(c)
            self.s["closed"] = keep

        # 3. cash must equal the double-entry identity
        realized = sum(c.get("pnl_usd", 0.0) for c in self.s.get("closed", []))
        open_cost = sum(p.get("cost", 0.0) for p in self.s["positions"].values())
        open_proceeds = sum(p.get("proceeds", 0.0) for p in self.s["positions"].values())
        expected = start + realized + open_proceeds - open_cost
        cash = self.s.get("cash", 0.0)
        # closed P&L is rounded to cents, so allow a cent per closed trade
        tol = 0.02 + 0.01 * len(self.s.get("closed", []))
        if not (cash == cash) or abs(cash - expected) > tol:
            fixes.append({"kind": "cash", "was": cash, "now": round(expected, 8)})
            self.s["cash"] = expected

        if fixes:
            self._save()
            try:
                from desk.ops import journal
                journal("ledger_repair", fixes=fixes, equity=self.equity())
            except Exception:
                pass
            print(f"[desk] ledger repaired: {len(fixes)} corrupt value(s) reset "
                  f"(equity now ${self.equity():,.2f})")
        return fixes
    def _save(self):
        self.path.write_text(json.dumps(self.s, indent=1))
        # paper book only; the live ledger is never pushed here
        if self.path.name == "paper_ledger.json" or self.path.name.endswith("paper_ledger.json"):
            try:
                from desk.store import sync_kv_file
                sync_kv_file("paper_ledger", self.path)
            except Exception:
                pass

    def _roll_day(self):
        if self.s["day"] != str(date.today()):
            self.s["day"], self.s["day_start"] = str(date.today()), self.equity()
            self._save()

    def _value(self, p: dict, price: float | None = None) -> float:
        """What one position is worth, clamped to a sane band around its cost.

        `last` is written by the quote loop, so a single corrupt print (wrong
        pool, decimals mismatch, hand-edited ledger) used to reprice the whole
        book: on 2026-11-14 one bad mark showed $549,195,436 on a $160 bankroll
        (3,432,471x). A mark outside [cost * min_mark_mult, cost * max_mark_mult]
        is not a price, it is a bug, so the position is valued at what it cost
        and `clamped()` reports it instead of letting it through silently.
        """
        qty = p.get("qty") or 0.0
        cost = p.get("cost") or 0.0
        px = p.get("last") if price is None else price
        value = qty * (px or 0.0)
        if not (value == value) or value in (float("inf"), float("-inf")):   # NaN / inf
            return cost
        hi = cost * float(PAPER.get("max_mark_mult", 1000.0))
        lo = cost * float(PAPER.get("min_mark_mult", 0.001))
        if cost and not (lo <= value <= hi):
            return cost
        return value

    def clamped(self) -> list[dict]:
        """Positions whose mark is outside the sane band (empty on a clean book)."""
        out = []
        for m, p in self.s["positions"].items():
            raw = (p.get("qty") or 0.0) * (p.get("last") or 0.0)
            if self._value(p) != raw:
                out.append({"mint": m, "symbol": p.get("symbol"), "last": p.get("last"),
                            "entry": p.get("entry"), "value": round(raw, 2),
                            "cost": p.get("cost")})
        return out

    def equity(self, marks: dict[str, float] | None = None) -> float:
        marks = marks or {}
        held = sum(self._value(p, marks.get(m, p["last"])) for m, p in self.s["positions"].items())
        return round(self.s["cash"] + held, 2)

    def realized_equity(self) -> float:
        """Starting bankroll + closed-trade P&L. Unrealized gains are not sized against (compounding).

        The basis is the LEDGER's start_equity, not config.PAPER. A config change
        must never re-base a book that already has history.
        """
        start = self.s.get("start_equity", PAPER["starting_equity_usd"])
        return round(start + sum(c.get("pnl_usd", 0.0) for c in self.s["closed"]), 2)

    def day_pnl(self) -> float:
        return round(self.equity() - self.s["day_start"], 2)

    def open_count(self) -> int:
        return len(self.s["positions"])

    def reset(self):
        eq = PAPER["starting_equity_usd"]
        self.s = {"cash": eq, "positions": {}, "day": str(date.today()), "day_start": eq,
                  "start_equity": eq, "closed": []}
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

    def _position_row(self, mint: str, p: dict) -> dict:
        """One position for the dashboard, using the SAME clamped mark as equity.

        `last` is written by the quote loop and can be corrupt (a decimals
        mismatch, a different pool for the same mint, a hand-edited ledger). The
        equity already refuses to value such a mark (`_value`), but this row used
        to hand the raw `last` to the UI and compute `pnl_pct` from it, so a
        position bought at 8.85e-7 and marked at 284.67 showed
        +32,156,727,091% on the card while the book itself stayed sane. The card
        must never disagree with the equity, so the mark here is the clamped one.
        `raw_last` and `mark_clamped` are kept so the corruption stays visible
        instead of silently disappearing.
        """
        qty = p.get("qty") or 0.0
        cost = p.get("cost") or 0.0
        entry = p.get("entry") or 0.0
        raw_last = p.get("last") or 0.0
        value = self._value(p)                       # clamped, same as equity()
        eff_price = (value / qty) if qty else raw_last
        clamped = abs(value - qty * raw_last) > 1e-9
        pnl_pct = round((eff_price / entry - 1) * 100, 2) if entry else 0.0
        return {"mint": mint, "symbol": p.get("symbol"), "qty": qty, "entry": entry,
                "last": eff_price, "raw_last": raw_last, "mark_clamped": clamped,
                "value": round(value, 2), "cost": cost, "pnl_pct": pnl_pct,
                "opened": p.get("opened"), "half_taken": p.get("half_taken"),
                "horizon": p.get("horizon"), "stage": p.get("stage", 0),
                "stop_price": p.get("stop_price"), "peak": p.get("peak")}

    def snapshot(self) -> dict:
        return {
            "cash": round(self.s["cash"], 2),
            "equity": self.equity(),
            "day_pnl": self.day_pnl(),
            # Equity when the day rolled. The day P&L percentage must divide by
            # this, not by the bankroll: after a day of trading the two differ,
            # and dividing by the start bankroll overstated the daily move.
            "day_start": round(self.s.get("day_start", self.s.get("start_equity", 0.0)), 2),
            "open_count": self.open_count(),
            "clamped_marks": self.clamped(),
            "positions": [self._position_row(m, p) for m, p in self.s["positions"].items()],
            "closed": list(reversed(self.s["closed"][-30:])),
        }
