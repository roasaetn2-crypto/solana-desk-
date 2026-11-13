#!/usr/bin/env python3
"""Did the AI opinion actually help? Join journal rows and report.

The desk journals two things that can be joined:

    {"kind":"brain_exit","mint":..,"rule":..,"verdict":"hold|sell_half|sell_now",
     "confidence":..,"provider":..}
    {"kind":"exit","mint":..,"pnl_usd":..,"reason":..}

so every closed trade that had an AI read can be bucketed by what the model said.
That is the only honest way to answer "does this raise my win rate": compare the
realised P&L of the trades where the model agreed with the rule against the ones
where it disagreed.

Entry opinions are NOT joinable: `journal("decision", ...)` is written before the
async verdict arrives, so the row says `{"status":"pending"}` and the answer is
never persisted. This script says so instead of pretending.

Usage:
    python scripts/brain_scorecard.py
    python scripts/brain_scorecard.py --json
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desk.ops import data_file

# The rule that fired already decided to sell. "sell_now"/"sell_half" agree with
# it; "hold" is the model arguing against the exit.
AGREE = {"sell_now", "sell_half"}


def rows() -> list[dict]:
    p = data_file("journal.jsonl")
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def build(rs: list[dict]) -> dict:
    reads: dict[str, dict] = {}
    for r in rs:
        if r.get("kind") == "brain_exit" and r.get("mint"):
            reads[r["mint"]] = r           # last read wins

    closed: list[dict] = []
    for r in rs:
        if r.get("kind") != "exit" or "pnl_usd" not in r:
            continue
        m = r.get("mint")
        b = reads.get(m) or {}
        closed.append({
            "mint": m, "symbol": r.get("symbol"), "pnl": float(r["pnl_usd"]),
            "rule": r.get("reason"), "verdict": b.get("verdict"),
            "confidence": b.get("confidence"), "provider": b.get("provider"),
        })

    with_ai = [c for c in closed if c["verdict"]]
    buckets: dict[str, list[float]] = defaultdict(list)
    for c in with_ai:
        buckets[c["verdict"]].append(c["pnl"])

    def stats(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0}
        wins = [x for x in xs if x > 0]
        return {"n": len(xs), "win_rate": round(100.0 * len(wins) / len(xs), 1),
                "avg_pnl": round(sum(xs) / len(xs), 2),
                "total_pnl": round(sum(xs), 2),
                "best": round(max(xs), 2), "worst": round(min(xs), 2)}

    agree = [c["pnl"] for c in with_ai if c["verdict"] in AGREE]
    disagree = [c["pnl"] for c in with_ai if c["verdict"] not in AGREE]
    return {
        "closed": len(closed), "with_ai": len(with_ai),
        "by_verdict": {k: stats(v) for k, v in sorted(buckets.items())},
        "agreed": stats(agree), "disagreed": stats(disagree),
        "all_closed": stats([c["pnl"] for c in closed]),
        "pending_entries": sum(1 for r in rs
                               if r.get("kind") == "decision"
                               and isinstance(r.get("brain"), dict)
                               and r["brain"].get("status") == "pending"),
    }


def main() -> int:
    d = build(rows())
    if "--json" in sys.argv:
        print(json.dumps(d, indent=2))
        return 0

    if not d["closed"]:
        print("No closed trades in the journal yet.")
        print(f"journal: {data_file('journal.jsonl')}")
        return 0

    print(f"closed trades: {d['closed']}   with an AI read: {d['with_ai']}")
    if d["pending_entries"]:
        print(f"entry opinions never persisted: {d['pending_entries']} "
              "(written before the async verdict arrives)")

    a = d["all_closed"]
    print(f"\nall closed        n={a['n']:>4}  win {a['win_rate']:>5}%  "
          f"avg ${a['avg_pnl']:>7}  total ${a['total_pnl']}")

    if not d["with_ai"]:
        print("\nNo AI reads joined yet. Exits journal a brain_exit row only when a")
        print("provider answered, so this fills in as positions close.")
        return 0

    print("\nby what the model said:")
    for v, s in d["by_verdict"].items():
        print(f"  {v:<10} n={s['n']:>4}  win {s['win_rate']:>5}%  "
              f"avg ${s['avg_pnl']:>7}  total ${s['total_pnl']}")

    ag, dg = d["agreed"], d["disagreed"]
    print("\nagreed with the rule vs argued against it:")
    for label, s in (("agreed", ag), ("disagreed", dg)):
        if s["n"]:
            print(f"  {label:<10} n={s['n']:>4}  win {s['win_rate']:>5}%  "
                  f"avg ${s['avg_pnl']:>7}  total ${s['total_pnl']}")
        else:
            print(f"  {label:<10} n=   0")

    if ag["n"] and dg["n"]:
        delta = dg["avg_pnl"] - ag["avg_pnl"]
        print(f"\n  avg P&L when the model disagreed: {delta:+.2f} vs agreement")
        if ag["n"] < 30 or dg["n"] < 30:
            print("  too few trades to conclude anything (need ~30+ per bucket)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
