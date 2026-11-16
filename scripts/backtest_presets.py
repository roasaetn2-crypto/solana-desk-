#!/usr/bin/env python3
"""Backtest the presets against real candles, using the desk's own exit engine.

This does not reimplement the exit rules. It calls ``desk.exits.evaluate()`` --
the exact function the live broker calls -- bar by bar, feeding each bar's low
and then its high so that a stop inside the bar is seen before a take-profit.
That ordering is deliberately pessimistic and is applied identically to every
preset, so the ranking between them is fair.

What is compared
----------------
``legend_scalp`` and ``super_legend_v2`` have **identical entry gates** and
differ in exactly eight exit keys (verified in ``--diff``). So this is a clean
A/B on the exit ladder: same entries, same candles, different exits.

Method
------
1. Load real OHLCV from ``data/g87e/*.json`` (10 pools x 5m/15m/1h).
2. Load the entry list the gate backtest already found
   (``research/backtest_g87e.json``). Entries are deduped by
   (pool, tf, entry_ts, side) so the same setup is never counted twice across
   the baseline/fixed/tuned variants.
3. For each preset: apply its ``scalp.*`` values to ``config.SCALP``, build the
   profile with ``exits.profile("scalp")``, then walk the candles forward.
4. Deduct the real round-trip cost (paper fees + 2x slippage) from every trade.

Honest limitations
------------------
* The candle set is 10 pools. It is a sample, not the whole market.
* Entries come from a gate backtest, so they carry its selection bias. Because
  both presets see the SAME entries, that bias cancels in the comparison.
* Intrabar order is unknown; stop-before-TP is assumed, which understates all
  presets equally.
* Volume-decay exits are skipped (no per-bar volume history for h6/h24).

Usage
-----
    python scripts/backtest_presets.py
    python scripts/backtest_presets.py --diff          # show what differs
    python scripts/backtest_presets.py --tf 5m         # one timeframe only
    python scripts/backtest_presets.py --json
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from config import EXITS, PAPER, SCALP  # noqa: E402
from desk import exits  # noqa: E402
from desk.broker import paper_side_fee  # noqa: E402
from desk.settings import PRESETS  # noqa: E402

PRESETS_TO_TEST = ("legend_scalp", "super_legend_v2", "super_legend_scalp")
TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60}
TF_SECONDS = {k: v * 60 for k, v in TF_MINUTES.items()}


# --------------------------------------------------------------- data loading
def load_candles() -> dict:
    out = {}
    for f in glob.glob(str(ROOT / "data" / "g87e" / "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        label, tf = d.get("label"), d.get("tf")
        if label and tf and d.get("candles"):
            out[(label, tf)] = d["candles"]
    return out


def load_entries(candles: dict, only_tf: str | None = None) -> list:
    """Unique real entries. Deduped across the baseline/fixed/tuned variants."""
    path = ROOT / "research" / "backtest_g87e.json"
    bt = json.load(open(path, encoding="utf-8"))
    seen, entries = set(), []
    for key, variants in (bt.get("trades") or {}).items():
        label, tf = key.rsplit(":", 1)
        if only_tf and tf != only_tf:
            continue
        cs = candles.get((label, tf))
        if not cs:
            continue
        for trades in variants.values():
            for t in trades or []:
                sig = (label, tf, t.get("entry_ts"), t.get("side"))
                if sig in seen:
                    continue
                seen.add(sig)
                entries.append((label, tf, t, cs))
    return entries


# ------------------------------------------------------------------- presets
def profile_for(name: str) -> dict:
    """Apply a preset's scalp.* values to SCALP and read the real profile."""
    v = PRESETS[name]["values"]
    for k, val in v.items():
        if k.startswith("scalp."):
            SCALP[k.split(".", 1)[1]] = val
    return exits.profile("scalp")


def round_trip_cost_pct(ticket: float = 5.0) -> float:
    """Paper fees on both sides plus the slippage the paper fill already takes."""
    buy = paper_side_fee(ticket)["total_usd"]
    sell = paper_side_fee(ticket)["total_usd"]
    slip = float(PAPER.get("slippage_pct", 0.02)) * 2
    return (buy + sell) / ticket + slip


