"""Control Tower — a read-only picture of the paper loop's one cursor.

The desk already runs in a single thread: Scanner, Analyst, Brain, Executor
and Risk never click at the same time, because the loop is the cursor. This
module only records that fact. It does not size, veto, fill, or reorder work.

A seat "holds" when it asks for the cursor while another seat still has it
checked out (Risk's stop patrol, which the loop runs before new buys). Risk
keeps the cursor; the ask is counted as a hold, and as risk-first when Risk
is the one holding or the one asking. Two holders at once would be a
collision. The recorder refuses that, so the collision count stays 0 unless
the single-holder invariant is actually broken.

Demo replay is off unless an operator turns it on. It never writes the book.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager

# One overnight watch, the same length as the reference night (22:00 → 06:30).
# The plane moves across the real process clock; the window rolls forward so a
# box that stays up for days still has a start and a landing.
WATCH_S = 8.5 * 3600

ROLES = ("scanner", "analyst", "brain", "executor", "risk")

# Risk outranks everyone. This is display priority for "who's next", matching
# the loop, which already runs stops before new entries. It does not reorder
# the loop.
PRIORITY = ("risk", "executor", "brain", "analyst", "scanner")

ROLE_META = {
    "scanner": {"name": "Scanner", "job": "finds coins", "code": "SCN",
                "color": "#3ddc97", "head": "market"},
    "analyst": {"name": "Analyst", "job": "takes coins apart", "code": "ANA",
                "color": "#c084fc", "head": "strategist"},
    "brain": {"name": "Brain", "job": "sizes the buy", "code": "BRN",
              "color": "#dbe7ff", "head": "robot"},
    "executor": {"name": "Executor", "job": "clicks Buy", "code": "EXE",
                 "color": "#ff9f43", "head": "executor"},
    "risk": {"name": "Risk", "job": "guards every stop", "code": "RSK",
             "color": "#ff6b9a", "head": "risk"},
}

_REJECT_ROLE = {
    "scanner": "scanner",
    "scan": "scanner",
    "risk": "risk",
    "signal": "risk",
    "confirm": "risk",
    "rulebook": "risk",
    "guard": "risk",
    "security": "risk",
}

# Reference-night replay. Fed through the same cursor recorder as live clicks,
# so holds, risk-first and collisions are computed, not typed in. Off by default.
DEMO_DURATION = 7.0
DEMO_EVENTS = (
    (0.0, "click", "scanner", "open coin page", {"symbol": "WASTED"}),
    (0.3, "click", "scanner", "open new pairs", {"symbol": "OGTA"}),
    (0.7, "click", "analyst", "takes coins apart",
     {"symbol": "WASTED", "mcap": 3_330_000, "top_wallet": 0.098,
      "top_10": 0.62, "holders": 842}),
    (1.1, "click", "analyst", "read top 10", {"symbol": "GATO", "mcap": 890_000}),
    (1.5, "click", "brain", "size $36.21",
     {"symbol": "WASTED", "usd": 36.21, "ok": True, "mint": "DEMOWASTED",
      "mcap": 3_330_000, "top_wallet": 0.098, "top_10": 0.62, "holders": 842}),
    (1.9, "checkout", "risk", "guards every stop", {"symbol": "WASTED"}),
    (2.3, "click", "analyst", "holders read", {"symbol": "WASTED"}),
    (2.7, "click", "executor", "click Buy $36.21",
     {"symbol": "WASTED", "usd": 36.21, "mint": "DEMOWASTED"}),
    (3.0, "release", "risk", "", {}),
    (3.4, "click", "executor", "click Sell 100%", {"symbol": "GATO", "usd": 27.64}),
    (3.8, "click", "risk", "check stop", {"symbol": "GATO"}),
    (4.2, "click", "brain", "size $27.64",
     {"symbol": "GATO", "usd": 27.64, "ok": True, "mint": "DEMOGATO", "mcap": 890_000}),
    (4.6, "checkout", "risk", "risk first on the stop", {"symbol": "GATO"}),
    (5.0, "click", "executor", "click Sell 50%", {"symbol": "WASTED"}),
    (5.3, "release", "risk", "", {}),
    (5.7, "click", "scanner", "open new pairs", {"symbol": "OGTA"}),
    (6.2, "click", "executor", "click Buy $15.09",
     {"symbol": "OGTA", "usd": 15.09, "mint": "DEMOOGTA", "mcap": 1_170_000}),
)
DEMO_POSITIONS = (
    {"symbol": "WASTED", "mint": "DEMOWASTED", "pnl_usd": 42.73, "pnl_pct": 118.0,
     "value": 78.94, "cost": 36.21, "mcap_usd": 3_330_000},
    {"symbol": "GATO", "mint": "DEMOGATO", "pnl_usd": -4.20, "pnl_pct": -11.6,
     "value": 32.01, "cost": 36.21, "mcap_usd": 890_000},
)
DEMO_END_EQUITY = 1029.94
DEMO_START_EQUITY = 30.0


def _sym(value) -> str:
    if not value:
        return ""
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if text.startswith("$"):
        text = text[1:]
    return text[:16]


def _clip(value, n: int = 96) -> str:
    text = " ".join(str(value or "").split())
    return text[:n]


class Tower:
    """In-memory cursor log. One instance per process; safe to call from the loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = {role: 0 for role in ROLES}
        self.clicks = {role: 0 for role in ROLES}
        self.holds = {role: 0 for role in ROLES}
        self.waits = {role: deque(maxlen=60) for role in ROLES}
        self.sparks = {role: deque([0.0] * 16, maxlen=16) for role in ROLES}
        self.actions = deque(maxlen=80)
        self.readback = deque(maxlen=60)
        self.last = {}
        self.holder: str | None = None
        self.holder_since = 0.0
        self.depth = 0
        self.waiters: set[str] = set()
        self.collisions = 0
        self.risk_first = 0
        self.sized: dict[str, bool] = {}
        self.focus: dict = {}
        self.facts: dict[str, dict] = {}
        self._open = 0
        self.demo_on = False
        self.demo_started = 0.0

    def reset(self) -> None:
        """Drop the feed. Does not turn demo on, and does not touch the book."""
        with self.lock:
            self.seq = {role: 0 for role in ROLES}
            self.clicks = {role: 0 for role in ROLES}
            self.holds = {role: 0 for role in ROLES}
            self.waits = {role: deque(maxlen=60) for role in ROLES}
            self.sparks = {role: deque([0.0] * 16, maxlen=16) for role in ROLES}
            self.actions.clear()
            self.readback.clear()
            self.last.clear()
            self.holder = None
            self.holder_since = 0.0
            self.depth = 0
            self.waiters = set()
            self.collisions = 0
            self.risk_first = 0
            self.sized.clear()
            self.focus = {}
            self.facts.clear()
            self._open = 0

    # -- cursor ----------------------------------------------------------
    def _set_open(self, n: int) -> None:
        """Single holder. A second grant is a collision and is refused."""
        if n > 1:
            self.collisions += 1
            n = 1
        self._open = n

    def _count_wait(self, role: str, now: float) -> None:
        self.holds[role] += 1
        waited = max(0.0, now - self.holder_since) if self.holder_since else 0.0
        self.waits[role].append(waited)
        self.waiters.add(role)
        risk_has_it = self.holder == "risk" or role == "risk"
        if risk_has_it:
            self.risk_first += 1
        if len(self.waiters) >= 2:
            names = " + ".join(ROLE_META[r]["name"] for r in PRIORITY if r in self.waiters)
            self.readback.append({
                "ts": now, "level": "conflict",
                "text": f"✗ CONFLICT · {names} want the cursor",
            })
        else:
            how = "RISK FIRST" if risk_has_it else "HOLD"
            self.readback.append({
                "ts": now, "level": "hold",
                "text": (f"{how} · {ROLE_META[role]['name']} waited on "
                         f"{ROLE_META[self.holder]['name']}"),
            })

    def click(self, role: str, text: str, *, checkout: bool = False,
              at: float | None = None, **meta) -> dict | None:
        """Record one seat action. checkout=True leaves the cursor with `role`."""
        if role not in ROLE_META:
            return None
        now = time.time() if at is None else float(at)
        with self.lock:
            if self.holder and self.holder != role:
                self._count_wait(role, now)
            if checkout:
                if self.holder == role:
                    self.depth += 1
                else:
                    # Handoff or first grant. Still one holder.
                    self.holder = role
                    self.holder_since = now
                    self.depth = 1
                    self.waiters = set()
                self._set_open(1)
            elif self.holder == role:
                pass
            else:
                # Atomic click: the cursor was free, or we only logged a wait
                # against whoever already holds it. Never a second grant.
                if not self.holder:
                    self._set_open(0)

            self.seq[role] += 1
            self.clicks[role] += 1
            n = self.seq[role]
            code = f"{ROLE_META[role]['code']}-{n:03d}" if n < 1000 else f"{ROLE_META[role]['code']}-{n}"
            symbol = _sym(meta.get("symbol"))
            mint = meta.get("mint") or ""
            verb = _clip(text, 80)
            for r in ROLES:
                prev = self.sparks[r][-1] if self.sparks[r] else 0.0
                self.sparks[r].append(round(prev * 0.45, 3))
            self.sparks[role][-1] = 1.0
            action = {
                "id": code, "role": role, "text": verb, "symbol": symbol,
                "mint": mint, "ts": now, "usd": meta.get("usd"),
                "color": ROLE_META[role]["color"],
            }
            self.actions.append(action)
            self.last[role] = action
            line = f"✓ {code} {verb}" + (f" · {symbol}" if symbol else "")
            self.readback.append({"ts": now, "level": "ok", "text": line})
            self._remember(meta, symbol, mint)
            if role == "brain" and mint:
                self.sized[mint] = bool(meta.get("ok"))
            elif role == "executor" and mint:
                self.sized.pop(mint, None)
            elif role == "risk" and mint and self.sized.pop(mint, False):
                self.risk_first += 1
                self.readback.append({
                    "ts": now, "level": "hold",
                    "text": f"RISK FIRST · veto before the buy · {symbol or mint[:6]}",
                })
            return action

    def _remember(self, meta: dict, symbol: str, mint: str) -> None:
        if not symbol and not mint:
            return
        focus = dict(self.focus)
        if symbol:
            focus["symbol"] = symbol
        if mint:
            focus["mint"] = mint
        slot = self.facts.setdefault(mint, {}) if mint else None
        for key in ("mcap", "top_wallet", "top_10", "holders", "price"):
            if meta.get(key) is not None:
                focus[key] = meta[key]
                if slot is not None:
                    slot[key] = meta[key]
        if slot is not None and symbol:
            slot["symbol"] = symbol
        self.focus = focus

    def release(self, role: str) -> None:
        with self.lock:
            if self.holder != role:
                return
            self.depth -= 1
            if self.depth <= 0:
                self.holder = None
                self.holder_since = 0.0
                self.depth = 0
                self.waiters = set()
                self._set_open(0)

    @contextmanager
    def holding(self, role: str, text: str, **meta):
        """Check the cursor out for one block of loop work. Always handed back."""
        self.click(role, text, checkout=True, **meta)
        try:
            yield
        finally:
            self.release(role)

    def observe(self, ev: dict) -> None:
        """Map an event the loop already emitted. Never raises into the loop."""
        try:
            self._observe(ev or {})
        except Exception:
            return

    def _observe(self, ev: dict) -> None:
        kind = ev.get("kind") or ""
        stage = ev.get("stage") or ""
        symbol = ev.get("symbol")
        mint = ev.get("mint")
        if kind == "scan":
            source = ev.get("source") or "feed"
            self.click("scanner", f"open new pairs · {source}", symbol=symbol, mint=mint)
            return
        if kind == "reject":
            role = _REJECT_ROLE.get(stage, "analyst")
            reason = ev.get("reason") or stage or "cut"
            self.click(role, f"cut {reason}", symbol=symbol, mint=mint)
            return
        if kind == "thesis":
            verdict = ev.get("verdict") or "thesis"
            score = ev.get("score")
            text = f"{verdict} {score}/10" if score is not None else str(verdict)
            self.click("analyst", text, symbol=symbol, mint=mint)
            return
        if kind == "signal":
            action = ev.get("action") or "signal"
            self.click("analyst", str(action).lower(), symbol=symbol, mint=mint)
            return
        if kind == "entry":
            usd = ev.get("ticket_usd")
            text = f"click Buy ${usd:.2f}" if isinstance(usd, (int, float)) else "click Buy"
            self.click("executor", text, symbol=symbol, mint=mint, usd=usd)
            return
        if kind == "exit":
            frac = ev.get("fraction")
            try:
                pct = int(round(float(frac) * 100)) if frac is not None else 100
            except (TypeError, ValueError):
                pct = 100
            why = _clip(ev.get("reason") or "", 40)
            text = f"click Sell {pct}%" + (f" · {why}" if why else "")
            self.click("executor", text, symbol=symbol, mint=mint,
                       usd=ev.get("pnl_usd"))
            return
        if kind == "info" and stage == "risk":
            self.click("risk", _clip(ev.get("message") or "risk note", 60),
                       symbol=symbol, mint=mint)
            return
        if kind == "warn" and "daily loss" in str(ev.get("message") or "").lower():
            self.click("risk", "daily stop", symbol=symbol, mint=mint)

    # -- demo ------------------------------------------------------------
    def set_demo(self, on: bool) -> bool:
        with self.lock:
            self.demo_on = bool(on)
            if self.demo_on:
                self.demo_started = time.time()
            return self.demo_on

    def replay_demo(self) -> bool:
        """Start the sample night over. No-op when demo is off. Book untouched."""
        with self.lock:
            if not self.demo_on:
                return False
            self.demo_started = time.time()
            return True

    def _play_demo(self, elapsed: float, now: float) -> "Tower":
        """Replay the sample onto a throwaway recorder.

        Timestamps are anchored so the newest played click sits just before
        `now`. Waits are the gaps in the script, not wall-clock sleeps.
        """
        played = Tower()
        origin = now - elapsed
        for t, kind, role, text, meta in DEMO_EVENTS:
            if t > elapsed:
                break
            at = origin + t
            if kind == "checkout":
                played.click(role, text, checkout=True, at=at, **meta)
            elif kind == "release":
                played.release(role)
            else:
                played.click(role, text, at=at, **meta)
        if elapsed + 1e-9 >= DEMO_DURATION and played.holder:
            played.release(played.holder)
        return played

    # -- snapshot --------------------------------------------------------
    def snapshot(self, *, equity: float, day_pnl: float, start_equity: float,
                 positions: list, running: bool, started_at: float, mode: str,
                 cycle: int, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        with self.lock:
            demo = self.demo_on
            demo_started = self.demo_started
        if demo:
            elapsed = max(0.0, now - demo_started)
            played = self._play_demo(min(elapsed, DEMO_DURATION), now)
            done = elapsed >= DEMO_DURATION
            if done:
                eq, start = DEMO_END_EQUITY, DEMO_START_EQUITY
                pos = [dict(p) for p in DEMO_POSITIONS]
            else:
                start = DEMO_START_EQUITY
                # A few paper clicks into the sample, not the landing number.
                buys = played.clicks["executor"]
                eq = round(start + buys * 4.5, 2)
                pos = []
            view = played._view(equity=eq, day_pnl=round(eq - start, 2),
                                start_equity=start, positions=pos, running=True,
                                started_at=now - WATCH_S * (1 if done else 0.42),
                                mode="paper", cycle=0, now=now)
            view["demo"] = True
            view["demo_label"] = ("DEMO REPLAY · sample night, not this desk's book. "
                                  "Your paper ledger was not touched.")
            view["live_equity"] = round(float(equity or 0), 2)
            view["live_start"] = round(float(start_equity or 0), 2)
            view["span_label"] = "overnight"
            if done:
                view["window"]["progress"] = 1.0
            return view
        return self._view(equity=equity, day_pnl=day_pnl, start_equity=start_equity,
                          positions=positions, running=running, started_at=started_at,
                          mode=mode, cycle=cycle, now=now, demo=False)

    def _view(self, *, equity, day_pnl, start_equity, positions, running,
              started_at, mode, cycle, now, demo=False) -> dict:
        with self.lock:
            start, end = _window(started_at or now, now)
            progress = 0.0 if end <= start else min(1.0, max(0.0, (now - start) / (end - start)))
            total_clicks = sum(self.clicks.values())
            total_holds = sum(self.holds.values())
            agents = []
            for role in ROLES:
                meta = ROLE_META[role]
                waits = list(self.waits[role])
                avg = (sum(waits) / len(waits)) if waits else 0.0
                last = self.last.get(role)
                agents.append({
                    "id": role,
                    "name": meta["name"],
                    "job": meta["job"],
                    "color": meta["color"],
                    "head": meta["head"],
                    "badge": self._badge(role, now),
                    "clicks": self.clicks[role],
                    "holds": self.holds[role],
                    "avg_wait_s": round(avg, 2),
                    "spark": [round(x, 3) for x in self.sparks[role]],
                    "line": _agent_line(last),
                })
            actions = list(self.actions)
            readback = list(self.readback)
            holder = self.holder
            waiters = set(self.waiters)
            focus = dict(self.focus)
            facts = {k: dict(v) for k, v in self.facts.items()}
            collisions = self.collisions
            risk_first = self.risk_first
        nxt = _next_role(holder, waiters)
        rows = []
        for p in positions or []:
            row = dict(p)
            if row.get("pnl_usd") is None:
                row["pnl_usd"] = round(float(row.get("value") or 0) - float(row.get("cost") or 0), 2)
            rows.append(row)
        token = _token_panel(focus, rows, actions, facts)
        start_eq = float(start_equity or 0)
        eq = float(equity or 0)
        multiple = round(eq / start_eq, 2) if start_eq else 0.0
        return {
            "demo": demo,
            "demo_label": "",
            "paper": True,
            "signing": False,
            "mode": mode or "paper",
            "running": bool(running),
            "cycle": cycle,
            "window": {"start": start, "end": end, "now": now, "progress": round(progress, 4)},
            "span_label": "this session",
            "clicks": total_clicks,
            "holds": total_holds,
            "risk_first": risk_first,
            "collisions": collisions,
            "holder": holder,
            "holder_name": ROLE_META[holder]["name"] if holder else None,
            "next": nxt,
            "next_name": ROLE_META[nxt]["name"] if nxt else None,
            "agents": agents,
            "queue": actions[-10:],
            "readback": readback[-40:],
            "token": token,
            "positions": rows,
            "pnl": {
                "start": round(start_eq, 2),
                "equity": round(eq, 2),
                "day_pnl": round(float(day_pnl or 0), 2),
                "multiple": multiple,
            },
        }

    def _badge(self, role: str, now: float) -> str:
        if self.holder == role:
            return "ON CURSOR"
        if role in self.waiters:
            n = self.holds[role]
            return f"HOLD #{n}" if n else "HOLD"
        last = self.last.get(role)
        if not last:
            return "READY"
        age = now - float(last.get("ts") or now)
        if role == "risk" and age < 180:
            return "VERIFIED"
        if age < 30:
            return "DONE"
        return "READY"


def _window(started_at: float, now: float) -> tuple[float, float]:
    """The watch that contains `now`. A window that ends on this exact second
    stays the one that just landed, instead of rolling a fresh empty one."""
    elapsed = max(0.0, now - started_at)
    slot = int((elapsed - 1e-3) // WATCH_S) if elapsed > 0 else 0
    if slot < 0:
        slot = 0
    start = started_at + slot * WATCH_S
    return start, start + WATCH_S


def _next_role(holder: str | None, waiters: set[str]) -> str | None:
    for role in PRIORITY:
        if role in waiters and role != holder:
            return role
    if not holder:
        return None
    idx = ROLES.index(holder)
    return ROLES[(idx + 1) % len(ROLES)]


def _agent_line(last: dict | None) -> str:
    if not last:
        return "idle"
    symbol = last.get("symbol") or ""
    text = last.get("text") or ""
    ident = last.get("id") or ""
    line = f"> {ident} {text}" + (f" · {symbol}" if symbol else "")
    return line


def _token_panel(focus: dict, positions: list, actions: list,
                 facts: dict | None = None) -> dict | None:
    facts = facts or {}
    pick = None
    mint = focus.get("mint")
    if positions:
        pick = next((p for p in positions if mint and p.get("mint") == mint), positions[0])
    if pick is None and not focus:
        return None
    symbol = (pick or {}).get("symbol") or focus.get("symbol") or ""
    mint = (pick or {}).get("mint") or focus.get("mint") or ""
    known = dict(facts.get(mint) or {})
    if not known and mint == focus.get("mint"):
        known = focus
    mcap = (pick or {}).get("mcap_usd")
    if mcap is None:
        mcap = known.get("mcap")
    holders = _holders(known)
    trades = [a for a in actions if symbol and a.get("symbol") == _sym(symbol)]
    return {
        "symbol": symbol,
        "mint": mint,
        "mcap_usd": mcap,
        "holders": holders,
        "holder_count": known.get("holders"),
        "trades": trades[-12:],
        "demo_mint": str(mint).startswith("DEMO"),
    }


def _holders(focus: dict) -> list | None:
    tw, t10 = focus.get("top_wallet"), focus.get("top_10")
    if tw is None and t10 is None:
        return None
    try:
        tw_f = max(0.0, float(tw or 0))
        t10_f = max(tw_f, float(t10 if t10 is not None else tw_f))
    except (TypeError, ValueError):
        return None
    # Chain facts store shares as fractions (0.14 = 14%).
    if t10_f > 1.5:
        tw_f /= 100.0
        t10_f /= 100.0
    rest = max(0.0, 1.0 - t10_f)
    mid = max(0.0, t10_f - tw_f)
    return [
        {"label": "top wallet", "pct": round(tw_f * 100, 1)},
        {"label": "next 9", "pct": round(mid * 100, 1)},
        {"label": "the rest", "pct": round(rest * 100, 1)},
    ]


# Process-wide feed. The loop emits into this; the view reads it.
FEED = Tower()


def click(role: str, text: str, **meta):
    try:
        return FEED.click(role, text, **meta)
    except Exception:
        return None


def release(role: str) -> None:
    try:
        FEED.release(role)
    except Exception:
        return


@contextmanager
def holding(role: str, text: str, **meta):
    try:
        FEED.click(role, text, checkout=True, **meta)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            FEED.release(role)
        except Exception:
            return


def observe(ev: dict) -> None:
    FEED.observe(ev)


def reset() -> None:
    FEED.reset()


def set_demo(on: bool) -> bool:
    return FEED.set_demo(on)


def replay_demo() -> bool:
    return FEED.replay_demo()


def snapshot(**kwargs) -> dict:
    return FEED.snapshot(**kwargs)
