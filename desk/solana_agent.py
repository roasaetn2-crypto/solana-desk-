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
from collections import deque
from datetime import datetime, timezone

import requests

from config import AGENT, EXECUTION
from desk import (analyst, brain, chain, dexscreener, devcheck, dialogue, helius,
                  meme, scanner)
from desk import live as live_mod
from desk.broker import PaperBroker
from desk.ops import Bench, alert, journal, killed
from desk.risk import (Account, check_live_trade, check_token_trade,
                       live_sizing_breakdown, sizing_breakdown)


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
                      "risk_rejects": 0, "entries": 0, "exits": 0, "dialogues": 0,
                      "errors": 0}

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

    # ---------- lifecycle ----------
    def start(self):
        with self.lock:
            if self.running:
                return {"ok": True, "message": "agent already running"}
            self._stop.clear()
            self.running = True
            self._thread = threading.Thread(target=self._loop, name="solana-agent", daemon=True)
            self._thread.start()
        self.emit("info", "Solana Agent started. Watching new Solana pools.")
        return {"ok": True, "message": "agent started"}

    def stop(self):
        with self.lock:
            self.running = False
            self._stop.set()
        self.emit("info", "Solana Agent paused.")
        return {"ok": True, "message": "agent paused"}

    def _loop(self):
        while not self._stop.is_set():
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
            fill = self.broker.sell(mint, 1.0, price, reason)
            journal("exit", **fill)
            self.emit("exit", f"closed {fill['symbol']} — {reason}", **fill)

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
        self.emit("scan", f"cycle {cycle}: {len(tokens)} pools from {source}",
                  count=len(tokens), source=source)

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
                                           context={"scorecard": sc, "dev": dev, "ds": ds})
                analyst_left -= 1
                self.stats["analyst_calls"] += 1

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
                    acct = Account(equity=self.broker.equity(), day_pnl=self.broker.day_pnl(),
                                   open_positions=self.broker.open_count())
                    ok, ticket, reason = check_token_trade(th, acct, t["liquidity_usd"])
                    sizes = sizing_breakdown(acct, t["liquidity_usd"])
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

                # seat 5: paper fill + alert
                flags = "; ".join(th.red_flags[:2]) or "none"
                alert(f"VALID SETUP ${t['symbol']} score {th.score}/10 | ticket ${ticket}\n"
                      f"mcap ${t['mcap_usd']:,.0f} liq ${t['liquidity_usd']:,.0f}\n"
                      f"red flags: {flags}\nhttps://dexscreener.com/solana/{t['mint']}")
                try:
                    fill = self.broker.buy(t["mint"], t["symbol"], ticket, t["price_usd"])
                except live_mod.LiveError as e:
                    self.stats["errors"] += 1
                    self.emit("error", f"{t['symbol']}: live order refused \u2014 {e}",
                              symbol=t["symbol"], mint=t["mint"])
                    journal("reject", stage="live", mint=t["mint"], symbol=t["symbol"],
                            reason=str(e))
                    continue
                journal("entry", **fill)
                self.stats["entries"] += 1
                tag = "LIVE BUY" if self.mode == "live" else "PAPER BUY"
                self.emit("entry", f"{tag} ${t['symbol']} \u2014 ticket ${ticket} "
                                   f"(score {th.score}/10)",
                          **{k: fill[k] for k in ("mint", "symbol", "qty", "fill", "ticket_usd")},
                          score=th.score, live=self.mode == "live", tx=fill.get("tx"))
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
        return {"cycle": cycle, "source": source, "stats": dict(self.stats)}

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
            return live, ("geckoterminal" if live else "geckoterminal-empty")
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
            return live + demo, ("geckoterminal+demo" if live else "demo")
        return live, "geckoterminal"

    def _manage_positions(self):
        for mint in list(self.broker.s["positions"]):
            if mint.startswith("DEMO"):
                q = scanner.demo_quote(mint, self.broker.s["positions"][mint]["last"])
            else:
                q = scanner.quote(mint, self.session)
            sig = self.broker.exit_signal(mint, q)
            if sig:
                fraction, reason = sig
                price = self.broker.s["positions"][mint]["last"]
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
                self.emit("exit", f"EXIT {fill['symbol']} {int(fraction * 100)}% — {reason}"
                           + (f" (pnl ${fill['pnl_usd']})" if "pnl_usd" in fill else ""),
                          **fill)

    # ---------- read-only views for the dashboard ----------
    def status(self) -> dict:
        import config
        with self.lock:
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
                "broker": self.broker.snapshot(),
                "events": list(self.events)[:120],
                "decisions": list(self.decisions)[:40],
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
        self.emit("info", f"Desk reset: {self.mode} book back to start, bench cleared.")
        return {"ok": True}