# -------------------------------------------------------------------- replay
def replay(entry: dict, candles: list, pr: dict, tf: str) -> dict | None:
    """Walk real bars through the desk's own exits.evaluate().

    Two passes per bar, because a bar hides its own order of events:

      probe pass  the bar's low then its high, with the clock disabled. This
                  catches a stop or a trail at the adverse extreme and a
                  take-profit at the favourable one, in that (pessimistic)
                  order, using the real engine.
      clock pass  the bar's close, with the clock enabled. A timeout or a max
                  hold is a decision made on the price the desk actually sees,
                  so it must fill at the close -- not at the bar's low, which
                  would book every clock exit as a maximum loss.
    """
    i0 = entry.get("entry_i")
    entry_px = entry.get("entry")
    if i0 is None or not entry_px:
        return None

    pos = {
        "entry": float(entry_px), "opened": float(entry.get("entry_ts") or 0.0),
        "last": float(entry_px), "peak": float(entry_px), "stage": 0,
        "miss": 0, "stop_price": float(entry_px) * (1 - pr["stop_pct"]),
        "horizon": "scalp", "qty": 1.0,
    }
    remaining, realised = 1.0, 0.0
    bar_s = TF_SECONDS.get(tf, 300)
    max_bars = int(math.ceil(pr.get("max_hold_minutes", 60) / TF_MINUTES.get(tf, 5))) + 1
    max_bars = max(1, min(max_bars, 400))

    probe = {**pr, "timeout_minutes": 1e9, "max_hold_minutes": 1e9}

    def take(frac: float, reason: str, fill: float) -> bool:
        nonlocal remaining, realised
        sold = remaining * max(0.0, min(1.0, frac))
        realised += sold * (fill / pos["entry"] - 1.0)
        remaining -= sold
        return remaining <= 1e-9

    for j in range(i0 + 1, min(len(candles), i0 + 1 + max_bars)):
        bar = candles[j]
        now = float(bar["ts"]) + bar_s
        lo, hi, close = float(bar["low"]), float(bar["high"]), float(bar["close"])

        # --- probe pass: extremes, clock off -------------------------------
        for price in (lo, hi):
            if price <= 0:
                continue
            res = exits.evaluate(pos, {"price_usd": price}, now, probe)
            if not res:
                continue
            frac, reason = res
            if reason.startswith("stop") or "breakeven" in reason:
                fill = pos.get("fill_at") or price
            elif reason.startswith("TP1"):
                fill = min(price, pos["entry"] * pr["tp1_at"])
            elif reason.startswith("TP2"):
                fill = min(price, pos["entry"] * pr["tp2_at"])
            elif reason.startswith("trail"):
                fill = pos["peak"] * (1 - exits.runner_trail(pr))
            else:
                fill = price
            if take(frac, reason, fill):
                return {"ret": realised, "reason": reason, "bars": j - i0}

        # --- clock pass: the close, clock on ------------------------------
        if close > 0:
            res = exits.evaluate(pos, {"price_usd": close}, now, pr)
            if res:
                frac, reason = res
                if take(frac, reason, close):
                    return {"ret": realised, "reason": reason, "bars": j - i0}
        if remaining <= 1e-9:
            break

    if remaining > 1e-9:
        close = float(candles[min(len(candles) - 1, i0 + max_bars)]["close"])
        realised += remaining * (close / pos["entry"] - 1.0)
        return {"ret": realised, "reason": "max hold", "bars": max_bars}
    return {"ret": realised, "reason": "flat", "bars": 0}


