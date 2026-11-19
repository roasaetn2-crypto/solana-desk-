"""The Solana Agent: one autonomous worker that runs the whole desk for Solana.

It is the same five seats as the reference desk, wired into a single long-lived
agent you can start, pause, step and reset from the dashboard:

    scanner (free) -> chain facts (RPC) -> analyst (thesis) -> risk (code) -> paper fill

The agent owns the loop, the budgets and the event log.

Seat 5 is swappable. In paper mode (the default, and the mode it boots into
every time) approved setups become paper fills and alerts. In live mode, armed
explicitly with a key and a confirmation phrase, the same approved setup becomes
a real Jupiter swap signed locally by desk/live.py. The agent loop itself does
not change: it holds a broker, and the broker decides whether the fill is paper.
"""
from __future__ import annotations

import os

import hashlib
import random
import threading
import time

# How long a status() snapshot may be reused. Kept short: long enough that a
# burst of dashboard polls costs one build, short enough that the screen is
# never visibly behind the desk.
_STATUS_TTL = 2.0
from collections import Counter, deque
from datetime import date, datetime, timezone

import requests

from config import AGENT, CONFIRM, ENTRY, EXECUTION, EXITS, FOMO, MEME, PAPER, RISK, RULEBOOK, SCALP
from desk.brain_advisor import ADVISOR, should_advise, wants_entry, wants_exit
from desk import (analyst, analyst_ai, brain, chain, dexscreener, devcheck, dialogue, exits, fomo, goplus, gmgn,
                  groq_postmortem, groq_sizer, guard, heartbeat, helius, meme, meme_tactics,
                  pumpportal, regime, risk, scanner, signal_engine, signals, social)
from desk import chart as candle_chart
from desk import dip_recovery, entry_trigger, marketdata, rulebook
from desk import confirm as entry_confirm
from desk import links as fomo_links
from desk.age import created_at_of
from desk import equity_curve
from desk import live as live_mod
from desk.broker import PaperBroker
from desk.netutil import offline
from desk.ops import Bench, alert, data_file, journal, killed
from desk.risk import daily_loss_hit, daily_loss_limit_usd, daily_override_active
from desk.risk import (Account, check_live_trade, check_token_trade,
                       fit_risk_budget, live_cap_problems, live_fee_model,
                       live_sizing_breakdown,
                       open_risk_usd,
                       risk_budget_usd, sizing_base, sizing_breakdown)



_TOKEN_SNAP_KEYS = (
    "mint", "symbol", "price_usd", "mcap_usd", "liquidity_usd",
    "volume_m5", "volume_h1", "volume_h6", "volume_h24",
    "buys_h1", "sells_h1", "buyers_h1", "change_m5", "change_h1",
    "age_minutes", "source",
)


