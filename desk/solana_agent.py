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
from collections import Counter, deque
from datetime import datetime, timezone

import requests

from config import AGENT, ENTRY, EXECUTION, PAPER, RISK
from desk import (analyst, brain, chain, dexscreener, devcheck, dialogue, exits, goplus, gmgn,
                  guard, heartbeat, helius, meme, pumpportal, regime, scanner,
                  signals)
from desk import chart as candle_chart
from desk import links as fomo_links
from desk.age import created_at_of
from desk.autotune import AutoTune
from desk import equity_curve
from desk import live as live_mod
from desk.broker import PaperBroker
from desk.netutil import offline
from desk.ops import Bench, alert, journal, killed
from desk.risk import (Account, check_live_trade, check_token_trade,
                       live_sizing_breakdown, sizing_base, sizing_breakdown)


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
    def __init__(self, ledger="paper_ledger.json", bench_path="desk.db",
                 live_ledger="live_ledger.json"):
        self.paper = PaperBroker(ledger)
        self.live = live_mod.LiveBroker(live_ledger)
        self.mode = EXECUTION["default_mode"]          # always paper on boot
        self.bench = Bench(bench_path)
        # MERGE: count why tokens were benched, so autotune can loosen the gate that blocks most
        self.reject_reasons: Counter = Counter()
        _sit = self.bench.sit

        def _counting_sit(mint, reason, *a, **k):
            self.reject_reasons[reason] += 1
            return _sit(mint, reason, *a, **k)
        self.bench.sit = _counting_sit
        self.started_at = time.time()
        self.last_entry_ts: float | None = None
        self.autotune = AutoTune(self)
        self.session = requests.Session()
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.running = False
        self.cycle_no = 0
        self.last_cycle_at: str | None = None
        self.data_source = "idle"
        self.events: deque = deque(maxlen=AGENT["max_events"])
        self.decisions: deque = deque(maxlen=60)
        self.transcripts: deque = deque(maxlen=40)
        self.tokens_seen: dict[str, dict] = {}
        self.stats = {"cycles": 0, "seen": 0, "free_rejects": 0, "chain_rejects": 0,
                      "dev_rejects": 0, "meme_rejects": 0, "analyst_calls": 0,
                      "risk_rejects": 0, "sec_rejects": 0, "guard_rejects": 0, "entries": 0, "exits": 0, "dialogues": 0,
                      "errors": 0}
        self.equity_curve: deque = deque(maxlen=equity_curve.MAX_POINTS)
        self.curve_marks: deque = deque(maxlen=equity_curve.MAX_MARKS)
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
            "wallet": self.live.pubkey(),
            "confirm_phrase": EXECUTION["live_confirm_phrase"],
            "caps": {
                "max_trade": EXECUTION["live_max_trade_usd"],
                "max_notional": EXECUTION["live_max_notional_usd"],
                "daily_loss_usd": self.live.loss_limit_usd(),
                "max_slippage_bps": EXECUTION["live_max_slippage_bps"],
            },
            "paper_equity": self.paper.equity(),
            "live_pnl": self.live.equity(),
        }

    def set_mode(self, mode: str, confirm: str = "") -> dict:
        """Switch seat 5. Paper is instant and safe. Live needs the phrase + a key."""
        mode = (mode or "").strip().lower()
        if mode == "paper":
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
            return equity_curve.record_point(
                self.equity_curve, ts, self.broker.equity(), self.broker.day_pnl())

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

    # ---------- lifecycle ----------
    def start(self):
        with self.lock:
            if self.running:
                return {"ok": True, "message": "agent already running"}
            self._stop.clear()
            self.running = True
            self._thread = threading.Thread(target=self._loop, name="solana-agent", daemon=True)
            self._thread.start()
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
        for mint in list(self.broker.s["positions"]):
            price = self.broker.s["positions"][mint]["last"]
            entry = self.broker.s["positions"][mint].get("entry")
            fill = self.broker.sell(mint, 1.0, price, reason)
            journal("exit", **fill)
            self.emit("exit", f"closed {fill['symbol']} — {reason}", fraction=1.0, entry=entry, **fill)
            self._mark_trade("sell", fill, fraction=1.0, entry=entry, mcap_usd=self._mcap_of(mint))

    # ---------- one cycle ----------
    def run_once(self, force_demo: bool = False) -> dict:
        with self.lock:
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
        tokens, source = self._scan(force_demo)
        with self.lock:
            self.data_source = source

        # rank by volume churn: the loudest pools first
        tokens.sort(key=lambda t: t["volume_h1"] / max(t["mcap_usd"], 1), reverse=True)
        chain_left, analyst_left = AGENT["chain_budget"], AGENT["analyst_budget"]
        counts = scanner.source_counts(tokens)
        report = scanner.fetch_report()
        self.emit("scan",
                  f"cycle {cycle}: {len(tokens)} pools from {source} "
                  f"(gt={counts['geckoterminal']} ds={counts['dexscreener']}, "
                  f"calls gt={report['geckoterminal']['calls']} ds={report['dexscreener']['calls']})",
                  count=len(tokens), source=source, **counts)
        for warning in report.get("warnings") or []:
            self.emit("info", f"scanner: {warning}", stage="scanner")

        # discipline layer: market regime tag and drawdown breaker. Paper only, tighten-only.
        brk = {"state": "ok", "mult": 1.0, "reason": "ok"}
        if self.mode != "live":
            self.regime = ({"tag": "UNKNOWN", "halve": False} if (force_demo or offline())
                           else regime.current(self.session))
            brk = guard.circuit_breaker(self.broker.s["closed"], self.broker.day_pnl(),
                                        self.broker.s["day_start"])
            if brk["state"] != self.breaker.get("state"):
                self.emit("info" if brk["state"] == "ok" else "warn",
                          f"breaker: {brk['state']} ({brk['reason']})", stage="guard")
            self.breaker = brk
            if brk["mult"] <= 0:
                with self.lock:
                    for t in tokens:
                        self.tokens_seen[t["mint"]] = t
                tokens = []          # positions are still managed; only new entries pause

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
                if (k := devcheck.dev_kill(dev)):
                    self.bench.sit(t["mint"], k)
                    self.stats["dev_rejects"] += 1
                    self.emit("reject", f"{t['symbol']}: dev investigation said no ({k})",
                              stage="dev", reason=k, symbol=t["symbol"], mint=t["mint"],
                              dev_score=dev.get("dev_score"))
                    journal("reject", stage="dev", mint=t["mint"], symbol=t["symbol"],
                            reason=k, dev=dev)
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

                sc = meme.scorecard(t, f, ds, dev)
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
                th = analyst.analyze_token(t, f, links,
                                           context={"scorecard": sc, "dev": dev, "ds": ds,
                                                    "security": {"flags": [c["t"] for c in sec["chips"]],
                                                                 "regime": self.regime.get("tag")}})
                analyst_left -= 1
                self.stats["analyst_calls"] += 1

                chips = list(sec["chips"])
                # seat 4: risk engine. Live money gets live caps, not paper percentages.
                if self.mode == "live":
                    caps = {"max_trade": EXECUTION["live_max_trade_usd"],
                            "max_notional": EXECUTION["live_max_notional_usd"],
                            "daily_loss_usd": self.live.loss_limit_usd(),
                            "deployed": self.live.position_notional()}
                    ok, ticket, reason = check_live_trade(
                        th, self.live.day_pnl(), self.live.open_count(),
                        t["liquidity_usd"], caps)
                    sizes = live_sizing_breakdown(t["liquidity_usd"], caps)
                else:
                    horizon = exits.horizon_for(t.get("age_minutes"))
                    stop = exits.profile(horizon)["stop_pct"]
                    acct = Account(equity=self.broker.equity(), day_pnl=self.broker.day_pnl(),
                                   open_positions=self.broker.open_count(),
                                   realized_equity=self.broker.realized_equity())
                    ok, ticket, reason = check_token_trade(th, acct, t["liquidity_usd"],
                                                           stop_pct=stop)
                    sizes = {**sizing_breakdown(acct, t["liquidity_usd"], stop),
                             "horizon": horizon, "stop_pct": stop,
                             "sizing_base": round(acct.realized_equity, 2)}
                    if ok:
                        ok, ticket, reason, guard_info, chips = self._caution(
                            t, ticket, sec, brk, stop, acct)
                        sizes["guard"] = guard_info
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
                }
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

                # MERGE (old desk research.py): confirm price + liquidity on a second read before filling
                buy_price = t["price_usd"]
                if self.mode != "live":
                    cq = scanner.quote(t["mint"], self.session)
                    bad = None
                    if not cq or not cq.get("price_usd"):
                        bad = "no_confirm_quote" if ENTRY["require_confirm_quote"] else None
                    else:
                        gap = abs(cq["price_usd"] / t["price_usd"] - 1) * 100 if t["price_usd"] else 999
                        liq_ratio = (cq["liquidity_usd"] / t["liquidity_usd"]) if t["liquidity_usd"] else 0
                        if gap > ENTRY["max_price_gap_pct"]:
                            bad = "price_gap"
                        elif liq_ratio < ENTRY["min_confirm_liq_ratio"]:
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
                        journal("reject", stage="confirm", mint=t["mint"], symbol=t["symbol"], reason=bad)
                        continue

                cl = (sizes.get("guard") or {}).get("checklist")
                if cl is not None and self.mode != "live":
                    cl["items"].append({"name": "price confirmed on a second read", "ok": True})
                    cl["passed"] += 1
                    cl["total"] += 1

                # seat 5: paper fill + alert
                flags = "; ".join(th.red_flags[:2]) or "none"
                alert(f"VALID SETUP ${t['symbol']} score {th.score}/10 | ticket ${ticket}\n"
                      f"mcap ${t['mcap_usd']:,.0f} liq ${t['liquidity_usd']:,.0f}\n"
                      f"red flags: {flags}\nhttps://dexscreener.com/solana/{t['mint']}")
                try:
                    fill = self.broker.buy(t["mint"], t["symbol"], ticket, buy_price,
                                           horizon=sizes.get("horizon", "swing"))
                except live_mod.LiveError as e:
                    self.stats["errors"] += 1
                    self.emit("error", f"{t['symbol']}: live order refused \u2014 {e}",
                              symbol=t["symbol"], mint=t["mint"])
                    journal("reject", stage="live", mint=t["mint"], symbol=t["symbol"],
                            reason=str(e))
                    continue
                journal("entry", **fill)
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
                          score=th.score, live=self.mode == "live", tx=fill.get("tx"))
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
        try:
            self.autotune.tick()
        except Exception as e:   # tuning must never break a cycle
            self.emit("error", f"autotune failed ({type(e).__name__}: {e})")
        journal("cycle", **self.stats, equity=self.broker.equity())
        self._snapshot_equity()
        return {"cycle": cycle, "source": source, "stats": dict(self.stats)}

    def _caution(self, t: dict, ticket: float, sec: dict, brk: dict, stop: float,
                 acct: Account) -> tuple[bool, float, str, dict, list]:
        """Paper-only caution layer. Can shrink or refuse a ticket, never grow or admit one.

        Cuts: security halve-flags, RISK_OFF regime, drawdown breaker, high Jupiter
        impact. They multiply (floor 0.25x). Then the pre-trade checklist must pass.
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
        mult, names = guard.size_multiplier(cuts)
        new_ticket = round(ticket * mult, 2)
        floor = PAPER.get("min_ticket_usd", RISK["min_ticket_usd"])
        checklist = guard.pretrade_checklist(
            stop_pct=stop, ticket=new_ticket,
            max_ticket=RISK["max_position_pct"] * sizing_base(acct),
            breaker=brk, block_flags=sec["block"], quote_confirmed=None,
            paper_only=self.mode != "live")
        info = {"mult": mult, "cuts": names, "breaker": brk["state"], "ticket_before": ticket,
                "jupiter_impact_pct": jup.get("impact_pct"), "checklist": checklist}
        if not checklist["ok"]:
            self.stats["guard_rejects"] += 1
            return False, 0.0, "pre-trade checklist failed: " + "; ".join(checklist["failed"]), info, chips
        if new_ticket < floor:
            self.stats["guard_rejects"] += 1
            return (False, 0.0, f"ticket ${new_ticket} below fee floor ${floor} after caution cuts "
                                f"({', '.join(names)})", info, chips)
        reason = "approved" if not names else f"approved, size x{mult:g} ({', '.join(names)})"
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

    def _manage_positions(self):
        live_mints = [m for m in self.broker.s["positions"] if not m.startswith("DEMO")]
        quotes = scanner.quote_many(live_mints, self.session) if live_mints else {}
        for mint in list(self.broker.s["positions"]):
            if mint.startswith("DEMO"):
                q = scanner.demo_quote(mint, self.broker.s["positions"][mint]["last"])
            else:
                q = quotes.get(mint)
            sig = self.broker.exit_signal(mint, q)
            if sig:
                fraction, reason = sig
                pos = self.broker.s["positions"][mint]
                price = pos["last"]
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
                    fill = self.broker.sell(mint, fraction, price, reason)
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
        with self.lock:
            broker = self.broker.snapshot()
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
                        "score": ev.get("score"),
                        "message": ev.get("message"),
                        "time": ev.get("time"),
                        "chart_tabs": candle_chart.tabs_for(age_hours=age_h),
                        "created_at": created,
                    })
                    break
            return {
                "running": self.running,
                "cycle": self.cycle_no,
                "last_cycle_at": self.last_cycle_at,
                "data_source": self.data_source,
                "analyst": analyst.analyst_name(),
                "brain": brain.health(),
                "rpc": {**helius.health(), "endpoint": chain.rpc_source()},
                "execution": self.mode_info(),
                "stats": dict(self.stats),
                "autotune": self.autotune.snapshot(),
                "broker": broker,
                "latest_buy": latest_buy,
                "starting_equity": PAPER["starting_equity_usd"],
                "equity_curve": list(self.equity_curve),
                "curve_marks": list(self.curve_marks),
                "latest_realized": self._latest_realized(broker),
                "activity": self._activity(seen)[:60],
                "guard": {
                    "breaker": dict(self.breaker),
                    "regime": dict(self.regime),
                    "coach": guard.coach(list(self.reviews)),
                    "reviews": [{k: r.get(k) for k in ("symbol", "note", "won", "pnl_usd", "time")}
                                for r in list(self.reviews)[:10]],
                },
                "integrations": self.integrations(),
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
        self._snapshot_equity()
        self.emit("info", f"Desk reset: {self.mode} book back to start, bench cleared.")
        return {"ok": True}