def stats(rets: list, cost: float) -> dict:
    n = len(rets)
    if not n:
        return {"trades": 0}
    net = [r - cost for r in rets]
    wins = [r for r in net if r > 0]
    losses = [r for r in net if r <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    avg_w = gw / len(wins) if wins else 0.0
    avg_l = gl / len(losses) if losses else 0.0
    return {
        "trades": n,
        "win_rate": len(wins) / n,
        "avg_win_pct": avg_w * 100,
        "avg_loss_pct": avg_l * 100,
        "rr": (avg_w / avg_l) if avg_l else None,
        "expectancy_pct": sum(net) / n * 100,
        "profit_factor": (gw / gl) if gl else None,
        "breakeven_win": (1 / (1 + avg_w / avg_l) * 100) if avg_l and avg_w else None,
        "gross_expectancy_pct": sum(rets) / n * 100,
    }


def cost_table() -> None:
    """Where the round-trip cost actually comes from, and why ticket size matters.

    The Solana fee is FIXED DOLLARS per swap (~$0.031 at SOL $150: a 5,000
    lamport signature plus a 200,000 lamport priority bid). As a percentage it
    shrinks as the ticket grows. Slippage is the opposite: a percentage that
    does not care how big the ticket is. That is why a $5 book and a $20 book
    with identical settings do not have identical economics.
    """
    print("COST BREAKDOWN -- round trip, both sides\n")
    slip = float(PAPER.get("slippage_pct", 0.02))
    hdr = f"{'ticket':>8}{'fees$':>9}{'fees%':>8}{'slip%':>8}{'total%':>9}"
    print(hdr)
    print("-" * len(hdr))
    for ticket in (1, 2, 5, 10, 20, 50):
        fees_usd = paper_side_fee(ticket)["total_usd"] * 2
        fees_pct = fees_usd / ticket * 100
        slip_pct = slip * 2 * 100
        print(f"{ticket:>7.0f}${fees_usd:>9.3f}{fees_pct:>7.2f}%"
              f"{slip_pct:>7.1f}%{fees_pct + slip_pct:>8.2f}%")
    print(f"\nslippage assumption: {slip * 100:.1f}% per side "
          f"(config PAPER['slippage_pct'])")
    print("the fee is fixed dollars, so a bigger ticket dilutes it;")
    print("slippage is a percentage and never dilutes. At $1 the fee alone")
    print("is most of the round trip -- that is the sizing floor talking.\n")

    print("WHAT A GIVEN GROSS EDGE BECOMES NET\n")
    hdr2 = f"{'gross/trade':>12}" + "".join(f"{f'${t}':>9}" for t in (1, 5, 20))
    print(hdr2)
    print("-" * len(hdr2))
    for gross in (0.0, 0.5, 1.0, 2.0, 5.0, 10.0):
        row = f"{gross:>11.1f}%"
        for ticket in (1, 5, 20):
            c = round_trip_cost_pct(ticket) * 100
            row += f"{gross - c:>8.2f}%"
        print(row)
    print("\nA scalper needs its gross edge to exceed the cost line before it")
    print("makes a cent. On this candle set the measured gross edge is ~0%.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default=None, help="only one timeframe: 5m, 15m or 1h")
    ap.add_argument("--diff", action="store_true", help="show which keys differ")
    ap.add_argument("--cost", action="store_true", help="show the cost breakdown")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ticket", type=float, default=5.0)
    args = ap.parse_args(argv)

    if args.cost:
        cost_table()
        return 0

    if args.diff:
        a = PRESETS["legend_scalp"]["values"]
        b = PRESETS["super_legend_v2"]["values"]
        keys = sorted(set(a) | set(b))
        diff = [(k, a.get(k), b.get(k)) for k in keys if a.get(k) != b.get(k)]
        print(f"legend_scalp vs super_legend_v2: {len(diff)} differing key(s)")
        for k, x, y in diff:
            print(f"  {k:<30} legend={x!r:<10} v2={y!r}")
        print("\nentry gates identical:", not any(
            k.startswith(("scan.", "meme.", "analyst.", "signal.", "entry."))
            for k, _, _ in diff))
        return 0

    candles = load_candles()
    entries = load_entries(candles, args.tf)
    if not entries:
        print("no entries found -- check data/g87e and research/backtest_g87e.json")
        return 1
    cost = round_trip_cost_pct(args.ticket)

    # The plausibility guard rejects >25x intrabar jumps, which are normal in a
    # memecoin bar. Relax it for the replay only; it is a live-data guard, not
    # part of the strategy.
    saved = {k: EXITS.get(k) for k in ("quote_max_jump_x", "quote_max_drop_x",
                                       "missing_quote_closes")}
    EXITS["quote_max_jump_x"] = 1e9
    EXITS["quote_max_drop_x"] = 1e9
    EXITS["missing_quote_closes"] = 10 ** 9

    snap = dict(SCALP)
    results = {}
    try:
        for name in PRESETS_TO_TEST:
            pr = profile_for(name)
            rets, reasons = [], {}
            for label, tf, t, cs in entries:
                r = replay(t, cs, pr, tf)
                if r is None:
                    continue
                rets.append(r["ret"])
                reasons[r["reason"].split(" at")[0].split(" -")[0]] = \
                    reasons.get(r["reason"].split(" at")[0].split(" -")[0], 0) + 1
            results[name] = {**stats(rets, cost), "exits": reasons,
                             "stop_pct": pr["stop_pct"],
                             "tp1_at": pr["tp1_at"], "tp2_at": pr["tp2_at"],
                             "max_hold_minutes": pr.get("max_hold_minutes")}
    finally:
        SCALP.clear()
        SCALP.update(snap)
        for k, v in saved.items():
            if v is None:
                EXITS.pop(k, None)
            else:
                EXITS[k] = v

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"replayed {len(entries)} real entries"
          f"{' on ' + args.tf if args.tf else ' (5m/15m/1h)'}")
    print(f"round-trip cost deducted: {cost * 100:.2f}% per trade "
          f"(paper fees + 2x slippage)\n")
    hdr = (f"{'preset':<20}{'stop':>6}{'TP1':>6}{'TP2':>6}{'hold':>6}"
           f"{'n':>6}{'win%':>7}{'avgW':>7}{'avgL':>7}{'R:R':>6}"
           f"{'gross%':>8}{'exp%':>8}{'PF':>6}{'need%':>7}")
    print(hdr)
    print("-" * len(hdr))
    for name, r in results.items():
        if not r.get("trades"):
            continue
        print(f"{name:<20}{r['stop_pct'] * 100:>5.0f}%{r['tp1_at']:>6.2f}"
              f"{r['tp2_at']:>6.2f}{str(r['max_hold_minutes']):>5}m"
              f"{r['trades']:>6}{r['win_rate'] * 100:>6.1f}%"
              f"{r['avg_win_pct']:>7.2f}{r['avg_loss_pct']:>7.2f}"
              f"{(r['rr'] or 0):>6.2f}{r['gross_expectancy_pct']:>8.2f}"
              f"{r['expectancy_pct']:>8.2f}"
              f"{(r['profit_factor'] or 0):>6.2f}{(r['breakeven_win'] or 0):>6.1f}%")
    print("\nexit mix:")
    for name, r in results.items():
        if r.get("trades"):
            print(f"  {name:<20}{r['exits']}")
    print("\ngross% is before cost, exp% is after. need% is the win rate the "
          "payoff ratio demands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