def token_snap(t: dict | None) -> dict:
    """Small token snapshot for the journal / backtest. No secrets, no raw dumps."""
    if not t:
        return {}
    return {k: t[k] for k in _TOKEN_SNAP_KEYS if t.get(k) is not None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _demo_links(mint: str) -> dict:
    h = int(hashlib.sha256(mint.encode()).hexdigest(), 16)
    rng = random.Random(h)
    has_x = rng.random() < 0.55
    has_tg = rng.random() < 0.45
    return {
        "x": f"https://x.com/{mint[:8]}" if has_x else None,
        "telegram": f"https://t.me/{mint[:8]}" if has_tg else None,
        "website": f"https://{mint[:8]}.xyz" if rng.random() < 0.4 else None,
    }


class SolanaAgent:
    def __init__(self, ledger=None, bench_path=None,
                 live_ledger=None):
        self.paper = PaperBroker(ledger if ledger is not None else data_file("paper_ledger.json"))
        self.live = live_mod.LiveBroker(live_ledger if live_ledger is not None else data_file("live_ledger.json"))
        self.mode = EXECUTION["default_mode"]          # always paper on boot
        self.bench = Bench(str(bench_path if bench_path is not None else data_file("desk.db")))
        # MERGE: count why tokens were benched (funnel reporting)
        self.reject_reasons: Counter = Counter()
        _sit = self.bench.sit

        self.funnel: Counter = Counter()   # reject reasons since the last funnel log line
        # Confirmed scalp entry checklist (desk/confirm.py): totals and the last
        # few candidates with per-check pass/fail, shown on /api/health.
        self.confirm_stats: Counter = Counter()
        self.confirm_recent: deque = deque(maxlen=12)

        def _counting_sit(mint, reason, *a, **k):
            self.reject_reasons[reason] += 1
            self.funnel[reason] += 1
            return _sit(mint, reason, *a, **k)
        self.bench.sit = _counting_sit
        self.started_at = time.time()
        self.last_entry_ts: float | None = None
        # Operator-confirmed paper settings are re-applied on boot so they survive a redeploy.
        try:
            from desk import settings as desk_settings
            desk_settings.apply_saved()
        except Exception as e:
            print(f"[desk] settings: re-apply skipped ({type(e).__name__})")
        self.session = requests.Session()
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.running = False
        self.cycle_no = 0
        self._floor_warn_cycle = -1
        self.last_cycle_at: str | None = None
        self.data_source = "idle"
        self.events: deque = deque(maxlen=AGENT["max_events"])
        self.decisions: deque = deque(maxlen=60)
        self.transcripts: deque = deque(maxlen=40)
        self.tokens_seen: dict[str, dict] = {}
        self.stats = {"cycles": 0, "seen": 0, "free_rejects": 0, "chain_rejects": 0,
                      "dev_rejects": 0, "meme_rejects": 0, "analyst_calls": 0,
                      "risk_rejects": 0, "sec_rejects": 0, "guard_rejects": 0, "floor_rejects": 0,
                      "signal_rejects": 0, "shadow_blocks": 0, "rulebook_rejects": 0,
                      "trigger_pass": 0, "trigger_none": 0, "trigger_unknown": 0,
                      "breakout_blocked_no_chase": 0,
                      "trigger_pass_rsi": 0, "trigger_pass_breakout": 0,
                      "trigger_pass_vwap": 0, "trigger_pass_ema": 0,
                      "entries": 0, "exits": 0, "dialogues": 0,
                      "errors": 0}
        self.equity_curve: deque = deque(maxlen=equity_curve.MAX_POINTS)
        self.curve_marks: deque = deque(maxlen=equity_curve.MAX_MARKS)
        # The chart used to die on every restart because these were memory-only.
        # Restore them from disk; if the store is missing (first boot after this
        # change, or a wiped data dir) rebuild it from the ledger's real fills so
        # an existing book does not show a blank chart.
        self._curve_loaded = equity_curve.load(self.equity_curve, self.curve_marks)
        # Say it out loud in desk.log. "The chart is empty after a restart" was
        # impossible to diagnose from the dashboard: you could not tell a wiped
        # data dir from a checkout that predates this module from a frontend bug.
        # One line at boot names the file, the count and the checkout's HEAD.
        _ci = equity_curve.store_info()
        print(f"[desk] chart: {self._curve_loaded} point(s), "
              f"{len(self.curve_marks)} mark(s) restored from {_ci['path']}")
        if not self._curve_loaded:
            try:
                pts, mks = equity_curve.backfill_from_ledger(self.paper.s)
                for pt in pts:
                    self.equity_curve.append(pt)
                for m in mks:
                    self.curve_marks.append(m)
                if pts or mks:
                    equity_curve.save(self.equity_curve, self.curve_marks)
                    print(f"[desk] chart: store was empty; rebuilt {len(pts)} point(s) "
                          f"and {len(mks)} mark(s) from the ledger's real fills")
            except Exception:
                pass
        # discipline layer (paper only, tighten-only)
        self.regime: dict = {"tag": "UNKNOWN", "halve": False}
        self.breaker: dict = {"state": "ok", "mult": 1.0, "reason": "ok"}
        self.reviews: deque = deque(maxlen=50)

    # ---------- execution mode ----------
    @property
    def broker(self):
        """The active seat 5. Paper by default, live only after explicit arming."""
        if live_mod.LIVE_DISABLED:  # LOCAL CHANGE
            return self.paper
        return self.live if self.mode == "live" else self.paper

    def mode_info(self) -> dict:
        ok, why = live_mod.ready()
        return {
            "mode": self.mode,
            "live_ready": ok,
            "live_reason": why,
            "dry_run": live_mod.dry_run(),
            "wallet": self.live.pubkey(),
            "confirm_phrase": EXECUTION["live_confirm_phrase"],
            "caps": {
                "max_trade": EXECUTION["live_max_trade_usd"],
                "min_ticket": EXECUTION["live_min_ticket_usd"],
                "max_notional": EXECUTION["live_max_notional_usd"],
                "daily_loss_usd": self.live.loss_limit_usd(),
                "max_slippage_bps": EXECUTION["live_max_slippage_bps"],
            },
            # A contradictory cap pair (per-trade cap under the fee floor) looks
            # perfectly healthy in the UI but refuses every fill. Surface it.
            "cap_problems": live_cap_problems(),
            # Jupiter charges 0%. This is what Solana actually costs per round
            # trip at the smallest ticket, so the dashboard can show the real
            # break-even instead of a decorative fee column.
            # Paper shows the paper fee model (flat per side + spread); live shows
            # the live floor model.
            "fees": (self.paper.fee_model() if self.mode != "live"
                     else live_fee_model(EXECUTION["live_min_ticket_usd"])),
            "paper_equity": self.paper.equity(),
            "live_pnl": self.live.equity(),
        }

    def set_mode(self, mode: str, confirm: str = "", force: bool = False) -> dict:
        """Switch seat 5. Paper is instant and safe. Live needs the phrase + a key."""
        mode = (mode or "").strip().lower()
        if mode == "paper":
            # Switching to paper does NOT close live positions: the two seats keep
            # separate books, and every exit/stop path iterates self.broker, which
            # would become the PAPER book the instant we flip. A live position left
            # open that way is orphaned -- no stop loss, no take profit, no exit --
            # while real money sits on chain. Refuse unless the operator forces it.
            if self.mode == "live" and not force:
                open_live = [m for m in self.live.s["positions"]
                             if not str(m).startswith("DEMO")]
                if open_live:
                    return {
                        "ok": False,
                        "error": (f"{len(open_live)} live position(s) still open. "
                                  "Switching to paper would orphan them with no stop "
                                  "loss. Close them first, or force=true to abandon them."),
                        "open_live": open_live,
                    }
            self.mode = "paper"
            self.emit("info", "Execution mode: PAPER. No real money can move.")
            return {"ok": True, **self.mode_info()}
        if mode != "live":
            return {"ok": False, "error": "mode must be 'paper' or 'live'"}
        if live_mod.LIVE_DISABLED:  # LOCAL CHANGE: live can never be armed
            self.mode = "paper"
            return {"ok": False, "error": live_mod.LIVE_DISABLED_MSG}
        if confirm != EXECUTION["live_confirm_phrase"]:
            return {"ok": False, "error": "confirmation phrase did not match — live not armed"}
        ok, why = live_mod.ready()
        if not ok:
            return {"ok": False, "error": why}
        problems = live_cap_problems()
        if problems:
            return {"ok": False,
                    "error": "live caps are contradictory, not arming: "
                             + "; ".join(problems),
                    "cap_problems": problems}
        self.mode = "live"
        self.emit("warn", f"LIVE TRADING ARMED — wallet {self.live.pubkey()}. "
                          f"Real money can move. Caps: "
                          f"${EXECUTION['live_max_trade_usd']:.0f}/trade, "
                          f"${EXECUTION['live_max_notional_usd']:.0f} total.")
        journal("mode", mode="live", wallet=self.live.pubkey())
        return {"ok": True, **self.mode_info()}

    # ---------- event log ----------
    def emit(self, kind: str, message: str, **data):
        # LOCAL CHANGE: never show the RPC URL (it may embed an API key) in the UI log
        for _sec in (os.environ.get("SOLANA_RPC_URL", ""), os.environ.get("HELIUS_API_KEY", "")):
            if len(_sec) > 8:
                message = message.replace(_sec, "***")
                if "?" in _sec:
                    message = message.replace(_sec.split("?", 1)[1], "***")
        ev = {"time": _now(), "kind": kind, "message": message, **data}
        with self.lock:
            self.events.appendleft(ev)
        return ev


    def _snapshot_equity(self, t: float | None = None) -> dict:
        """Record the book as it is right now. Never invent a price."""
        ts = t if t is not None else time.time()
        with self.lock:
            pt = equity_curve.record_point(
                self.equity_curve, ts, self.broker.equity(), self.broker.day_pnl())
            equity_curve.save(self.equity_curve, self.curve_marks)
            return pt

    def _mark_trade(self, kind: str, fill: dict, **extra) -> dict:
        ts = time.time()
        usd = extra.pop("usd", None)
        if usd is None:
            usd = fill.get("ticket_usd")
            if usd is None and fill.get("fill") is not None and fill.get("qty") is not None:
                usd = round(float(fill["fill"]) * float(fill["qty"]), 2)
        mark = equity_curve.record_mark(
            self.curve_marks, ts, kind, fill.get("symbol") or "?",
            usd=usd, pnl_usd=fill.get("pnl_usd"), fraction=extra.get("fraction"),
            reason=fill.get("reason"), mint=fill.get("mint"),
            entry=extra.get("entry"), fill=fill.get("fill"),
            mult=extra.get("mult"), mcap_usd=extra.get("mcap_usd"))
        # force=True: a fill must be on disk immediately, not 20s later.
        with self.lock:
            equity_curve.save(self.equity_curve, self.curve_marks, force=True)
        self._snapshot_equity(ts)
        return mark

    def _mcap_of(self, mint: str | None) -> float | None:
        if not mint:
            return None
        tok = self.tokens_seen.get(mint) or {}
        val = tok.get("mcap_usd")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def _activity(self, seen: dict) -> list[dict]:
        rows = []
        for ev in self.events:
            if ev.get("kind") not in ("entry", "exit"):
                continue
            mint = ev.get("mint")
            tok = seen.get(mint or "") or {}
            mcap = tok.get("mcap_usd")
            row = {
                "kind": ev["kind"],
                "symbol": ev.get("symbol"),
                "mint": mint,
                "time": ev.get("time"),
                "reason": ev.get("reason") or ev.get("message"),
                "seat": "broker",
                "usd": ev.get("ticket_usd"),
                "pnl_usd": ev.get("pnl_usd"),
                "fraction": ev.get("fraction"),
                "entry": ev.get("entry"),
                "fill": ev.get("fill"),
                # How many tokens the fill actually bought. The entry event has
                # always carried it, but this row dropped it, so the tape could
                # say "Bought $WIF - $12" and never how much WIF that was. On a
                # memecoin the token count IS the position: $12 of a 8.8e-7
                # token is ~13.5M units, and the size of that number is what
                # tells you whether the fill was sane or a decimals mismatch.
                "qty": ev.get("qty"),
                "message": ev.get("message"),
                "review": ev.get("review"),
            }
            if ev.get("kind") == "exit" and row["usd"] is None and ev.get("fill") is not None and ev.get("qty") is not None:
                row["usd"] = round(float(ev["fill"]) * float(ev["qty"]), 2)
            if ev.get("entry") and ev.get("fill"):
                try:
                    entry = float(ev["entry"])
                    if entry > 0:
                        row["mult"] = round(float(ev["fill"]) / entry, 4)
                except (TypeError, ValueError):
                    pass
            if mcap is not None:
                try:
                    row["mcap_usd"] = float(mcap)
                except (TypeError, ValueError):
                    pass
            rows.append(row)
        return rows

    def _latest_realized(self, broker: dict) -> dict | None:
        for c in broker.get("closed") or []:
            if c.get("pnl_usd") is not None:
                return {
                    "symbol": c.get("symbol"),
                    "mint": c.get("mint"),
                    "pnl_usd": c.get("pnl_usd"),
                    "reason": c.get("reason"),
                    "t": c.get("closed"),
                }
        return None

    # ---------- daily-loss override (PAPER only, today only) ----------
    def _paper_only(self) -> bool:
        """True only when the desk is in PAPER and the active seat 5 is the paper broker."""
        return self.mode == "paper" and self.broker is self.paper

    def _paper_account(self) -> Account:
        return Account(equity=self.paper.equity(), day_pnl=self.paper.day_pnl(),
                       open_positions=self.paper.open_count(),
                       realized_equity=self.paper.realized_equity(),
                       cash=self.paper.s.get("cash"),
                       day_start_equity=self.paper.s.get("day_start"))

    def daily_override_today(self) -> bool:
        """Operator tapped Continue today, or auto-continue is switched on.

        Never true in LIVE; the manual override expires at the day roll. The
        auto-continue setting (RISK["auto_continue_after_daily_stop"]) is an
        explicit operator choice: the daily stop still fires and is still
        journaled, but new paper entries are not blocked by it.
        """
        if not self._paper_only():
            return False
        if RISK.get("auto_continue_after_daily_stop"):
            return True
        return daily_override_active(self.mode, self.paper.s.get("daily_loss_override_date"),
                                     str(date.today()))

    def daily_loss_halted(self) -> bool:
        """The 3% daily stop is tripped on the paper book today (override or not)."""
        if not self._paper_only():
            return False
        return daily_loss_hit(self._paper_account())

    def risk_status(self) -> dict:
        with self.lock:
            self.paper._roll_day()
            paper = self._paper_only()
            halted = self.daily_loss_halted()
            override = self.daily_override_today()
            acct = self._paper_account() if paper else None
            return {
                "mode": self.mode,
                "daily_loss_limit_pct": RISK["max_daily_loss_pct"],
                "daily_loss_limit_usd_setting": float(RISK.get("max_daily_loss_usd") or 0.0),
                "daily_loss_limit_usd": round(daily_loss_limit_usd(acct), 2) if acct else None,
                "auto_continue": bool(RISK.get("auto_continue_after_daily_stop")),
                "day": self.paper.s.get("day"),
                "day_pnl": self.paper.day_pnl() if paper else None,
                "daily_loss_halted": halted,
                "override_today": override,
                "blocking_entries": halted and not override,
                "can_continue": paper and halted and not override,
                "override_date": self.paper.s.get("daily_loss_override_date") if paper else None,
                "risk_budget_usd": round(risk_budget_usd(acct), 2) if acct else None,
                "enforce_risk_budget": bool(RISK.get("enforce_risk_budget", True)),
                "open_risk_usd": (round(open_risk_usd(self.paper.s.get("positions") or {}), 2)
                                  if paper else None),
                "over_risk_budget": (
                    bool(RISK.get("enforce_risk_budget", True))
                    and open_risk_usd(self.paper.s.get("positions") or {})
                    > risk_budget_usd(acct) + 1e-9) if acct else False,
            }

    def continue_today(self) -> dict:
        """Operator Continue: resume PAPER entries for the rest of today after the daily stop.

        Refuses unless the desk is in PAPER and the daily stop is actually tripped
        today. The limit is untouched; the override expires at the day roll.
        """
        with self.lock:
            if not self._paper_only():
                return {"ok": False, "error": "Continue works in PAPER only"}
            self.paper._roll_day()
            today = str(date.today())
            acct = self._paper_account()
            if not daily_loss_hit(acct):
                return {"ok": False, "error": "the daily loss stop is not active today",
                        "risk": self.risk_status()}
            if self.paper.s.get("daily_loss_override_date") == today:
                return {"ok": True, "already": True,
                        "message": "daily stop already overridden for today",
                        "risk": self.risk_status()}
            self.paper.s["daily_loss_override_date"] = today
            self.paper._save()
            running = self.running
        row = journal("risk_override", action="continue_today", mode="paper", day=today,
                      day_pnl=acct.day_pnl, equity=acct.equity,
                      limit_pct=RISK["max_daily_loss_pct"], loop_running=running)
        print(f"[desk] risk: operator Continue at {row['time']} — daily stop overridden for "
              f"{today} (paper, day P&L ${acct.day_pnl:.2f})")
        self.emit("warn", f"Continue: daily loss stop overridden for {today} "
                          f"(day P&L ${acct.day_pnl:.2f}). Paper only; resets at day roll.",
                  stage="risk")
        return {"ok": True, "message": "paper trading resumed for today",
                "loop_running": running, "risk": self.risk_status()}

    # ---------- lifecycle ----------
    def start(self, persist_intent: bool = True, intent_source: str = "operator"):
        with self.lock:
            if self.running:
                if persist_intent:
                    from desk.intent import record_loop_intent
                    record_loop_intent("running", source=intent_source)
                return {"ok": True, "message": "agent already running"}
            self._stop.clear()
            self.running = True
            self._thread = threading.Thread(target=self._loop, name="solana-agent", daemon=True)
            self._thread.start()
        if persist_intent:
            from desk.intent import record_loop_intent
            record_loop_intent("running", source=intent_source)
        if not offline():
            # optional, free, fail-open: both are no-ops until configured / reachable
            heartbeat.start(self._heartbeat_status)
            pumpportal.start()
        if not self.equity_curve:
            self._snapshot_equity()
        self.emit("info", "Solana Agent started. Watching new Solana pools.")
        return {"ok": True, "message": "agent started"}

    def _heartbeat_status(self) -> tuple[bool, float | None]:
        age = None
        if self.last_cycle_at:
            try:
                age = time.time() - datetime.fromisoformat(self.last_cycle_at).timestamp()
            except ValueError:
                age = None
        return self.running, age

    def stop(self):
        with self.lock:
            self.running = False
            self._stop.set()
        from desk.intent import record_loop_intent
        record_loop_intent("stopped", source="operator")
        self.emit("info", "Solana Agent paused.")
        return {"ok": True, "message": "agent paused"}

    def _loop(self):
        while not self._stop.is_set():
            if not offline():
                pumpportal.start()          # restart the feed if its thread died
            if killed():
                self._liquidate("KILL file found")
                with self.lock:
                    self.running = False
                from desk.intent import record_loop_intent
                record_loop_intent("stopped", source="kill")
                self.emit("info", "KILL switch: all paper positions closed, agent stopped.")
                return
            try:
                self.run_once()
            except Exception as e:  # never let one bad cycle kill the agent
                self.stats["errors"] += 1
                self.emit("error", f"cycle failed: {e}")
                journal("error", stage="agent_cycle", error=str(e))
            self._stop.wait(AGENT["cycle_seconds"])

    def _liquidate(self, reason: str):
        brk = self.broker
        book = brk.s["positions"]
        for mint in list(book):
            pos = book.get(mint)
            if not pos:
                continue
            price = pos.get("fill_at") or pos["last"]
            entry = pos.get("entry")
            fill = brk.sell(mint, 1.0, price, reason)
            journal("exit", **fill)
            self.emit("exit", f"closed {fill['symbol']} — {reason}", fraction=1.0, entry=entry, **fill)
            self._mark_trade("sell", fill, fraction=1.0, entry=entry, mcap_usd=self._mcap_of(mint))

    def _enforce_daily_stop(self):
        """Turn the daily loss stop from a label into a promise.

        Until now `daily_loss_hit` only blocked NEW entries. Open positions kept
        bleeding, so a desk that advertises "Daily loss stop 3%" could end the day
        at -80% (screenshot 2026-11-14 01:12: $66.81 -> $13.01, -80.53%). A stop
        that does not close the book is not a stop.

        Paper only, once per day, and never when the operator has tapped
        "Continue today". The limit itself is untouched.
        """
        if not self._paper_only() or not RISK.get("flatten_on_daily_stop", True):
            return
        today = str(date.today())
        if self.paper.s.get("daily_stop_flatten_date") == today:
            return
        if daily_override_active(self.mode, self.paper.s.get("daily_loss_override_date"), today):
            return
        acct = self._paper_account()
        if not daily_loss_hit(acct) or not self.paper.s["positions"]:
            return
        before, day_pnl = acct.equity, acct.day_pnl
        self._liquidate(f"daily loss stop {RISK['max_daily_loss_pct'] * 100:.0f}% hit")
        self.paper.s["daily_stop_flatten_date"] = today
        self.paper._save()
        after = self.paper.equity()
        journal("risk_flatten", day=today, equity_before=round(before, 2),
                equity_after=round(after, 2), day_pnl=round(day_pnl, 2),
                limit_pct=RISK["max_daily_loss_pct"])
        self.emit("warn",
                  f"daily loss stop: flattened the book at ${before:.2f} "
                  f"(day P&L ${day_pnl:.2f}); no new entries until tomorrow.",
                  stage="risk")

    # ---------- one cycle ----------
    def run_once(self, force_demo: bool = False) -> dict:
        with self.lock:
            # roll the paper day in a long-running process too, so the daily stop
            # (and any Continue override) resets at the day roll, not only on restart
            self.paper._roll_day()
            # Heal a corrupt mark every cycle, not only on boot. On Termux the
            # process can run for days, so a bad print written by any path would
            # otherwise sit in the ledger until the next restart. _repair is
            # idempotent and only writes when it actually fixes something.
            try:
                self.paper._repair()
            except Exception as e:
                self.stats["errors"] += 1
                self.emit("error", f"ledger repair skipped ({type(e).__name__}: {e})")
            # The daily stop must close the book, not just stop new buys.
            try:
                self._enforce_daily_stop()
            except Exception as e:
                self.stats["errors"] += 1
                self.emit("error", f"daily stop flatten skipped ({type(e).__name__}: {e})")
            self.cycle_no += 1
            self.stats["cycles"] += 1
            cycle = self.cycle_no

        try:
            self._manage_positions()
        except Exception as e:  # an exit that fails must not block new entries
            with self.lock:
                self.stats["errors"] += 1
            self.emit("error", f"position management failed ({type(e).__name__}: {e})")
            journal("error", stage="manage", error=str(e))
        # Re-check after this cycle's exits: a batch of stops can push the day
        # past the limit inside one cycle, and the rest of the book must not keep
        # bleeding until the next one.
        try:
            self._enforce_daily_stop()
        except Exception as e:
            self.stats["errors"] += 1
            self.emit("error", f"daily stop flatten skipped ({type(e).__name__}: {e})")
        tokens, source = self._scan(force_demo)
        with self.lock:
            self.data_source = source
        ADVISOR.new_cycle()
        analyst_ai.new_cycle()
        try:    # rulebook: remember each pool's rolling 5m volume (no HTTP)
            rulebook.record_volume(tokens)
        except Exception:
            pass

        # rank by volume churn: the loudest pools first
        tokens.sort(key=lambda t: t["volume_h1"] / max(t["mcap_usd"], 1), reverse=True)
        chain_left, analyst_left = AGENT["chain_budget"], AGENT["analyst_budget"]
        counts = scanner.source_counts(tokens)
        report = scanner.fetch_report()
        self.emit("scan",
                  f"cycle {cycle}: {len(tokens)} pools from {source} "
                  f"(gt={counts['geckoterminal']} ds={counts['dexscreener']} "
                  f"jup={counts['jupiter']} pp={counts['pumpportal']}, "
                  f"calls gt={report['geckoterminal']['calls']} ds={report['dexscreener']['calls']} "
                  f"jup={report['jupiter']['calls']}"
                  + (f", cooling {','.join(f'{h} {s}s' for h, s in report['cooling'].items())}"
                     if report.get('cooling') else "") + ")",
                  count=len(tokens), source=source, **counts)
        print(f"scan: cycle {cycle}: {len(tokens)} pools from {source} "
              f"(gt={counts['geckoterminal']} ds={counts['dexscreener']} jup={counts['jupiter']} "
              f"pp={counts['pumpportal']}; calls gt={report['geckoterminal']['calls']} "
              f"ds={report['dexscreener']['calls']} jup={report['jupiter']['calls']}; "
              f"cooling={report.get('cooling') or {}})", flush=True)
        for warning in report.get("warnings") or []:
            self.emit("info", f"scanner: {warning}", stage="scanner")

        # discipline layer: market regime tag and drawdown breaker. Tighten-only,
        # and it must run in BOTH modes. Gating this on `mode != live` meant the
        # breaker never updated for real money -- self.breaker stayed
        # {"state":"ok"} forever, so the drawdown stop could not fire on the one
        # book where it matters. A guard that only guards the simulator is not a
        # guard.
        brk = {"state": "ok", "mult": 1.0, "reason": "ok"}
        try:
            self.regime = ({"tag": "UNKNOWN", "halve": False} if (force_demo or offline())
                           else regime.current(self.session))
        except Exception as e:      # a regime outage must not brick entries; UNKNOWN = no halve
            self.stats["errors"] += 1
            self.emit("warn", f"regime unavailable ({type(e).__name__}); treating as UNKNOWN",
                      stage="guard")
            self.regime = {"tag": "UNKNOWN", "halve": False}
        # The pause switch is a PAPER setting. Real money always keeps the pause.
        # PAPER only: losses that closed within ~60 s of a settings change are artifacts
        # of the change and are left out of the loss streak. Real money counts every loss.
        times = None
        if self.mode != "live":
            try:
                from desk import settings as desk_settings
                times = desk_settings.change_times()
            except Exception:
                times = None
        brk = guard.circuit_breaker(self.broker.s["closed"], self.broker.day_pnl(),
                                    self.broker.s["day_start"],
                                    pause_enabled=True if self.mode == "live" else None,
                                    change_times=times)
        if brk["state"] != self.breaker.get("state"):
            self.emit("info" if brk["state"] == "ok" else "warn",
                      f"breaker: {brk['state']} ({brk['reason']})", stage="guard")
        self.breaker = brk
        if brk["mult"] <= 0:
            with self.lock:
                for t in tokens:
                    self.tokens_seen[t["mint"]] = t
            tokens = []          # positions are still managed; only new entries pause

        # Social attention is a per-cycle cache warm, never a per-token fetch.
        # Doing it here (once, over the shared session) keeps the signal seat
        # instant and stops the ~16-19 MB RSS spike that used to OOM the box.
        try:
            social.refresh()
        except Exception:
            pass

        for t in tokens:
            try:
                with self.lock:
                    self.stats["seen"] += 1
                    self.tokens_seen[t["mint"]] = t
                if t["mint"] in self.broker.s["positions"] or self.bench.benched(t["mint"]):
                    continue

                # seat 1: free cuts
                if (k := scanner.free_kill(t)):
                    self.bench.sit(t["mint"], k)
                    self.stats["free_rejects"] += 1
                    self.emit("reject", f"{t['symbol']}: cut at scanner ({k})",
                              stage="scanner", reason=k, symbol=t["symbol"], mint=t["mint"])
                    journal("reject", stage="scanner", mint=t["mint"], symbol=t["symbol"],
                            reason=k, token=token_snap(t))
                    continue

                # trading style (PAPER, operator-confirmed): scalp only / swing only / both
                if self.mode != "live" and EXITS.get("style", "both") != "both":
                    hz = exits.horizon_for(t.get("age_minutes"))
                    if EXITS.get("style") != hz:
                        self.bench.sit(t["mint"], "style")
                        self.stats["free_rejects"] += 1
                        self.emit("reject", f"{t['symbol']}: {hz} coin, desk is {EXITS.get('style')} only",
                                  stage="scanner", reason="style", symbol=t["symbol"], mint=t["mint"])
                        continue

                if chain_left <= 0 or analyst_left <= 0:
                    break

                # seat 2: chain facts
                try:
                    f = chain.facts(t["mint"], self.session)
                except Exception as e:
                    self.stats["errors"] += 1
                    self.emit("error", f"{t['symbol']}: chain lookup failed ({type(e).__name__})",
                              stage="chain", symbol=t["symbol"])
                    journal("error", stage="chain", mint=t["mint"], error=str(e))
                    continue
                chain_left -= 1
                if (k := chain.chain_kill(f)):
                    self.bench.sit(t["mint"], k)
                    self.stats["chain_rejects"] += 1
                    self.emit("reject", f"{t['symbol']}: chain fact killed it ({k})",
                              stage="chain", reason=k, symbol=t["symbol"], mint=t["mint"])
                    journal("reject", stage="chain", mint=t["mint"], symbol=t["symbol"],
                            reason=k, facts=f)
                    continue

                # seat 2b: DexScreener market view + dev/creator investigation
                ds = dexscreener.facts(t["mint"], self.session)
                dev = devcheck.investigate(t["mint"], f, self.session)

                # seat 2b-ii: FOMO evidence (optional; no key = "unknown", no cost).
                # One lookup, reused by the dev veto and the meme nudge below. It
                # cannot raise and cannot block: a failure here is simply "unknown".
                fomo_ev = None
                if FOMO.get("enabled") and not t["mint"].startswith("DEMO"):
                    try:
                        lvl = FOMO.get("level", "signals")
                        if lvl == "thesis" and FOMO.get("thesis_only_for_finalists"):
                            lvl = "signals"   # thesis is bought later, at the last gate
                        fomo_ev = fomo.token_evidence(t["mint"], lvl)
                        devcheck.apply_fomo(dev, fomo_ev)
                    except Exception:
                        fomo_ev = None

                if (k := devcheck.dev_kill(dev)):
                    self.bench.sit(t["mint"], k)
                    self.stats["dev_rejects"] += 1
                    self.emit("reject", f"{t['symbol']}: dev investigation said no ({k})",
                              stage="dev", reason=k, symbol=t["symbol"], mint=t["mint"],
                              dev_score=dev.get("dev_score"))
                    journal("reject", stage="dev", mint=t["mint"], symbol=t["symbol"],
                            reason=k, dev=dev)
                    continue

                # seat 2 fallback: RPC had no holder concentration -> RugCheck's list, pools excluded
                if (f2 := chain.with_fallback_concentration(f, dev)) is not None:
                    f = f2
                    if (k := chain.chain_kill(f)):
                        self.bench.sit(t["mint"], k)
                        self.stats["chain_rejects"] += 1
                        self.emit("reject", f"{t['symbol']}: chain fact killed it ({k}, via RugCheck holders)",
                                  stage="chain", reason=k, symbol=t["symbol"], mint=t["mint"])
                        journal("reject", stage="chain", mint=t["mint"], symbol=t["symbol"],
                                reason=k, facts=f)
                        continue

                # Romz's rulebook, PAPER only: 5m buy/sell ratio and LP lock (no extra
                # calls), then 5m volume rising and no-chase on 5m candles. Missing
                # data is a skip, never a pass.
                if self.mode != "live" and not t["mint"].startswith("DEMO"):
                    if (ds or {}).get("available"):
                        marketdata.remember_tx(t["mint"], (ds or {}).get("buys_m5"), (ds or {}).get("sells_m5"),
                                               "dexscreener")
                    k = rulebook.tx_ratio_gate(ds)
                    if k == "tx_ratio_unknown":
                        # DexScreener has no 5m counts (cooling / not listed): Jupiter's 5m stats
                        # (cached row, else one targeted search), then GeckoTerminal for this pool.
                        # No data anywhere -> still a skip, and the reason names the cause:
                        # tx_ratio_unknown:jup_cooling+gt_cooling.
                        alt = marketdata.tx_m5(t, ds, self.session)
                        if alt.get("available"):
                            k = rulebook.tx_ratio_gate(alt)
                            self._count_source("tx_" + alt["source"])
                            # the confirm checklist and the analyst read these same counts
                            f["tx_m5"] = {k2: alt.get(k2) for k2 in ("buys_m5", "sells_m5", "source", "via", "age_s")
                                          if alt.get(k2) is not None}
                        else:
                            k = f"tx_ratio_unknown:{alt['cause']}"
                            self._count_source("tx_missing")
                            f["tx_m5_cause"] = alt["cause"]
                    if (k and k.split(":")[0] == "tx_ratio_unknown"
                            and RULEBOOK.get("tx_ratio_unknown_pass", False)):
                        # Romz: unknown 5m counts (no source, no recent last-good) pass this one gate.
                        # Every other gate below still applies.
                        why = k.partition(":")[2] or "no_data"
                        print(f"gate: ${t.get('symbol')} tx_ratio_unknown_pass ({why})", flush=True)
                        self.stats["tx_ratio_unknown_pass"] = self.stats.get("tx_ratio_unknown_pass", 0) + 1
                        self.emit("info", f"gate: ${t.get('symbol')} tx_ratio_unknown_pass ({why})",
                                  stage="tx_ratio", reason="tx_ratio_unknown_pass", symbol=t.get("symbol"),
                                  mint=t["mint"])
                        f["tx_ratio_unknown_pass"] = True
                        f.setdefault("data_gaps", {})["buy_sell_5m"] = why
                        k = None
                    k = k or rulebook.lp_gate(dev)
                    if not k and (RULEBOOK.get("volume_rising_gate", True)
                                  or RULEBOOK.get("no_chase_gate", True)):
                        try:
                            rb5 = (candle_chart.series(t["mint"], "5m", session=self.session)
                                   .get("candles") or [])
                        except Exception:
                            rb5 = []
                        _now_s = time.time()
                        k = (rulebook.volume_gate(t, rb5, _now_s)
                             or rulebook.chase_gate(rb5, t, ds, _now_s))
                    if k == "no_chase":
                        self._count_breakout_vs_no_chase(t)
                    if k:
                        self._rule_skip(t, k)
                        continue

                # Dip-recovery chart reader, PAPER only. Classifies the last ~30 one-minute
                # candles (cache-first, rate-limited, no flood). Informational for the log,
                # analyst and chips; it only blocks a dead_after_dip coin, and only when
                # rulebook.dip_recovery_gate is ON (default OFF). Unknown never blocks.
                dip = None
                if self.mode != "live" and not t["mint"].startswith("DEMO"):
                    dip = dip_recovery.read(t, ds, self.session)
                    if dip["class"] in (dip_recovery.RECOVERED, dip_recovery.DEAD):
                        msg = f"dip: ${t.get('symbol')} {dip_recovery.describe(dip)}"
                        self.emit("info", msg, stage="dip", symbol=t.get("symbol"), mint=t["mint"],
                                  dip_class=dip["class"])
                        print(msg, flush=True)
                    if (k := dip_recovery.gate(dip)):
                        self._rule_skip(t, k)
                        continue

                # Entry trigger, PAPER only (Romz): RSI oversold turning up on short bars, or a
                # breakout above resistance on 2x average volume. Cache-first reads, no flood.
                # Computed before the analyst so Grok sees the RSI and the volume multiple.
                trig = None
                if self.mode != "live" and not t["mint"].startswith("DEMO"):
                    trig = entry_trigger.evaluate(t["mint"], session=self.session,
                                                  age_min=t.get("age_minutes"))
                    k = entry_trigger.gate(trig)
                    stats_up = []
                    if trig["fired"]:
                        stats_up = ["trigger_pass"] + [
                            "trigger_pass_" + entry_trigger.SHORT[n] for n in trig["fired_all"]]
                    elif trig["reason"] == entry_trigger.SKIP_NONE:
                        stats_up = ["trigger_none"]
                    elif trig["reason"]:
                        stats_up = ["trigger_unknown"]
                    with self.lock:
                        for stat in stats_up:
                            self.stats[stat] = self.stats.get(stat, 0) + 1
                    for _k in ("rsi_source", "vol_source"):
                        if trig.get(_k):
                            self._count_source(f"{_k[:3]}_{trig[_k]}")
                    if trig["vwap"].get("source"):
                        self._count_source(f"vwap_{trig['vwap']['source']}_{trig['vwap'].get('vwap_kind')}")
                    if trig["ema"].get("source"):
                        self._count_source(f"ema_{trig['ema']['source']}")
                    msg = f"trigger: ${t.get('symbol')} {entry_trigger.describe(trig)}"
                    print(msg, flush=True)
                    if trig["fired"]:
                        self.emit("info", msg, stage="trigger", symbol=t.get("symbol"), mint=t["mint"],
                                  **entry_trigger.tag(trig))
                    if k:
                        self._rule_skip(t, k)
                        continue

                # Memecoin tactics B (pullback), A (ATR stop) and D (volatility
                # ceiling) all need one thing the other seats do not produce:
                # recent candles. Fetched only when the operator switched one of
                # them on, so the default desk costs nothing extra.
                #
                # The bars are 5-minute, not the chart's default 15s scalp tab.
                # Rule B was measured as "20% below the 20-bar high" on 5m
                # candles, i.e. a 100-minute window. Asking for the default tab
                # made the live gate look at a 5-minute window instead -- a
                # different, much twitchier rule than the one that was tested.
                # Unknown values stay neutral (never a veto).
                if (MEME.get("pullback_entry") or MEME.get("pullback_shadow")
                        or MEME.get("atr_stop") or MEME.get("vol_ceiling")
                        or MEME.get("retest_entry")) \
                        and not t["mint"].startswith("DEMO"):
                    try:
                        cs = candle_chart.series(t["mint"], "5m", session=self.session)
                        candles = cs.get("candles") or []
                        # chart.py candles use short keys (h/l/c): reading only "high" left
                        # high_20 unset and the dip gate permanently neutral.
                        highs = [h for h in (meme_tactics.bar_high(c) for c in candles[-20:]) if h]
                        if highs:
                            f["high_20"] = max(highs)
                        if MEME.get("retest_entry"):
                            f["retest"] = meme_tactics.retest_state(candles)
                        # Same bars, same basis as the eval: ATR14 / close.
                        a = meme_tactics.atr_pct_from(candles)
                        if a is not None:
                            f["atr_pct"] = a
                        f["candle_tf"] = cs.get("tf")
                        f["candle_n"] = len(candles)
                    except Exception:
                        pass

                # Scalp entry rule (SCALP["entry_mode"], default moving_candle). It was a
                # setting nobody read; now a scalp-horizon buy needs the 5m candle to
                # agree. No candles -> neutral, so a brand-new launch is not starved.
                if (SCALP.get("enabled") and SCALP.get("entry_gate", True)
                        and not t["mint"].startswith("DEMO")
                        and exits.horizon_for(t.get("age_minutes")) == "scalp"):
                    try:
                        c5, cinfo = self._entry_candles(t["mint"], "5m")
                        if cinfo.get("sparse"):
                            # price-only tape with under 2 ticks in the forming bar: a flat bar
                            # would read as a red candle. Unknown, never a veto.
                            ce = {"ok": None, "why": "no candles", "mode": SCALP.get("entry_mode")}
                        else:
                            ce = meme_tactics.scalp_entry_state(
                                c5, SCALP.get("entry_mode"),
                                SCALP.get("moving_candle_bars"), SCALP.get("moving_candle_min_body_pct"),
                                now=time.time())
                        ce["source"] = cinfo.get("source")
                        if ce.get("ok") is None and cinfo.get("cause"):
                            ce["cause"] = cinfo["cause"]
                            ce["why"] = f"{ce['why']} ({cinfo['cause']})"
                        elif ce.get("ok") is None and cinfo.get("sparse"):
                            ce["cause"] = "sparse_tape"
                            ce["why"] = "no candles (sparse_tape)"
                    except Exception:
                        ce = {"ok": None, "why": "candle fetch failed"}
                    f["scalp_entry"] = ce
                    if ce.get("ok") is None and ce.get("cause"):
                        f.setdefault("data_gaps", {})["scalp_candles"] = ce["cause"]
                    if ce.get("ok") is None and SCALP.get("require_candles", True):
                        # P3 (2026-11-19): unknown (no candles / sparse tape / stale) no longer passes.
                        # 0/7 clean paper scalps won, and the two with no candles had their exit armed from
                        # a snapshot. No candles = no confirmed entry.
                        self.stats["scalp_no_candles"] = self.stats.get("scalp_no_candles", 0) + 1
                        self.bench.sit(t["mint"], "scalp_no_candles")
                        self.emit("reject", f"{t['symbol']}: scalp entry unknown, no candles ({ce.get('why')})",
                                  stage="entry", reason="scalp_no_candles", symbol=t["symbol"], mint=t["mint"])
                        journal("reject", stage="entry", mint=t["mint"], symbol=t["symbol"],
                                reason="scalp_no_candles", detail=ce)
                        continue
                    if ce.get("ok") is False:
                        self.bench.sit(t["mint"], "scalp_candle")
                        self.emit("reject", f"{t['symbol']}: scalp entry ({ce.get('mode')}): {ce['why']}",
                                  stage="entry", reason="scalp_candle", symbol=t["symbol"], mint=t["mint"])
                        journal("reject", stage="entry", mint=t["mint"], symbol=t["symbol"],
                                reason="scalp_candle", detail=ce)
                        continue

                # Confirmed scalp entry (PAPER): trend 1m+5m, buy pressure, turnover,
                # no falling knife, not overextended. A check with no data is n/a.
                if (CONFIRM.get("enabled", True) and self.mode != "live"
                        and not t["mint"].startswith("DEMO")
                        and exits.horizon_for(t.get("age_minutes")) == "scalp"):
                    if self._confirm_skip(t, ds, f):
                        continue

                # seat 2b+: free security / launch / promo signals (fail open to "unknown")
                sec = signals.security(t["mint"], self.session)
                if sec["block"]:
                    k = "sec_" + sec["block"][0]
                    self.bench.sit(t["mint"], k)
                    self.stats["sec_rejects"] += 1
                    self.emit("reject", f"{t['symbol']}: security flag blocked it "
                                        f"({', '.join(sec['block'])})",
                              stage="security", reason=k, symbol=t["symbol"], mint=t["mint"])
                    journal("reject", stage="security", mint=t["mint"], symbol=t["symbol"],
                            reason=k, security=sec)
                    continue

                # seat 2c: what gives this coin value (community / dev / narrative / distribution / utility)
                links = _demo_links(t["mint"]) if t["mint"].startswith("DEMO") \
                    else scanner.socials(t["mint"], self.session)

                # With Helius we can read the token's own metadata: the real name
                # and the socials the project published. Without it, we classify on
                # whatever the listing gave us. Either way the classifier is the
                # same; it just gets better inputs.
                if helius.configured() and not t["mint"].startswith("DEMO"):
                    md = helius.metadata(t["mint"], self.session)
                    if md:
                        t = {**t, "name": t.get("name") or md.get("name"),
                             "symbol": t.get("symbol") or md.get("symbol")}
                        ds = {**ds, "has_x": ds.get("has_x") or md["has_x"],
                              "has_telegram": ds.get("has_telegram") or md["has_telegram"],
                              "websites": ds.get("websites") or ([md["website"]] if md.get("website") else [])}
                        links = {k: links.get(k) or md.get(k)
                                 for k in ("x", "telegram", "website")}
                        links["x"] = links["x"] or ("https://x.com" if md["has_x"] else None)

                sc = meme.scorecard(t, f, ds, dev, fomo_ev=fomo_ev)
                if (k := meme.meme_kill(sc)):
                    self.bench.sit(t["mint"], k)
                    self.stats["meme_rejects"] += 1
                    self.emit("reject", f"{t['symbol']}: not a community coin "
                                        f"(meme {sc['total']}/100, {k})",
                              stage="meme", reason=k, symbol=t["symbol"], mint=t["mint"],
                              meme_score=sc["total"])
                    journal("reject", stage="meme", mint=t["mint"], symbol=t["symbol"],
                            reason=k, scorecard=sc)
                    continue

                # seat 3: analyst
                verdict_notes: list = []
                th = analyst.analyze_token(t, f, links, mode=self.mode, notes=verdict_notes,
                                           context={"scorecard": sc, "dev": dev, "ds": ds,
                                                    "security": {"flags": [c["t"] for c in sec["chips"]],
                                                                 "regime": self.regime.get("tag")},
                                                    "dip": dip,
                                                    "entry_trigger": entry_trigger.facts(trig)})
                analyst_left -= 1
                self.stats["analyst_calls"] += 1
                for n in verdict_notes:
                    self.stats["analyst_code_overrides"] = self.stats.get("analyst_code_overrides", 0) + 1
                    self.emit("info", f"analyst: code set ${t.get('symbol')} verdict {n['before']} -> {n['after']} "
                                      f"(score {n['score']}, serious flags {n['serious']})",
                              stage="analyst", symbol=t.get("symbol"), mint=t["mint"], **n)

                # seat 3b: the signal engine turns every seat's read into ONE
                # deterministic BUY / SELL / HOLD / AVOID recommendation, with a
                # direction (bullish/bearish), a confidence, a calibrated win
                # probability and an entry/stop/take-profit plan. Pure function:
                # it never sizes, never fills, and a hard security block always wins.
                sig = signal_engine.evaluate(t, f, links, thesis=th, scorecard=sc,
                                             security=sec, regime=self.regime, dev=dev)

                chips = list(sec["chips"])
                # seat 4: risk engine. Live money gets live caps, not paper percentages.
                horizon = exits.horizon_for(t.get("age_minutes"))
                stop = exits.profile(horizon)["stop_pct"]
                if self.mode == "live":
                    caps = {"max_trade": EXECUTION["live_max_trade_usd"],
                            "min_ticket": EXECUTION["live_min_ticket_usd"],
                            "max_notional": EXECUTION["live_max_notional_usd"],
                            "daily_loss_usd": self.live.loss_limit_usd(),
                            "deployed": self.live.position_notional()}
                    ok, ticket, reason = check_live_trade(
                        th, self.live.day_pnl(), self.live.open_count(),
                        t["liquidity_usd"], caps)
                    sizes = {**live_sizing_breakdown(t["liquidity_usd"], caps),
                             "horizon": horizon, "stop_pct": stop}
                    # The caution layer used to run ONLY on the paper path, so a
                    # real fill skipped the security halve, the regime halve, the
                    # breaker cut, the Jupiter impact cut and the pre-trade
                    # checklist entirely. Run it here too. The per-trade cap is
                    # the live cap, not a paper percentage of equity.
                    if ok:
                        acct = Account(equity=self.live.equity(), day_pnl=self.live.day_pnl(),
                                       open_positions=self.live.open_count(),
                                       realized_equity=self.live.realized_equity())
                        ok, ticket, reason, guard_info, chips = self._caution(
                            t, ticket, sec, brk, stop, acct, max_ticket=caps["max_trade"])
                        sizes["guard"] = guard_info
                else:
                    acct = Account(equity=self.broker.equity(), day_pnl=self.broker.day_pnl(),
                                   open_positions=self.broker.open_count(),
                                   realized_equity=self.broker.realized_equity(),
                                   cash=(self.paper.s.get("cash")
                                         if self.broker is self.paper else None))
                    ok, ticket, reason = check_token_trade(th, acct, t["liquidity_usd"],
                                                           stop_pct=stop,
                                                           daily_override=self.daily_override_today(),
                                                           age_minutes=t.get("age_minutes"))
                    sizes = {**sizing_breakdown(acct, t["liquidity_usd"], stop),
                             "horizon": horizon, "stop_pct": stop,
                             "sizing_base": round(acct.realized_equity, 2)}
                    if ok:
                        ok, ticket, reason, guard_info, chips = self._caution(
                            t, ticket, sec, brk, stop, acct)
                        sizes["guard"] = guard_info
                    # Risk budget: the daily stop must cover the WHOLE open book,
                    # not one ticket at a time. Six 1.5%-risk trades is 9% at risk
                    # on a 3% day. `fit_risk_budget` shrinks the ticket to what is
                    # left of the budget instead of refusing on a few cents of
                    # overrun -- refusing outright is what bricked the desk once
                    # open risk drifted above the budget.
                    if ok and self.broker is self.paper:
                        open_risk = open_risk_usd(self.paper.s["positions"])
                        floor = PAPER.get("min_ticket_usd", RISK["min_ticket_usd"])
                        ok, fitted, budget_reason = fit_risk_budget(
                            open_risk, ticket, stop, acct, floor)
                        budget = risk_budget_usd(acct)
                        sizes["open_risk_usd"] = round(open_risk, 2)
                        sizes["new_risk_usd"] = round(fitted * stop, 2)
                        sizes["risk_budget_usd"] = round(budget, 2)
                        if ok and fitted != ticket:
                            ticket = fitted
                            reason = budget_reason
                            sizes["ticket"] = round(ticket, 2)
                            self.emit("info", f"risk budget: {t['symbol']} {budget_reason}",
                                      stage="risk")
                        elif not ok:
                            reason = budget_reason
                            self.emit("info", f"risk budget: skip ${t['symbol']} \u2014 {reason}",
                                      stage="risk")
                if dip and dip["class"] in (dip_recovery.RECOVERED, dip_recovery.DEAD):
                    sizes["dip_recovery"] = dip      # information only: never changes the ticket
                    chips = list(chips) + [signals.chip(
                        "dip_recovered" if dip["class"] == dip_recovery.RECOVERED else "dip_dead",
                        "info", "dip")]
                if trig and trig.get("fired"):
                    sizes["entry_trigger"] = {**entry_trigger.tag(trig), "detail": entry_trigger.describe(trig)}
                    chips = list(chips) + [signals.chip(
                        entry_trigger.CHIP[trig["fired"]], "info", "trigger")]
                    chips += [signals.chip(entry_trigger.CHIP[n], "info", "trigger")
                              for n in trig["fired_all"][1:]]
                # the seats talk to each other before anything is filled
                convo = dialogue.debate(t, f, ds, dev, sc, th.model_dump(), ok, ticket,
                                        reason, sizes)
                self.stats["dialogues"] += 1
                transcript = {"mint": t["mint"], "symbol": t["symbol"], "time": _now(),
                              "backend": convo["backend"], "messages": convo["messages"],
                              "approved": ok, "ticket_usd": ticket}
                with self.lock:
                    self.transcripts.appendleft(transcript)

                decision = {
                    "mint": t["mint"], "symbol": t["symbol"], "token": t,
                    "facts": f, "links": links, "thesis": th.model_dump(),
                    "dexscreener": ds, "dev": dev, "scorecard": sc,
                    "dialogue": convo["messages"], "dialogue_backend": convo["backend"],
                    "approved": ok, "ticket_usd": ticket, "reason": reason,
                    "sizes": sizes, "time": _now(), "source": t.get("source"),
                    "flags": chips, "regime": self.regime.get("tag"),
                    "checklist": sizes.get("guard", {}).get("checklist"),
                    "signal": sig,
                    "dip_recovery": dip,
                    "entry_trigger": entry_trigger.tag(trig) if trig else None,
                }
                self._advise(t, f, dev, sc, sec, links, decision, th)   # advisory only, never blocks
                with self.lock:
                    self.decisions.appendleft(decision)
                journal("decision", **decision)

                self.emit("dialogue", f"{t['symbol']}: seats debated "
                                      f"({convo['backend']}, {len(convo['messages'])} messages)",
                          symbol=t["symbol"], mint=t["mint"], backend=convo["backend"])

                self.emit("thesis", f"{t['symbol']}: {th.verdict} {th.score}/10 "
                                    f"({th.shape})",
                          symbol=t["symbol"], mint=t["mint"], verdict=th.verdict,
                          score=th.score, shape=th.shape, red_flags=th.red_flags[:3])

                # the one-line buy/sell call, always shown, even when the desk passes
                self.emit("signal", sig["signal"], symbol=t["symbol"], mint=t["mint"],
                          action=sig["action"], direction=sig["direction"],
                          confidence=sig["confidence"], win_probability=sig["win_probability"],
                          prompt=sig["prompt"], shadow=sig.get("tactic_shadow"))
                sh = sig.get("tactic_shadow")
                if sh and sh.get("would_block") and sig.get("action") == "BUY":
                    self.stats["shadow_blocks"] += 1

                if not ok:
                    self.stats["risk_rejects"] += 1
                    self.bench.sit(t["mint"], th.verdict if th.verdict != "valid_setup" else "score")
                    self.emit("reject", f"{t['symbol']}: risk engine said no ({reason})",
                              stage="risk", reason=reason, symbol=t["symbol"])
                    continue

                # MERGE: demo pools are analysis-only, they never touch paper P&L
                if t["mint"].startswith("DEMO"):
                    self.emit("info", f"{t['symbol']}: demo pool, analysed but not filled")
                    continue

                # seat 3c: signal gate. The BUY read must clear the configured bar
                # (bullish + confidence + win-prob). Off with signal.enabled = false.
                allow, why = signal_engine.allows_entry(sig)
                if not allow:
                    self.stats["signal_rejects"] += 1
                    self.bench.sit(t["mint"], "signal")
                    self.emit("reject", f"{t['symbol']}: signal gate said no ({why})",
                              stage="signal", reason=why, symbol=t["symbol"], mint=t["mint"],
                              action=sig["action"], confidence=sig["confidence"])
                    journal("reject", stage="signal", mint=t["mint"], symbol=t["symbol"],
                            reason=why, signal=sig)
                    continue

                # seat 4b: FOMO thesis, bought only for finalists. Credits are real
                # (1,250 a page), so this runs after every other gate has passed and
                # only when the owner asked for the thesis level. A confident,
                # strongly bearish crowd is a veto; a mildly positive one is not.
                if (FOMO.get("enabled") and FOMO.get("level") == "thesis"
                        and not t["mint"].startswith("DEMO")):
                    try:
                        ev2 = fomo.token_evidence(t["mint"], "thesis")
                        if ev2.get("available"):
                            fomo_ev = ev2
                            devcheck.apply_fomo(dev, ev2)
                            sc = meme.scorecard(t, f, ds, dev, fomo_ev=ev2)
                            if "fomo_thesis_bearish" in (dev.get("hard_no") or []):
                                self.bench.sit(t["mint"], "fomo_thesis_bearish")
                                self.stats["dev_rejects"] += 1
                                self.emit("reject", f"{t['symbol']}: FOMO crowd is "
                                                    f"confidently bearish", stage="fomo",
                                          reason="fomo_thesis_bearish",
                                          symbol=t["symbol"], mint=t["mint"])
                                journal("reject", stage="fomo", mint=t["mint"],
                                        symbol=t["symbol"], reason="fomo_thesis_bearish",
                                        fomo=ev2)
                                continue
                    except Exception:
                        pass

                # Rulebook (PAPER): Jupiter price impact for the actual ticket. Reuses the
                # caution layer's quote (taken for a ticket >= this one, so an upper bound);
                # no quote / no route / rate-limited = skip.
                if self.mode != "live":
                    _g = sizes.get("guard") or {}
                    k = rulebook.impact_gate(_g.get("jupiter"), _g.get("ticket_before"), ticket)
                    if k:
                        self._rule_skip(t, k)
                        continue

                # MERGE (old desk research.py): confirm price + liquidity on a second read before filling
                buy_price = t["price_usd"]
                if self.mode != "live":
                    cq = scanner.quote(t["mint"], self.session)
                    bad = None
                    if not cq or not cq.get("price_usd"):
                        bad = "no_confirm_quote" if ENTRY["require_confirm_quote"] else None
                    else:
                        gap = abs(cq["price_usd"] / t["price_usd"] - 1) * 100 if t["price_usd"] else 999
                        # like for like: the confirm quote's liquidity and the discovery token's must be on
                        # the same basis (DexScreener's number is about twice Jupiter's on pump pools)
                        _cb = cq.get("liq_source") or "unknown"
                        _tb = scanner.liq_basis(t.get("source"))
                        liq_cmp = cq["liquidity_usd"]
                        liq_ratio = None
                        if _cb != _tb or _cb == "unknown":
                            alt_q = None
                            try:
                                alt_q = scanner.same_basis_quote(t["mint"], _tb, self.session) if _tb != "unknown" else None
                            except Exception:
                                alt_q = None
                            if alt_q and alt_q.get("liquidity_usd"):
                                liq_cmp = alt_q["liquidity_usd"]
                                liq_ratio = liq_cmp / t["liquidity_usd"] if t["liquidity_usd"] else 0
                                self.stats["confirm_liq_alt"] = self.stats.get("confirm_liq_alt", 0) + 1
                                print(f"confirm: ${t.get('symbol')} liquidity compared on {_tb}: scan "
                                      f"{t['liquidity_usd']:,.0f} [{_tb}] vs confirm {liq_cmp:,.0f} [{_tb}]", flush=True)
                            else:
                                self.stats["confirm_liq_mismatch"] = self.stats.get("confirm_liq_mismatch", 0) + 1
                                msg = (f"confirm: ${t.get('symbol')} liquidity ratio skipped, sources differ: scan "
                                       f"{t['liquidity_usd']:,.0f} [{_tb}] vs confirm {cq['liquidity_usd']:,.0f} [{_cb}]")
                                print(msg, flush=True)
                                self.emit("info", msg, stage="confirm", reason="confirm_liq_mismatch",
                                          symbol=t.get("symbol"), mint=t["mint"])
                        else:
                            liq_ratio = (cq["liquidity_usd"] / t["liquidity_usd"]) if t["liquidity_usd"] else 0
                        if gap > ENTRY["max_price_gap_pct"]:
                            bad = "price_gap"
                        elif liq_ratio is not None and liq_ratio < ENTRY["min_confirm_liq_ratio"]:
                            bad = "liq_unconfirmed"
                        else:
                            buy_price = cq["price_usd"]
                            ticket = round(min(ticket, RISK["max_pool_share"] * cq["liquidity_usd"]), 2)
                            floor = (RISK["min_ticket_usd"] if self.mode == "live"
                                     else PAPER.get("min_ticket_usd", RISK["min_ticket_usd"]))
                            if ticket < floor:
                                bad = "liq_unconfirmed"
                    if bad:
                        self.stats["risk_rejects"] += 1
                        self.bench.sit(t["mint"], bad)
                        self.emit("reject", f"{t['symbol']}: entry confirm failed ({bad})",
                                  stage="confirm", reason=bad, symbol=t["symbol"], mint=t["mint"])
                        snap = {
                            "scan_price": t.get("price_usd"),
                            "confirm_price": (cq or {}).get("price_usd") if cq else None,
                            "scan_liq": t.get("liquidity_usd"),
                            "confirm_liq": (cq or {}).get("liquidity_usd") if cq else None,
                        }
                        if snap["scan_price"] and snap["confirm_price"]:
                            snap["gap_pct"] = abs(snap["confirm_price"] / snap["scan_price"] - 1) * 100
                        journal("reject", stage="confirm", mint=t["mint"], symbol=t["symbol"],
                                reason=bad, token=token_snap(t), **snap)
                        continue

                cl = (sizes.get("guard") or {}).get("checklist")
                if cl is not None:
                    # Both modes confirm the price on a second read before filling:
                    # paper re-quotes via scanner.quote above, live re-quotes through
                    # Jupiter inside LiveBroker.buy(). Record it on the checklist.
                    cl["items"].append({"name": "price confirmed on a second read", "ok": True})
                    cl["passed"] += 1
                    cl["total"] += 1

                # seat 5: paper fill + alert
                flags = "; ".join(th.red_flags[:2]) or "none"
                alert(f"VALID SETUP ${t['symbol']} score {th.score}/10 | ticket ${ticket}\n"
                      f"mcap ${t['mcap_usd']:,.0f} liq ${t['liquidity_usd']:,.0f}\n"
                      f"red flags: {flags}\nhttps://dexscreener.com/solana/{t['mint']}")
                try:
                    _sh = sig.get("tactic_shadow")
                    _meta = ({"shadow_block": bool(_sh.get("would_block")),
                              "shadow_why": _sh.get("why"),
                              "shadow_drop": _sh.get("drop_pct")}
                             if _sh else {})
                    if self.mode != "live":
                        # rulebook fast exits compare against these
                        # entry liquidity with its source: the exit only compares like for like
                        _liqs = {}
                        _cq_l = (cq or {}).get("liquidity_usd")
                        if _cq_l and (cq or {}).get("liq_source"):
                            _liqs[cq["liq_source"]] = _cq_l
                        _t_b = scanner.liq_basis(t.get("source"))
                        if t.get("liquidity_usd") and _t_b != "unknown":
                            _liqs.setdefault(_t_b, t["liquidity_usd"])
                        _meta.update({"entry_liqs": _liqs,
                                      "entry_liq_src": ((cq or {}).get("liq_source") if _cq_l else
                                                        (_t_b if _t_b != "unknown" else None)),
                                      "entry_liq": (cq or {}).get("liquidity_usd") or t.get("liquidity_usd"),
                                      # same basis (RugCheck) as the exit's re-read
                                      "entry_top10": (dev.get("concentration") or {}).get("top_10_pct"),
                                      "entry_dev_hold": dev.get("creator_hold_pct")})
                        if f.get("entry_candle_low"):
                            _meta["entry_candle_low"] = min(f["entry_candle_low"], buy_price)
                        if trig and trig.get("fired"):
                            _meta.update({"entry_trigger": trig["fired"], "entry_rsi": trig.get("rsi_value"),
                                          "entry_rsi_tf": trig.get("rsi_tf"),
                                          "entry_vol_mult": trig.get("vol_mult"),
                                          "entry_triggers": list(trig.get("fired_all") or []),
                                          "entry_vwap": trig.get("vwap_value"),
                                          "entry_vwap_kind": trig.get("vwap_kind"),
                                          "entry_ema_fast": trig.get("ema_fast_value"),
                                          "entry_ema_slow": trig.get("ema_slow_value")})
                    fill = self.broker.buy(
                        t["mint"], t["symbol"], ticket, buy_price,
                        horizon=sizes.get("horizon", "swing"),
                        groq_mult=(sizes.get("guard", {}).get("groq") or {}).get("mult"),
                        meta=_meta or None)
                except live_mod.LiveError as e:
                    self.stats["errors"] += 1
                    self.emit("error", f"{t['symbol']}: live order refused \u2014 {e}",
                              symbol=t["symbol"], mint=t["mint"])
                    journal("reject", stage="live", mint=t["mint"], symbol=t["symbol"],
                            reason=str(e))
                    continue
                _tg = entry_trigger.tag(trig) if (trig and trig.get("fired")) else {}
                journal("entry", **fill, **{k: v for k, v in _tg.items() if k not in fill})
                pos_row = self.broker.s["positions"].get(t["mint"])
                if pos_row is not None and self.mode != "live":
                    # entry context for the ticket chips and the exit review (paper ledger only)
                    pos_row["meta"] = {"flags": chips, "decimals": f.get("decimals"),
                                       "regime": self.regime.get("tag")}
                    self.broker._save()
                self.stats["entries"] += 1
                self.last_entry_ts = time.time()
                tag = "LIVE BUY" if self.mode == "live" else "PAPER BUY"
                self.emit("entry", f"{tag} ${t['symbol']} \u2014 ticket ${ticket} "
                                   f"(score {th.score}/10)",
                          **{k: fill[k] for k in ("mint", "symbol", "qty", "fill", "ticket_usd")},
                          score=th.score, live=self.mode == "live", tx=fill.get("tx"), **_tg)
                self._mark_trade("buy", fill, usd=ticket, mcap_usd=t.get("mcap_usd"))
            except Exception as e:  # one bad token must not kill the cycle
                with self.lock:
                    self.stats["errors"] += 1
                self.emit("error", f"{t.get('symbol','?')}: seat error "
                                      f"({type(e).__name__}: {e})",
                          stage="cycle", symbol=t.get("symbol"), mint=t.get("mint"))
                journal("error", stage="cycle", mint=t.get("mint"), error=str(e))
                continue

        self.last_cycle_at = _now()
        journal("cycle", **self.stats, equity=self.broker.equity())
        self._snapshot_equity()
        self._log_funnel(cycle)
        return {"cycle": cycle, "source": source, "stats": dict(self.stats)}

    def _advise_exit(self, mint: str, pos: dict, q: dict | None, reason: str,
                     hold_sig: dict | None = None) -> None:
        """Ask the AI whether this exit is right, in a worker thread.

        ADVISORY ONLY and it can never delay or block the sell: the rule already
        decided, the order is already sent. The answer is emitted and journaled so
        you can compare the AI's read against the rule over time. One provider,
        one call, no debate — exits are rare, so this is cheap.
        """
        try:
            if not ADVISOR.enabled() or not wants_exit() or mint.startswith("DEMO"):
                return
            sym = pos.get("symbol") or mint[:6]
            summary = ADVISOR.summarize_exit(pos, q, reason, hold_sig)

            def done(res):
                if not res:
                    return
                why = "; ".join(res.get("reasons") or []) or "no reason given"
                self.emit("brain", f"AI on exit ${sym}: {res['verdict']} "
                                   f"{res['confidence']}% — {why}",
                          symbol=sym, mint=mint, verdict=res.get("verdict"))
                journal("brain_exit", mint=mint, symbol=sym, rule=reason,
                        verdict=res.get("verdict"), confidence=res.get("confidence"),
                        reasons=res.get("reasons"), provider=res.get("provider"))

            def log(line: str):
                self.emit("brain", line, symbol=sym, mint=mint)

            ADVISOR.submit_exit(mint, summary, done, log)
        except Exception:
            pass

    def _advise(self, t: dict, f: dict, dev: dict, sc: dict, sec: dict, links: dict,
                decision: dict, thesis=None) -> None:
        """Ask the AI second opinion about this coin, in a worker thread.

        ADVISORY ONLY: the result is stored on the decision record and logged. Nothing
        here is read by the risk engine, the broker, the kills or the mode, and a failure
        is swallowed. With no API keys set this does nothing. Only real candidates
        (analyst valid_setup at or above MIN_SCORE, after the scanner hard gates) are
        sent; a skip/watch or a weak score is left to the rule-based analyst alone.
        """
        try:
            if not ADVISOR.enabled() or str(t.get("mint", "")).startswith("DEMO"):
                return
            # BRAIN_ADVISE_ON=exit (the default) spends the free tier on sells only.
            if not wants_entry():
                return
            if not should_advise(thesis):
                return
            sym, mint = t.get("symbol"), t["mint"]

            def done(res):
                # all bots limited -> "resting": the rule-based analyst already decided alone
                decision["brain"] = res if res is not None else \
                    {"status": "resting" if ADVISOR.resting() else "unavailable", "advisory_only": True}

            def log(line: str):
                self.emit("brain", line, symbol=sym, mint=mint)
                print(f"[desk] {line}", flush=True)

            summary = ADVISOR.summarize(t, f, dev, sc, {**sec, "regime": self.regime.get("tag")}, links)
            decision["brain"] = {"status": "pending", "advisory_only": True}
            state = ADVISOR.submit(mint, summary, done, log)
            if state == "skipped":
                decision.pop("brain", None)
        except Exception:
            pass

    FUNNEL_EVERY = 10

    def _log_funnel(self, cycle: int) -> None:
        """Every N cycles, one log line: where candidates died, and entries so far. Read-only."""
        if cycle % self.FUNNEL_EVERY:
            return
        top = ", ".join(f"{r}={n}" for r, n in self.funnel.most_common(10)) or "none"
        st = self.stats
        print(f"[desk] funnel: cycles {cycle - self.FUNNEL_EVERY + 1}-{cycle} rejects {top}; "
              f"totals seen={st.get('seen', 0)} entries={st.get('entries', 0)} "
              f"exits={st.get('exits', 0)} open={self.broker.open_count()} "
              f"confirm checked={self.confirm_stats.get('checked', 0)} "
              f"passed={self.confirm_stats.get('passed', 0)}", flush=True)
        self.funnel.clear()

    def _caution(self, t: dict, ticket: float, sec: dict, brk: dict, stop: float,
                 acct: Account, *, max_ticket: float | None = None
                 ) -> tuple[bool, float, str, dict, list]:
        """The caution layer, run in BOTH paper and live. Tighten-only: it can
        shrink or refuse a ticket, never grow or admit one.

        Cuts: security halve-flags, RISK_OFF regime, drawdown breaker, high Jupiter
        impact, and the shrink-only Groq size opinion. They multiply (floor 0.25x).
        Then the pre-trade checklist must pass.

        `max_ticket` overrides the per-trade cap. Live passes its hard USD cap;
        paper leaves it None and uses a percentage of the sizing base.
        """
        chips = list(sec["chips"])
        cuts: list[tuple[str, float]] = []
        if sec["halve"]:
            cuts.append(("security", 0.5))
        if self.regime.get("halve"):
            cuts.append(("regime", 0.5))
            chips.append(signals.chip("regime_risk_off", "halve", "regime"))
        if brk["mult"] < 1.0:
            cuts.append(("breaker", brk["mult"]))
            chips.append(signals.chip("breaker_reduce", "halve", "guard"))
        jup = signals.entry_impact(t["mint"], ticket, self.session)
        chips += signals.impact_chips(jup)
        if jup.get("halve"):
            cuts.append(("jupiter", 0.5))
        # Groq second opinion on SIZE ONLY. Shrink-only by construction: the module
        # clamps to [0.25, 1.0] and fails open to 1.0, so a slow or wrong model can
        # only ever make a ticket smaller, never larger and never blocked. It runs
        # after every rule has already agreed, so it cannot admit a trade the desk
        # would otherwise refuse.
        groq_info = None
        if groq_sizer.enabled():
            gmult, groq_info = groq_sizer.size_mult(
                {**t, "dev": (t.get("dev") or {}), "stop_pct": stop, "ticket_usd": ticket},
                mint=t.get("mint", ""), symbol=t.get("symbol", ""),
                entry_ts=time.time(), log=lambda line: self.emit("brain", line))
            if gmult < 1.0:
                cuts.append(("groq", gmult))
                chips.append(signals.chip("groq_size", f"x{gmult:g}", "brain"))
        mult, names = guard.size_multiplier(cuts)
        new_ticket = round(ticket * mult, 2)
        # The fee floor is per-seat. Paper trades a $20 book and can afford a
        # $0.20 ticket; live money cannot (priority fee + ATA rent + slippage).
        # Using the paper floor on the live path let a $2 ticket through, and
        # using the live floor on the paper path deadlocked the simulator.
        if self.mode == "live":
            floor = EXECUTION["live_min_ticket_usd"]
        else:
            floor = PAPER.get("min_ticket_usd", RISK["min_ticket_usd"])
        # PAPER opt-in (default OFF): a cut ticket under the min is lifted to the min
        # instead of vetoed. Not for live, never for a 0 size; see risk.floor_cut_ticket.
        floored = False
        if self.mode != "live" and max_ticket is None:
            new_ticket, floored = risk.floor_cut_ticket(
                new_ticket, ticket, floor, getattr(acct, "cash", None), self.mode)
        cap = max_ticket if max_ticket is not None else RISK["max_position_pct"] * sizing_base(acct)
        # In live mode the checklist must assert the broker really is the live
        # broker and live trading is not disabled -- not "paper mode", which was
        # semantically backwards and would have failed every real fill.
        live_armed = (self.mode == "live" and self.broker is self.live
                      and not live_mod.LIVE_DISABLED)
        checklist = guard.pretrade_checklist(
            stop_pct=stop, ticket=new_ticket, max_ticket=cap,
            breaker=brk, block_flags=sec["block"], quote_confirmed=None,
            paper_only=self.mode != "live", live_armed=live_armed)
        info = {"mult": mult, "cuts": names, "breaker": brk["state"], "ticket_before": ticket,
                "jupiter_impact_pct": jup.get("impact_pct"), "checklist": checklist,
                "jupiter": {k: jup.get(k) for k in ("available", "route", "impact")}}
        if groq_info:
            info["groq"] = {k: groq_info.get(k) for k in
                            ("mult", "source", "confidence", "reasons", "latency_ms")}
        if not checklist["ok"]:
            self.stats["guard_rejects"] += 1
            return False, 0.0, "pre-trade checklist failed: " + "; ".join(checklist["failed"]), info, chips
        if new_ticket < floor:
            self.stats["guard_rejects"] += 1
            self.stats["floor_rejects"] += 1
            # A floor reject is a *configuration* problem, not a market signal:
            # the book is too small for the risk cap to survive a size cut. Say
            # so out loud (once per cycle) instead of silently skipping setups.
            need = risk.min_viable_equity(stop, mult)
            info["min_viable_equity"] = round(need, 2)
            if self._floor_warn_cycle != self.cycle_no:
                self._floor_warn_cycle = self.cycle_no
                self.emit(
                    "warn",
                    f"every ticket is under the ${floor} fee floor after a x{mult:g} cut "
                    f"({', '.join(names)}); a ${sizing_base(acct):.2f} book needs "
                    f"${need:.2f} to trade this profile. "
                    + ("Raise EXECUTION['live_max_trade_usd'] or wait for the breaker "
                       "to reset." if self.mode == "live"
                       else "Raise PAPER['starting_equity_usd'] or lower "
                            "PAPER['min_ticket_usd']."),
                    mint=t.get("mint"),
                )
            return (False, 0.0, f"ticket ${new_ticket} below fee floor ${floor} after caution cuts "
                                f"({', '.join(names)})", info, chips)
        reason = "approved" if not names else f"approved, size x{mult:g} ({', '.join(names)})"
        if floored:
            info["floored_to_min_ticket"] = True
            info["ticket_after_cuts"] = round(ticket * mult, 2)
            reason = (f"{risk.FLOORED_REASON}: ${info['ticket_after_cuts']} after cuts "
                      f"({', '.join(names)}) -> min ticket ${floor}")
            chips.append(signals.chip(risk.FLOORED_REASON, "info", "guard"))
            self.emit("info", f"{t.get('symbol', '?')}: {risk.FLOORED_REASON} "
                              f"(${info['ticket_after_cuts']} -> ${floor}, cuts: {', '.join(names)})",
                      stage="risk", mint=t.get("mint"))
        return True, new_ticket, reason, info, chips

    def _scan(self, force_demo: bool) -> tuple[list[dict], str]:
        if self.mode == "live":
            # real money never sees the synthetic feed, not even on a manual step
            try:
                live = scanner.new_tokens(self.session)
            except Exception as e:
                self.stats["errors"] += 1
                self.emit("error", f"scanner unavailable ({type(e).__name__}); "
                                   f"live mode will not substitute demo pools")
                return [], "live-no-data"
            return live, (scanner.source_label(live) if live else "live-no-data")
        if force_demo:
            return scanner.demo_tokens(8, seed=self.cycle_no * 7919 + 13), "demo"
        try:
            live = scanner.new_tokens(self.session)
        except Exception as e:
            self.stats["errors"] += 1
            self.emit("error", f"scanner unavailable ({type(e).__name__}), using demo feed")
            live = []
        if len(live) < 3 and AGENT["demo_when_empty"]:
            # top up with demo pools so the desk keeps exercising its rules
            demo = scanner.demo_tokens(5, seed=self.cycle_no * 7919 + 13)
            label = scanner.source_label(live) if live else "demo"
            return live + demo, (label + "+demo" if live else "demo")
        return live, scanner.source_label(live)

    def _rule_skip(self, t: dict, reason: str) -> None:
        """One concise line per rulebook skip; the bench keeps it from repeating."""
        self.bench.sit(t["mint"], reason)
        with self.lock:
            self.stats["rulebook_rejects"] = self.stats.get("rulebook_rejects", 0) + 1
        msg = f"gate: ${t.get('symbol')} skip {reason}"
        self.emit("reject", msg, stage="rulebook", reason=reason, symbol=t.get("symbol"),
                  mint=t["mint"])
        journal("reject", stage="rulebook", mint=t["mint"], symbol=t.get("symbol"), reason=reason)
        print(msg, flush=True)

    def _count_source(self, key: str) -> None:
        """Tiny per-source counters (which second source answered) for the funnel / state."""
        with self.lock:
            self.stats["src_" + key] = self.stats.get("src_" + key, 0) + 1

    def _count_breakout_vs_no_chase(self, t: dict) -> None:
        """no_chase is about to skip this coin. If a breakout trigger would have fired on it, count
        that collision (cache-only read, no request) so Romz can see what no_chase costs."""
        try:
            b = entry_trigger.read_breakout(t["mint"], time.time(), self.session, fetch=False)
        except Exception:
            return
        if b.get("state") != entry_trigger.FIRE:
            return
        with self.lock:
            self.stats["breakout_blocked_no_chase"] = self.stats.get("breakout_blocked_no_chase", 0) + 1
        print(f"trigger: ${t.get('symbol')} breakout ({b.get('vol_mult')}x vol) blocked by no_chase", flush=True)
        self.emit("info", f"trigger: ${t.get('symbol')} breakout blocked by no_chase", stage="trigger",
                  symbol=t.get("symbol"), mint=t["mint"], reason="breakout_blocked_no_chase")

    def _entry_candles(self, mint: str, tf: str):
        """1m / 5m candles for the scalp-entry rule and the confirm checklist (PAPER finalists).

        The chart's series first (GeckoTerminal cache / one call on the finalist lane, else the
        desk's own ticks). If that is empty or stale, GeckoTerminal 1m candles aggregated to the
        timeframe, else the Jupiter / quote price tape as price-only bars. Returns
        (candles, {"source", "cause", "sparse"}); a miss names the cause (gt_cooling, stale_ticks ...)."""
        now = time.time()
        cands, source = [], "none"
        try:
            cs = candle_chart.series(mint, tf, session=self.session, priority=True)
            cands, source = cs.get("candles") or [], cs.get("source") or "none"
        except Exception:
            pass
        if marketdata.fresh(cands, tf, now):
            self._count_source(f"candles_{tf}_{source}")
            return cands, {"source": source, "cause": None, "real": source == "geckoterminal"}
        memo = self.__dict__.get("_fb_memo")
        if memo and memo[0] == mint and now - memo[1] < 30:
            fb = memo[2]
        else:
            fb = marketdata.finalist_bars(mint, self.session, now)
            self._fb_memo = (mint, now, fb)
        if fb.get(tf):
            self._count_source(f"candles_{tf}_{fb['source']}")
            return fb[tf], {"source": fb["source"], "cause": None,
                            "real": (fb.get("via", fb["source"]) == "geckoterminal"),
                            "sparse": fb.get("via", fb["source"]) == "price_tape" and fb.get("tape_ticks_5m", 0) < 2
                            and tf == "5m", "age_s": fb.get("age_s")}
        self._count_source(f"candles_{tf}_missing")
        return cands, {"source": source if cands else "none", "cause": fb.get("cause") or "no_data",
                       "real": False}

    def _confirm_skip(self, t: dict, ds: dict | None, f: dict) -> bool:
        """Run the confirmed-entry checklist; True = skip this coin (logged + funnel)."""
        now = time.time()
        c5, c1, i5, i1 = [], [], {}, {}
        try:
            c5, i5 = self._entry_candles(t["mint"], "5m")
        except Exception:
            c5 = []
        try:
            c1, i1 = self._entry_candles(t["mint"], "1m")
        except Exception:
            c1 = []
        # buy/sell: the same 5m counts the tx-ratio gate used (DexScreener, else Jupiter / GeckoTerminal)
        tx = f.get("tx_m5")
        if tx is None and entry_confirm.buy_state(ds, t)["ok"] is None and f.get("tx_m5_cause") is None:
            try:
                alt = marketdata.tx_m5(t, ds, self.session)
            except Exception:
                alt = {"available": False, "cause": "error"}
            if alt.get("available"):
                tx = f["tx_m5"] = {k2: alt.get(k2) for k2 in ("buys_m5", "sells_m5", "source", "via", "age_s")
                                   if alt.get(k2) is not None}
            else:
                f["tx_m5_cause"] = alt.get("cause")
        cc = entry_confirm.checklist(t, ds, c1, c5, now, tx)
        f["confirm"] = cc
        gaps = f.setdefault("data_gaps", {})
        chk = cc["checks"]
        if chk["trend"]["ok"] is None and not c1 and not c5:
            gaps["trend"] = i5.get("cause") or i1.get("cause") or "no_candles"
        if chk["buy"]["ok"] is None:
            gaps["buy_sell_5m"] = f.get("tx_m5_cause") or "no_5m_txns"
        if chk["knife"]["ok"] is None and not c1:
            gaps["candles_1m"] = i1.get("cause") or "no_candles"
        cc["candles"] = {"1m": i1.get("source"), "5m": i5.get("source")}
        # The momentum exit's anchor comes from REAL exchange candles only (never price-tape snapshot bars or
        # tick buckets: GAYTA's anchor sat 9.6% above its fill and the exit fired on a green trade).
        low = entry_confirm.entry_candle_low(c1 if i1.get("real") else [], c5 if i5.get("real") else [], now)
        if low:
            f["entry_candle_low"] = low
        if not c1 and not c5 and SCALP.get("require_candles", True):
            self.stats["scalp_no_candles"] = self.stats.get("scalp_no_candles", 0) + 1
            self.bench.sit(t["mint"], "scalp_no_candles")
            self.emit("reject", f"{t.get('symbol')}: confirm has no candles ({gaps.get('trend')})",
                      stage="confirm_entry", reason="scalp_no_candles", symbol=t.get("symbol"), mint=t["mint"])
            journal("reject", stage="confirm_entry", mint=t["mint"], symbol=t.get("symbol"),
                    reason="scalp_no_candles", detail={"trend": gaps.get("trend")})
            return True
        with self.lock:
            self.confirm_stats["checked"] += 1
            self.confirm_stats["passed" if cc["ok"] else "failed"] += 1
            if not cc["ok"]:
                self.confirm_stats[cc["reason"]] += 1
            self.confirm_recent.appendleft({
                "at": int(now), "symbol": t.get("symbol"), "ok": cc["ok"], "fail": cc["fail"],
                "checks": {k: ({True: "pass", False: "FAIL", None: "n/a"}[v["ok"]], v["why"])
                           for k, v in cc["checks"].items()}})
        if cc["ok"]:
            print(f"confirm: ${t.get('symbol')} PASS {cc['passed']}/{cc['known']} known checks", flush=True)
            return False
        why = cc["checks"][cc["fail"]]["why"]
        self.bench.sit(t["mint"], cc["reason"])
        self.emit("reject", f"{t.get('symbol')}: confirm {cc['fail']} failed ({why})",
                  stage="confirm_entry", reason=cc["reason"], symbol=t.get("symbol"), mint=t["mint"])
        journal("reject", stage="confirm_entry", mint=t["mint"], symbol=t.get("symbol"),
                reason=cc["reason"], detail={k: v.get("why") for k, v in cc["checks"].items()})
        print(f"confirm: ${t.get('symbol')} skip {cc['reason']} ({why})", flush=True)
        return True

    def _rulebook_exit(self, mint: str, pos: dict, q: dict | None):
        """PAPER fast exits: momentum broke before TP1, liquidity -20% from entry,
        or the dev / top-10 sold. Missing data never forces a sell; it is logged once per position."""
        sym = pos.get("symbol") or mint[:6]
        if pos.get("horizon") == "scalp" and CONFIRM.get("momentum_exit", True):
            try:
                cs1 = candle_chart.series(mint, "1m", session=self.session)
                # arm only from real exchange 1m candles; tick buckets / tape bars are not candles
                c1 = (cs1.get("candles") or []) if cs1.get("source") == "geckoterminal" else []
                why = entry_confirm.momentum_exit(pos, c1, time.time())
            except Exception:
                why = None
            if why:
                print(f"exit: ${sym} {why}", flush=True)
                return (1.0, why)
        notes = []
        lc = rulebook.liq_check(pos, q)
        why, note = lc["reason"], (lc["note"] if lc["status"] in ("missing", "mismatch") else None)
        if lc["status"] in ("exit", "breach", "mismatch"):
            # both values and both sources, so a liquidity exit can be audited (and a skipped one explained)
            line = (f"liq: ${sym} {lc['status']} entry {lc.get('entry') or 0:,.0f} [{lc.get('entry_src')}] "
                    f"now {lc.get('now') or 0:,.0f} [{lc.get('now_src')}]"
                    + (f" drop {lc['drop']:.0%} reads {lc.get('reads')}" if lc.get("drop") is not None else ""))
            seen = self.__dict__.setdefault("_liq_log", {})
            key = (lc["status"], lc.get("reads"), lc.get("entry_src"), lc.get("now_src"))
            if seen.get(mint) != key:
                seen[mint] = key
                print(line, flush=True)
                self.emit("info", line, stage="liq_exit", reason=f"liq_{lc['status']}", symbol=pos.get("symbol"),
                          mint=mint)
                self.stats[f"liq_{lc['status']}"] = self.stats.get(f"liq_{lc['status']}", 0) + 1
        if note:
            notes.append(note)
        if not why and RULEBOOK.get("exit_on_holder_sell", True):
            if pos.get("entry_top10") is None and pos.get("entry_dev_hold") is None:
                notes.append("no entry holder snapshot")
            else:
                snap = devcheck.holder_snapshot(mint, self.session,
                                                max_age_s=float(RULEBOOK.get("holder_check_s", 120)))
                why, note = rulebook.holder_exit(pos, snap)
                if note:
                    notes.append(note)
        if why:
            print(f"exit: ${sym} {why}", flush=True)
            return (1.0, why)
        if notes:
            key = "; ".join(notes)
            seen = self.__dict__.setdefault("_rb_notes", {})
            if seen.get(mint) != key:
                seen[mint] = key
                print(f"exit: ${sym} rulebook no forced exit ({key})", flush=True)
        return None

    def _manage_positions(self):
        # Bind the seat ONCE. `self.broker` is a property that returns self.live
        # when mode == "live" and self.paper otherwise. The operator can arm or
        # disarm live from the dashboard at any moment, including mid-cycle. The
        # old code snapshotted `list(self.broker.s["positions"])` and then re-read
        # the property for every lookup, so a flip between the snapshot and the
        # lookup left us holding paper mints that do not exist in the live book:
        #   error: position management failed (KeyError: 'ANPik...pump')
        # Bind it here and never touch the property again inside this method.
        brk = self.broker
        book = brk.s["positions"]
        live_mints = [m for m in book if not m.startswith("DEMO")]
        quotes = scanner.quote_many(live_mints, self.session) if live_mints else {}
        try:    # one OHLCV refresh per cycle so the dashboard chart never has to fetch
            candle_chart.prefetch(self.session, [
                (m, (p or {}).get("horizon")) for m, p in book.items()])
        except Exception:
            pass
        for mint in list(book):
            # the book can change under us (a sell below, a reset, a mode flip
            # that already happened before brk was bound). Never assume the key
            # is still there.
            pos = book.get(mint)
            if not pos:
                continue
            if mint.startswith("DEMO"):
                q = scanner.demo_quote(mint, pos["last"])
            else:
                q = quotes.get(mint)
            if q and q.get("price_usd") and not mint.startswith("DEMO"):
                journal("quote", mint=mint, symbol=pos.get("symbol"),
                        price_usd=q.get("price_usd"), volume_h6=q.get("volume_h6"),
                        volume_h24=q.get("volume_h24"), liquidity_usd=q.get("liquidity_usd"))
            sig = brk.exit_signal(mint, q)
            if not sig and brk is self.paper and self.mode != "live" and not mint.startswith("DEMO"):
                sig = self._rulebook_exit(mint, book.get(mint) or {}, q)
            # Signal-driven exit: a strong bearish flip cuts a held position even when
            # no rule-based exit (TP / stop / trail / timeout) has fired yet. Rule-based
            # exits always take priority; this is the extra safety net. Configurable
            # off with signal.sell_on_bearish = false.
            hold_sig = None
            if not sig and not mint.startswith("DEMO"):
                try:
                    pos0 = book.get(mint) or {}
                    tok = dict(self.tokens_seen.get(mint) or {})
                    if q:
                        for _k in ("price_usd", "liquidity_usd", "volume_h6", "volume_h24"):
                            if q.get(_k) is not None:
                                tok[_k] = q.get(_k)
                    # fresh trajectory read: P&L vs entry, drawdown vs peak
                    _px = (q or {}).get("price_usd") or pos0.get("last")
                    if _px and pos0.get("entry"):
                        tok["change_h1"] = (_px / pos0["entry"] - 1.0) * 100.0
                    if _px and pos0.get("peak"):
                        tok["change_m5"] = (_px / pos0["peak"] - 1.0) * 100.0
                    if tok.get("age_minutes") is None and pos0.get("opened"):
                        tok["age_minutes"] = (time.time() - pos0["opened"]) / 60.0
                    hold_sig = signal_engine.evaluate(tok, holding=True)
                    _sell, _sreason = signal_engine.should_sell(hold_sig)
                    if _sell:
                        sig = (1.0, _sreason)
                        self.emit("signal", f"SELL ${pos0.get('symbol') or mint[:6]}: "
                                            f"{_sreason}", symbol=pos0.get("symbol"), mint=mint,
                                  action="SELL", direction=hold_sig["direction"],
                                  confidence=hold_sig["confidence"],
                                  win_probability=hold_sig["win_probability"])
                except Exception:
                    pass
            if sig:
                fraction, reason = sig
                pos = book.get(mint)
                if not pos:
                    continue
                # advisory only, never blocks or delays the sell below
                self._advise_exit(mint, pos, q, reason, hold_sig)
                # A stop that fired carries its own fill level (the stop price),
                # so a rug print cannot drag the fill far below the stop.
                price = pos.pop("fill_at", None) or pos["last"]
                entry = pos.get("entry")
                snap = {"peak": pos.get("peak"), "opened": pos.get("opened"),
                        "horizon": pos.get("horizon"), "meta": dict(pos.get("meta") or {}),
                        "qty": pos.get("qty") or 0.0}
                exit_jup = None
                if self.mode != "live" and not mint.startswith("DEMO"):
                    # advisory only: how hard would this sell move the price? (quote, never a swap)
                    exit_jup = signals.exit_impact(mint, snap["qty"] * fraction,
                                                   snap["meta"].get("decimals"), self.session)
                try:
                    fill = brk.sell(mint, fraction, price, reason)
                except live_mod.LiveError as e:
                    self.stats["errors"] += 1
                    self.emit("error", f"exit refused for {mint[:8]}\u2026 \u2014 {e}",
                              mint=mint)
                    continue
                journal("exit", **fill)
                self.stats["exits"] += 1
                if "pnl_usd" in fill:
                    self.bench.sit(mint, "exited")
                review = self._review(mint, fill, fraction, entry, snap, exit_jup)
                self.emit("exit", f"EXIT {fill['symbol']} {int(fraction * 100)}% — {reason}"
                           + (f" (pnl ${fill['pnl_usd']})" if "pnl_usd" in fill else ""),
                          fraction=fraction, entry=entry, review=review["note"],
                          exit_flags=review["exit_flags"], **fill)
                mult = None
                if entry and fill.get("fill"):
                    try:
                        if float(entry) > 0:
                            mult = round(float(fill["fill"]) / float(entry), 4)
                    except (TypeError, ValueError):
                        mult = None
                self._mark_trade("sell", fill, fraction=fraction, entry=entry, mult=mult,
                                 mcap_usd=self._mcap_of(mint))
                # Re-check the daily stop after EVERY realised exit. A batch of
                # rugs in one cycle used to realise the whole book before the
                # next cycle could flatten it; this closes the rest immediately.
                if self._paper_only():
                    try:
                        if daily_loss_hit(self._paper_account()):
                            self._enforce_daily_stop()
                            break
                    except Exception:
                        pass

    def _review(self, mint: str, fill: dict, fraction: float, entry, snap: dict,
                exit_jup: dict | None) -> dict:
        """Post-trade note for every exit. Journaled and shown; never changes a setting."""
        meta = snap.get("meta") or {}
        flags = [c.get("t") for c in meta.get("flags") or []]
        held = (time.time() - snap["opened"]) if snap.get("opened") else None
        rv = guard.review_exit(
            symbol=fill.get("symbol") or "?", reason=fill.get("reason") or "",
            entry=entry, exit_price=fill.get("fill"), peak=snap.get("peak"), held_s=held,
            pnl_usd=fill.get("pnl_usd"), horizon=snap.get("horizon"), flags=flags,
            regime=meta.get("regime"))
        exit_flags = []
        if exit_jup and exit_jup.get("available"):
            exit_flags = [signals.chip(f, "info", "jupiter") for f in exit_jup.get("flags") or []]
            if exit_jup.get("impact_pct") is not None:
                rv["note"] += f" | jupiter sell impact {exit_jup['impact_pct']}%"
                rv["jupiter_sell_impact_pct"] = exit_jup["impact_pct"]
        rv["exit_flags"] = exit_flags
        rv["time"] = _now()
        rv["mint"] = mint
        rv["fraction"] = fraction
        try:
            journal("review", **{k: v for k, v in rv.items() if k != "exit_flags"})
        except Exception:
            pass
        if "pnl_usd" in fill:                    # final close: feeds the coach
            with self.lock:
                self.reviews.appendleft(rv)
        return rv

    # ---------- read-only views for the dashboard ----------
    def chart(self, mint: str, tf: str | None = None) -> dict:
        """OHLC for one ticket. View-only; uses cached ticks / rate-limited OHLCV."""
        if not mint:
            return {"mint": "", "tf": tf or "15s", "tabs": ["15s", "30s"],
                    "candles": [], "source": "none", "fallback": False, "entry": None}
        with self.lock:
            pos = self.broker.s["positions"].get(mint) or {}
            tok = self.tokens_seen.get(mint) or {}
            entry = pos.get("entry")
            horizon = pos.get("horizon")
            age_h = (float(tok["age_minutes"]) / 60.0
                     if tok.get("age_minutes") is not None else None)
            session = self.session
        return candle_chart.series(
            mint, tf, horizon=horizon, age_hours=age_h, entry=entry, session=session)

    def status(self) -> dict:
        import config
        # status() takes the agent lock, and the cycle holds that same lock for
        # the whole scan (many seconds of network). The dashboard polls this, so
        # without a cache every poll queues behind a scan, and on the 512MB free
        # instance that queue is what got OOM-killed. A couple of seconds of
        # staleness is invisible on screen and keeps polls off the lock.
        now = time.time()
        hit = getattr(self, "_status_cache", None)
        if hit and now - hit[0] < _STATUS_TTL:
            return hit[1]
        with self.lock:
            broker = self.broker.snapshot()
            if self.mode != "live":
                # Tag trades that closed right after a settings change so tuning and
                # reviews can leave them out (the breaker already does).
                try:
                    from desk import settings as desk_settings
                    ct = desk_settings.change_times()
                    broker["closed"] = [dict(c, settings_artifact=True)
                                        if guard.is_settings_artifact(c, ct) else c
                                        for c in broker.get("closed") or []]
                except Exception:
                    pass
            seen = self.tokens_seen
            positions = []
            for p in broker.get("positions") or []:
                row = fomo_links.with_url(p)
                tok = seen.get(row.get("mint") or "") or {}
                created = created_at_of(tok)
                age_h = ((time.time() - created) / 3600.0) if created else (
                    float(tok["age_minutes"]) / 60.0 if tok.get("age_minutes") is not None else None)
                row["chart_tabs"] = candle_chart.tabs_for(
                    horizon=row.get("horizon"), age_hours=age_h)
                row["age_hours"] = age_h
                row["created_at"] = created
                if tok.get("mcap_usd") is not None:
                    row["mcap_usd"] = tok.get("mcap_usd")
                meta = (self.broker.s["positions"].get(row.get("mint")) or {}).get("meta") or {}
                row["flags"] = meta.get("flags") or []
                positions.append(row)
            broker["positions"] = positions
            decisions = []
            for d in list(self.decisions)[:40]:
                row = fomo_links.with_url(dict(d))
                tok = seen.get(row.get("mint") or "") or {}
                if not tok and isinstance(d.get("token"), dict):
                    tok = d["token"]
                row["created_at"] = created_at_of(tok)
                decisions.append(row)
            latest_buy = None
            for ev in self.events:
                if ev.get("kind") == "entry" and ev.get("mint"):
                    tok = seen.get(ev["mint"]) or {}
                    created = created_at_of(tok)
                    age_h = ((time.time() - created) / 3600.0) if created else (
                        float(tok["age_minutes"]) / 60.0 if tok.get("age_minutes") is not None else None)
                    meta = (self.broker.s["positions"].get(ev["mint"]) or {}).get("meta") or {}
                    latest_buy = fomo_links.with_url({
                        "flags": meta.get("flags") or [],
                        "symbol": ev.get("symbol"),
                        "mint": ev["mint"],
                        "ticket_usd": ev.get("ticket_usd"),
                        "fill": ev.get("fill"),
                        "qty": ev.get("qty"),
                        "score": ev.get("score"),
                        "message": ev.get("message"),
                        "time": ev.get("time"),
                        "chart_tabs": candle_chart.tabs_for(age_hours=age_h),
                        "created_at": created,
                    })
                    break
            snap = {
                "running": self.running,
                "cycle": self.cycle_no,
                "last_cycle_at": self.last_cycle_at,
                "data_source": self.data_source,
                "analyst": analyst.analyst_name(self.mode),
                "brain": analyst_ai.brain_view(self.mode, brain.health()),
                "analyst_ai": analyst_ai.status(),
                "advisor": ADVISOR.status(),
                "rpc": {**helius.health(), "endpoint": chain.rpc_source()},
                "execution": self.mode_info(),
                "stats": dict(self.stats),
                "broker": broker,
                "latest_buy": latest_buy,
                # The book's own basis, not config.PAPER. On Termux the ledger
                # persists across restarts and config changes, so a $50 book must
                # keep reporting $50 as its start even after the bankroll default
                # was raised to $160 -- otherwise the dashboard shows the right
                # equity next to a wrong multiple (e.g. 0.31x on a flat book).
                "starting_equity": (self.paper.s.get("start_equity")
                                    or PAPER["starting_equity_usd"]),
                "equity_curve": list(self.equity_curve),
                "curve_marks": list(self.curve_marks),
                # Proof, not a guess: where the chart lives on disk, how many
                # points it holds and how long ago it was written. A blank chart
                # with `exists: true` and 400 points is a frontend/tab problem;
                # `exists: false` is a data-dir problem; `points: 0` is a wipe.
                "curve": {**equity_curve.store_info(),
                          "restored_at_boot": self._curve_loaded},
                "latest_realized": self._latest_realized(broker),
                "activity": self._activity(seen)[:60],
                # Live view of the shrink-only Groq sizer: on/off, budget left,
                # and the last dozen decisions. No network, so polling is free.
                "groq_sizer": groq_sizer.status(),
                # Offline post-mortem: how many closed trades exist, whether the
                # 30-trade bar is met, and the last report if one was produced.
                # Reads two small files, never the network.
                "groq_postmortem": groq_postmortem.status(),
                # Which exit ladder is actually in force, and which source chose
                # it. Env outranks config.py, so a stale BRAIN_EXIT_LADDER makes
                # a config edit look like a no-op. Surfacing the source here is
                # the only way to see that from the dashboard.
                "exits": meme_tactics.ladder_info(),
                "guard": {
                    "breaker": dict(self.breaker),
                    "regime": dict(self.regime),
                    "coach": guard.coach(list(self.reviews)),
                    "reviews": [{k: r.get(k) for k in ("symbol", "note", "won", "pnl_usd", "time")}
                                for r in list(self.reviews)[:10]],
                },
                "integrations": self.integrations(),
                "risk": self.risk_status(),
                "events": list(self.events)[:120],
                "decisions": decisions,
                "transcripts": list(self.transcripts)[:12],
                "config": {
                    "min_score": config.MIN_SCORE,
                    "cycle_seconds": AGENT["cycle_seconds"],
                    "max_positions": config.RISK["max_open_positions"],
                    "stop_pct": config.EXITS["stop_pct"],
                    "take_half_at": config.EXITS["take_half_at"],
                    "meme_weights": config.MEME["weights"],
                    "meme_bands": config.MEME["bands"],
                    "max_dev_hold_pct": config.MEME["max_dev_hold_pct"],
                    "hold_horizons": config.HOLD_HORIZONS,
                },
            }
        self._status_cache = (now, snap)
        return snap

    def integrations(self) -> dict:
        """Status of each optional free source. Never includes a URL or a key."""
        hb = heartbeat.health()
        pp = pumpportal.health()
        gm = gmgn.health()
        return {
            "healthchecks": {"status": ("on" if hb["configured"] else
                                        "code ready, waiting on signup"),
                             "last_ok": hb.get("last_ok"), "fails": hb.get("fails")},
            "goplus": {"status": "on", "max_per_min": goplus.MAX_PER_MIN},
            "gmgn": {"status": "on" if gm["configured"] else "waiting on key",
                     "sleeping": gm["sleeping"]},
            "jupiter": {"status": "on (quotes only)", "max_rps": 0.5},
            "pumpportal": {"status": pumpportal.status_label(pp),
                           "tracked": pp["tracked"], "received": pp["received"],
                           "reconnects": pp["reconnects"], "error": pp.get("error")},
            "dexscreener_promo": {"status": "on"},
            "regime": {"status": "on", "tag": self.regime.get("tag")},
            "fomo": fomo.status(),
        }

    def reset(self):
        with self.lock:
            self.broker.reset()
            self.bench.clear()
            self.events.clear()
            self.decisions.clear()
            self.transcripts.clear()
            self.tokens_seen.clear()
            self.stats = {k: 0 for k in self.stats}
            self.cycle_no = 0
            self.data_source = "idle"
            self.equity_curve.clear()
            self.curve_marks.clear()
            self.reviews.clear()
            equity_curve.save(self.equity_curve, self.curve_marks)
        self._snapshot_equity()
        self.emit("info", f"Desk reset: {self.mode} book back to start, bench cleared.")
        return {"ok": True}
