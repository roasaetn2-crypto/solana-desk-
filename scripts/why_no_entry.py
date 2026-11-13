#!/usr/bin/env python3
"""Why is the desk not opening entries? And is take-profit actually armed?

Read-only. This script never places an order, never changes a setting and never
calls the network. It answers two questions with evidence from the files the desk
already wrote:

  1. Which gate is actually rejecting tokens? (journal.jsonl, grouped by stage)
  2. Is the take-profit ladder live for the current style? (config + a synthetic
     scalp position run through desk.exits.evaluate)

Usage (from the repo root, Termux included):

    python scripts/why_no_entry.py
    python scripts/why_no_entry.py --hours 6
    python scripts/why_no_entry.py --json

Exit code is 0 when the desk looks able to enter, 2 when a hard gate is blocking
(daily stop, breaker cooldown, full book, TP not armed), so a shell script can
branch on it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from config import ENTRY, EXITS, PAPER, RISK, SCAN, SIGNAL  # noqa: E402
from desk import exits, guard  # noqa: E402
from desk.risk import Account, daily_loss_hit, daily_loss_limit_usd  # noqa: E402


# ---------- file locations (honours DESK_DATA_DIR, like the desk itself) ----------
def data_dir() -> Path:
    raw = (os.environ.get("DESK_DATA_DIR") or "").strip()
    return Path(raw) if raw else ROOT


def _read_jsonl(path: Path, tail: int) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-tail:] if tail and len(rows) > tail else rows


def _ts(row: dict) -> float | None:
    v = row.get("time")
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------- reason normalisation so the tally is readable ----------
def norm_reason(stage: str, reason: str) -> str:
    r = str(reason or "?").strip()
    low = r.lower()
    if stage == "risk":
        if low.startswith("score "):
            return "risk: thesis score below min"
        if low.startswith("analyst verdict"):
            return "risk: analyst verdict not valid_setup"
        if low.startswith("open risk"):
            return "risk: open-risk budget would be exceeded"
        if "below fee floor" in low:
            return "risk: ticket below fee floor"
        if low == "max open positions":
            return "risk: book full (max open positions)"
        if low.startswith("paper cash is negative"):
            return "risk: paper cash negative (reset ledger)"
        if low.startswith("quiet hours"):
            return "risk: quiet hours"
        if "daily" in low:
            return "risk: daily loss stop"
    if stage == "confirm":
        return {
            "no_confirm_quote": "confirm: no second quote came back",
            "price_gap": "confirm: price moved past gap cap",
            "liq_unconfirmed": "confirm: liquidity failed re-check",
        }.get(r, f"confirm: {r}")
    if stage == "signal":
        if "win prob" in low:
            return "signal: win probability below floor"
        if "bearish" in low:
            return "signal: bearish read"
        if "need buy" in low:
            return "signal: not a BUY read"
        return f"signal: {r}"
    return f"{stage}: {r}"


def _money(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "?"


def load_ledger(d: Path) -> dict:
    p = d / "paper_ledger.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def ledger_view(led: dict) -> dict:
    """Recompute the numbers the risk gate uses, without constructing a broker."""
    if not led:
        return {"present": False}
    positions = led.get("positions") or {}
    cash = float(led.get("cash") or 0.0)
    start = float(led.get("start_equity") or PAPER["starting_equity_usd"])
    day_start = float(led.get("day_start") or start)
    held = 0.0
    for p in positions.values():
        qty = float(p.get("qty") or 0.0)
        last = float(p.get("last") or p.get("entry") or 0.0)
        held += qty * last
    equity = round(cash + held, 2)
    day_pnl = round(equity - day_start, 2)
    acct = Account(equity=equity, day_pnl=day_pnl, open_positions=len(positions),
                   realized_equity=start, cash=cash)
    limit = daily_loss_limit_usd(acct)
    halted = daily_loss_hit(acct)
    override_date = led.get("daily_loss_override_date")
    override = bool(override_date) and str(override_date) == str(date.today())
    brk = guard.circuit_breaker(led.get("closed") or [], day_pnl, day_start)
    return {
        "present": True, "cash": cash, "equity": equity, "day_start": day_start,
        "day_pnl": day_pnl, "day": led.get("day"), "open_positions": len(positions),
        "positions": positions, "daily_limit_usd": round(limit, 2),
        "daily_halted": halted, "override_today": override,
        "blocking_entries": halted and not override,
        "breaker": brk,
    }


def tp_view() -> dict:
    """Is the take-profit ladder armed, and which rungs will actually fire?"""
    style = str(EXITS.get("style", "both"))
    mode = str(EXITS.get("take_profit_mode") or "profile")
    prof = exits.profile("scalp")
    if mode == "single":
        rungs = [{"at": prof["tp1_at"], "sell": prof["tp1_frac"], "label": "single TP"}]
    else:
        rungs = [{"at": prof["tp1_at"], "sell": prof["tp1_frac"], "label": "TP1"},
                 {"at": prof["tp2_at"], "sell": prof["tp2_frac"], "label": "TP2"}]
    active = bool(rungs) and all(r["at"] > 1.0 and r["sell"] > 0 for r in rungs)
    return {"style": style, "mode": mode, "scalp": prof, "rungs": rungs, "active": active}


def tp_self_test() -> dict:
    """Run a fake scalp position up through the ladder. Proves TP can fire."""
    now = time.time()
    pr = exits.profile("scalp")
    pos = {"entry": 1.0, "opened": now - 120, "last": 1.0, "peak": 1.0,
           "stop_pct": pr["stop_pct"], "stop_price": 1.0 * (1 - pr["stop_pct"]),
           "stage": 0, "miss": 0, "horizon": "scalp", "qty": 100.0, "cost": 100.0}
    out: dict = {"tp1": None, "tp2": None, "stop": None}

    # cross the first take-profit rung
    q = {"price_usd": pr["tp1_at"] + 0.01, "volume_h24": 0, "volume_h6": 0}
    r = exits.evaluate(pos, q, now)
    out["tp1"] = r[1] if r else None
    # continue to the second rung (single/global may have no second rung)
    if pos["stage"] >= 1 and pr["tp2_at"] < 1e8:
        q2 = {"price_usd": min(pr["tp2_at"], 50.0) + 0.01, "volume_h24": 0, "volume_h6": 0}
        r2 = exits.evaluate(pos, q2, now)
        out["tp2"] = r2[1] if r2 else None
    # a fresh position dropped through the stop must close
    p2 = {"entry": 1.0, "opened": now - 60, "last": 1.0, "peak": 1.0,
          "stop_pct": pr["stop_pct"], "stop_price": 1.0 * (1 - pr["stop_pct"]),
          "stage": 0, "miss": 0, "horizon": "scalp"}
    r3 = exits.evaluate(p2, {"price_usd": p2["stop_price"] * 0.99,
                             "volume_h24": 0, "volume_h6": 0}, now)
    out["stop"] = r3[1] if r3 else None
    out["ok"] = bool(out["tp1"] and out["stop"])
    return out


def gate_view() -> dict:
    return {
        "style": EXITS.get("style", "both"),
        "scalp_max_age_hours": EXITS.get("scalp_max_age_hours"),
        "take_profit_mode": EXITS.get("take_profit_mode"),
        "analyst_min_score": config.MIN_SCORE,
        "signal_enabled": SIGNAL.get("enabled"),
        "signal_min_confidence": SIGNAL.get("min_confidence"),
        "signal_min_win_probability": SIGNAL.get("min_win_probability"),
        "require_confirm_quote": ENTRY.get("require_confirm_quote"),
        "max_price_gap_pct": ENTRY.get("max_price_gap_pct"),
        "min_confirm_liq_ratio": ENTRY.get("min_confirm_liq_ratio"),
        "max_h1_change_pct": SCAN.get("max_h1_change_pct"),
        "max_age_hours": SCAN.get("max_age_hours"),
        "min_age_minutes": SCAN.get("min_age_minutes"),
        "max_open_positions": RISK.get("max_open_positions"),
        "max_daily_loss_pct": RISK.get("max_daily_loss_pct"),
        "max_daily_loss_usd": RISK.get("max_daily_loss_usd"),
        "auto_continue": RISK.get("auto_continue_after_daily_stop"),
        "min_ticket_usd": PAPER.get("min_ticket_usd"),
    }


FIXES = {
    "scanner: chase": "scan.max_h1_change_pct is capping hot coins; raise it (20-2000) or let Desk AI propose it.",
    "scanner: age": "scan.max_age_hours / min_age_minutes window is too tight for the coins you scan.",
    "scanner: liquidity": "scan.min_liquidity_usd is above what your feed shows.",
    "scanner: volume": "scan.min_volume_h1_usd is above what your feed shows.",
    "scanner: trades": "scan.min_trades_h1 is above what your feed shows.",
    "scanner: style": "trading.style is scalp/swing only and these coins are the other horizon; use 'both'.",
    "risk: thesis score below min": "analyst.valid_score is high for this market; lower it by 1 or let Desk AI propose it.",
    "risk: analyst verdict not valid_setup": "The analyst is not convinced; check red_flags on the last decisions.",
    "risk: open-risk budget would be exceeded": "Open positions already use the day's risk; let one exit first.",
    "risk: ticket below fee floor": "Book is too small for the per-pool cap; raise bankroll or max_pool_share.",
    "confirm: price moved past gap cap": "entry.max_price_gap_pct is too tight for volatile memecoins; raise it (2-50).",
    "confirm: liquidity failed re-check": "entry.min_confirm_liq_ratio or the pool cap is rejecting the re-read.",
    "confirm: no second quote came back": "The confirm quote endpoint is failing; check the feed/network.",
    "signal: not a BUY read": "signal.min_confidence is high; lower it or disable the signal gate.",
    "signal: win probability below floor": "signal.min_win_probability is high for this regime.",
}


def analyse(hours: float, tail: int) -> dict:
    d = data_dir()
    rows = _read_jsonl(d / "journal.jsonl", tail)
    now = time.time()
    cutoff = now - hours * 3600

    in_window = [r for r in rows if (_ts(r) or 0) >= cutoff]
    kinds = Counter(str(r.get("kind")) for r in in_window)

    rejects = [r for r in in_window if r.get("kind") == "reject"]
    tally = Counter(norm_reason(str(r.get("stage") or "?"), r.get("reason")) for r in rejects)
    raw_tally = Counter(f"{r.get('stage')}: {r.get('reason')}" for r in rejects)

    entries = [r for r in in_window if r.get("kind") == "entry"]
    last_entry = None
    for r in reversed(rows):
        if r.get("kind") == "entry":
            last_entry = _ts(r)
            break

    symbols = Counter(str(r.get("symbol") or "?") for r in rejects)

    led = ledger_view(load_ledger(d))
    tp = tp_view()
    test = tp_self_test()
    gates = gate_view()

    # ---- verdict: the single most likely reason nothing fills ----
    # Hard account-level blocks are checked before "no journal", because a halted
    # book explains silence even when the journal is empty or rotated.
    verdict, fix, hard = "Gates look open; entries should fire on the next valid setup.", "", False
    if led.get("blocking_entries"):
        verdict = (f"Daily loss stop is blocking ALL new entries "
                   f"(day P&L {_money(led['day_pnl'])} vs limit {_money(led['daily_limit_usd'])}).")
        fix = ("Resume paper entries for today: curl -X POST http://127.0.0.1:8000/api/risk/continue "
               "(or set risk.auto_continue_after_daily_stop = true).")
        hard = True
    elif (led.get("breaker") or {}).get("mult", 1) <= 0:
        verdict = f"Circuit breaker paused entries: {led['breaker'].get('reason')}."
        fix = "Wait for the cooldown, or review the losing streak in the journal."
        hard = True
    elif led.get("open_positions", 0) >= int(RISK.get("max_open_positions", 6)):
        verdict = (f"Book is full: {led['open_positions']}/{RISK.get('max_open_positions')} "
                   f"positions open, so no new entries until one exits.")
        fix = ("Check exits: make sure trading.style matches the coins you scan and TP is armed "
               "(this report's take-profit section).")
        hard = True
    elif not rows:
        verdict = "No journal found, so the desk has not run a cycle here yet."
        fix = f"Start the desk (bash termux/start-desk.sh) and check DESK_DATA_DIR={d}."
    elif tally:
        top, n = tally.most_common(1)[0]
        verdict = f"Top reject in the last {hours:g}h: {top} ({n} times)."
        fix = FIXES.get(top, "Read the last rejects below and adjust that gate.")
    if not tp["active"]:
        verdict += " Take-profit is NOT fully armed."
        fix = (fix + " " if fix else "") + "Set exits.tp_plan = profile so the scalp ladder fires."
        hard = True
    if not test["ok"]:
        verdict += " TP self-test FAILED."
        fix = (fix + " " if fix else "") + "Inspect desk/exits.py::evaluate before trading."
        hard = True

    return {
        "data_dir": str(d), "journal_rows": len(rows), "window_hours": hours,
        "kinds": dict(kinds), "rejects": len(rejects), "entries": len(entries),
        "last_entry_age_min": (round((now - last_entry) / 60, 1) if last_entry else None),
        "tally": tally.most_common(15), "raw_tally": raw_tally.most_common(10),
        "top_symbols": symbols.most_common(8), "ledger": led, "tp": tp,
        "tp_self_test": test, "gates": gates, "verdict": verdict, "fix": fix,
        "hard_block": hard,
    }


def print_report(a: dict) -> None:
    print("=" * 70)
    print("WHY NO ENTRY / IS TAKE-PROFIT ARMED")
    print("=" * 70)
    print(f"data dir        : {a['data_dir']}")
    print(f"journal rows    : {a['journal_rows']}  (window {a['window_hours']:g}h)")
    print(f"events          : {a['kinds'] or 'none'}")
    age = a["last_entry_age_min"]
    print(f"entries         : {a['entries']}   last entry: "
          f"{'never' if age is None else format(age, '.1f') + ' min ago'}")
    print(f"rejects         : {a['rejects']}")

    print("\n-- reject tally (last %.0fh) --" % a["window_hours"])
    if not a["tally"]:
        print("  (no rejects recorded)")
    for reason, n in a["tally"]:
        print(f"  {n:>5}  {reason}")

    if a["top_symbols"]:
        print("\n-- most rejected symbols --")
        for sym, n in a["top_symbols"]:
            print(f"  {n:>5}  {sym}")

    led = a["ledger"]
    print("\n-- paper book / risk gates --")
    if not led.get("present"):
        print("  no paper_ledger.json found")
    else:
        print(f"  equity {_money(led['equity'])}  cash {_money(led['cash'])}  "
              f"day P&L {_money(led['day_pnl'])}")
        print(f"  daily stop {_money(led['daily_limit_usd'])}  halted={led['daily_halted']}  "
              f"override_today={led['override_today']}  blocking_entries={led['blocking_entries']}")
        print(f"  open positions {led['open_positions']}/{a['gates']['max_open_positions']}")
        print(f"  breaker {led['breaker']['state']} (mult {led['breaker']['mult']}) "
              f"- {led['breaker']['reason']}")
        for _mint, p in list(led["positions"].items())[:8]:
            print(f"    - {p.get('symbol', '?')}: horizon={p.get('horizon')} "
                  f"entry={p.get('entry')} last={p.get('last')} stage={p.get('stage')} "
                  f"stop={p.get('stop_price')}")

    tp = a["tp"]
    print("\n-- take-profit --")
    print(f"  style={tp['style']}  plan={tp['mode']}  armed={tp['active']}")
    for r in tp["rungs"]:
        print(f"    {r['label']}: sell {r['sell']:.0%} at {r['at']:g}x")
    print(f"  scalp stop {tp['scalp']['stop_pct']:.0%}, trail {tp['scalp']['trail_pct']:.0%}, "
          f"timeout {tp['scalp']['timeout_hours']:g}h, max hold {tp['scalp']['max_hold_hours']:g}h")
    t = a["tp_self_test"]
    print(f"  self-test: TP1={t['tp1']!r}  TP2={t['tp2']!r}  stop={t['stop']!r}  ok={t['ok']}")

    print("\n-- effective gates --")
    for k, v in a["gates"].items():
        print(f"  {k:28} {v}")

    print("\n" + "=" * 70)
    print("VERDICT: " + a["verdict"])
    if a["fix"]:
        print("FIX    : " + a["fix"])
    print("=" * 70)


def main() -> int:
    ap = argparse.ArgumentParser(description="Explain why the desk is not entering and whether TP is armed.")
    ap.add_argument("--hours", type=float, default=24.0, help="look-back window for the reject tally")
    ap.add_argument("--tail", type=int, default=6000, help="journal lines to read from the end")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    a = analyse(args.hours, args.tail)
    if args.json:
        print(json.dumps(a, indent=2, default=str))
    else:
        print_report(a)
    return 2 if a["hard_block"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
