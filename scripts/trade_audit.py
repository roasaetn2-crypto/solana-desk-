#!/usr/bin/env python3
"""Audit your own closed trades: true R:R, expectancy, and what the losers share.

Takes the output of `python3 scripts/groq_postmortem.py --cases` (a JSON array of
joined trades) and answers, without calling any model:

  1. What is my real average win, average loss, R:R and expectancy?
  2. Which exit reason is bleeding the account?
  3. Which entry context separates winners from losers? (liquidity, age, score,
     regime, confidence, mcap, volume)

This is the analysis the post-mortem asks an LLM for, computed arithmetically
instead. Where the numbers are strong enough to speak for themselves, they do.

Usage:
    python3 scripts/groq_postmortem.py --cases > cases.json
    python3 scripts/trade_audit.py cases.json

    # or straight from the journal
    python3 scripts/trade_audit.py --journal data/journal.jsonl

    python3 scripts/trade_audit.py cases.json --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# context fields worth splitting on, with the bucket edges that matter for a
# memecoin desk. Edges are chosen from the config floors, not from the data.
SPLITS = {
    "liquidity_usd": [8000, 20000, 50000],
    "mcap_usd": [100000, 500000, 2000000],
    "volume_h1": [5000, 20000, 100000],
    "age_minutes": [10, 60, 240, 1440],
    "score": [0.4, 0.55, 0.7],
    "confidence": [0.4, 0.6, 0.8],
    "win_probability": [0.4, 0.5, 0.6],
    "regime": None,
    "shape": None,
    "thesis": None,
    "direction": None,
    "exit_reason": None,
}


def load_cases(path: str | None, journal: str | None) -> list:
    if journal:
        from desk import groq_postmortem as pm
        return pm.build_cases(journal)
    if not path:
        return json.load(sys.stdin)
    p = Path(path)
    txt = p.read_text(encoding="utf-8")
    # tolerate the stderr summary line the CLI appends
    i = txt.find("[")
    j = txt.rfind("]")
    if i >= 0 and j > i:
        txt = txt[i:j + 1]
    return json.loads(txt)


def _bucket(v, edges):
    """Label a numeric value against ascending edges."""
    if v is None:
        return "missing"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "missing"
    lo = None
    for e in edges:
        if v < e:
            return f"<{e:g}" if lo is None else f"{lo:g}-{e:g}"
        lo = e
    return f">={edges[-1]:g}"


def _stat(rows: list) -> dict:
    rets = [r.get("pnl_usd") for r in rows if isinstance(r.get("pnl_usd"), (int, float))]
    n = len(rets)
    if not n:
        return {"n": 0}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gw = sum(wins)
    gl = abs(sum(losses))
    aw = gw / len(wins) if wins else 0.0
    al = gl / len(losses) if losses else 0.0
    return {
        "n": n,
        "win_rate": round(len(wins) / n, 4),
        "avg_win": round(aw, 2),
        "avg_loss": round(al, 2),
        "rr": round(aw / al, 3) if al else None,
        "expectancy": round(sum(rets) / n, 3),
        "profit_factor": round(gw / gl, 3) if gl else None,
        "net": round(sum(rets), 2),
        "breakeven_win": round(1 / (1 + aw / al) * 100, 1) if al and aw else None,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="cases JSON from --cases")
    ap.add_argument("--journal", default=None, help="read the journal directly")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-n", type=int, default=5, help="min trades to show a bucket")
    args = ap.parse_args(argv)

    cases = load_cases(args.path, args.journal)
    if not isinstance(cases, list):
        print("expected a JSON array of trades", file=sys.stderr)
        return 2
    cases = [c for c in cases if isinstance(c, dict) and isinstance(c.get("pnl_usd"), (int, float))]
    if not cases:
        print("no closed trades with pnl_usd found", file=sys.stderr)
        return 1

    overall = _stat(cases)
    out = {"overall": overall, "splits": {}}

    # ---- per-field split ----
    for field, edges in SPLITS.items():
        buckets: dict = {}
        for c in cases:
            v = c.get(field)
            key = _bucket(v, edges) if edges else (str(v) if v not in (None, "") else "missing")
            buckets.setdefault(key, []).append(c)
        rows = {}
        for key, grp in buckets.items():
            s = _stat(grp)
            if s.get("n", 0) >= args.min_n:
                rows[key] = s
        if len(rows) >= 2:
            # lift = bucket win rate minus overall, weighted by sample size
            for k, s in rows.items():
                s["win_lift"] = round((s["win_rate"] - overall["win_rate"]) * 100, 1)
                s["exp_lift"] = round(s["expectancy"] - overall["expectancy"], 3)
            out["splits"][field] = rows

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    # ---- report ----
    o = overall
    print(f"CLOSED TRADES: {o['n']}\n")
    print(f"  win rate        {o['win_rate']*100:>7.1f}%")
    print(f"  avg win         ${o['avg_win']:>7.2f}")
    print(f"  avg loss        ${o['avg_loss']:>7.2f}")
    print(f"  R:R             {(o['rr'] or 0):>7.2f} : 1")
    print(f"  expectancy      ${o['expectancy']:>7.3f} / trade")
    print(f"  profit factor   {(o['profit_factor'] or 0):>7.2f}")
    print(f"  net             ${o['net']:>7.2f}")
    print(f"  break-even win  {(o['breakeven_win'] or 0):>7.1f}%   "
          f"(you are {o['win_rate']*100:.1f}%)")

    verdict = ("PROFITABLE" if (o["expectancy"] or 0) > 0 else "LOSING")
    print(f"\n  -> {verdict} at {o['win_rate']*100:.1f}% win, "
          f"{(o['rr'] or 0):.2f}:1 R:R\n")

    # ---- the strongest separators ----
    print("ENTRY CONTEXT — win rate by bucket (lift vs overall)\n")
    ranked = []
    for field, rows in out["splits"].items():
        spread = max(r["win_rate"] for r in rows.values()) - min(r["win_rate"] for r in rows.values())
        ranked.append((spread, field, rows))
    ranked.sort(reverse=True)

    for spread, field, rows in ranked[:8]:
        print(f"  {field}   (spread {spread*100:.0f} pts)")
        for key, s in sorted(rows.items(), key=lambda kv: -kv[1]["win_rate"]):
            flag = "  <-- best" if s["win_rate"] == max(r["win_rate"] for r in rows.values()) else ""
            print(f"    {key:<14} n={s['n']:<4} win {s['win_rate']*100:>5.1f}%  "
                  f"lift {s['win_lift']:>+6.1f}  exp ${s['expectancy']:>+7.3f}{flag}")
        print()

    print("A spread under ~15 points on a bucket with n<20 is noise. Check n before")
    print("believing a row. The honest test is one config change, then winrate_lab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
