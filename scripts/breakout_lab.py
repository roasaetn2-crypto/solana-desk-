#!/usr/bin/env python3
"""Breakout vs dip vs no-filter entries, on the same candles and the same exits.

Question this answers: for a scalp, is it better to buy a DIP (price well below its
recent high) or a BREAKOUT (price clears its recent high on a volume surge)?

Fairness rules (so the comparison cannot flatter either side):
  * every entry rule is judged on the SAME bars, with the SAME exit plan and stop;
  * a "no filter" row (any bar) is printed, because an entry rule only matters if it
    beats entering at random;
  * after a trade opens, the next COOLDOWN bars are skipped, so one move is not counted
    as five trades;
  * results are grouped by pool, and the verdict needs >= MIN_POOLS pools, because
    trades inside one pool are one observation, not many.

Data (first that exists):
    --store data/poolstore.json     the big harvest from scripts/pool_harvest.py
    --cache backtest_cache.json
    data/g87e/*_5m.json             the 10 pools committed in the repo (thin; plumbing check)

Usage:
    python scripts/breakout_lab.py
    python scripts/breakout_lab.py --store data/poolstore.json --out research/breakout_lab.json
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
from desk import exits as exit_rules  # noqa: E402
from desk.signal_backtest import simulate  # noqa: E402

LOOKBACK = 20          # bars (5m) -> 100 minutes of history
MIN_HISTORY = 30
COOLDOWN = 6           # bars skipped after an entry (30 min)
MIN_POOLS = 10
MIN_TRADES = 30

# Same two ladders the repo's winrate_lab compares, both on a 30% stop.
PLANS = {
    "fast (1.25x/1.6x)": {"tp1_at": 1.25, "tp1_frac": 0.5, "tp2_at": 1.6, "tp2_frac": 0.5,
                          "trail_pct": 0.15, "stop_pct": 0.30},
    "patient (2x/3x)": {"tp1_at": 2.0, "tp1_frac": 0.33, "tp2_at": 3.0, "tp2_frac": 0.5,
                        "trail_pct": 0.20, "stop_pct": 0.30},
}


def _entries(candles: list[dict]) -> dict[str, list[int]]:
    """Bar indexes where each entry rule fires, using only data up to that bar's close."""
    out: dict[str, list[int]] = {k: [] for k in RULES}
    last = {k: -10**9 for k in RULES}
    for i in range(max(MIN_HISTORY, LOOKBACK + 1), len(candles) - 1):
        c = candles[i]
        prior = candles[i - LOOKBACK:i]
        hi = max(p["high"] for p in prior)
        hi_close = max(p["close"] for p in prior)
        avg_vol = sum(p["vol"] for p in prior) / LOOKBACK
        if avg_vol <= 0 or not c["close"]:
            continue
        green = c["close"] > c["open"]
        vmult = c["vol"] / avg_vol
        fire = {
            "any bar (no filter)": True,
            "dip 10%": c["close"] <= hi_close * 0.90,
            "dip 20%": c["close"] <= hi_close * 0.80,
            "breakout 20-bar high, vol>=1.5x": c["close"] > hi and green and vmult >= 1.5,
            "breakout 20-bar high, vol>=2x": c["close"] > hi and green and vmult >= 2.0,
            "breakout 20-bar high, vol>=3x": c["close"] > hi and green and vmult >= 3.0,
        }
        for k, ok in fire.items():
            if ok and i - last[k] > COOLDOWN:
                out[k].append(i)
                last[k] = i
    return out


RULES = ["any bar (no filter)", "dip 10%", "dip 20%",
         "breakout 20-bar high, vol>=1.5x", "breakout 20-bar high, vol>=2x",
         "breakout 20-bar high, vol>=3x"]


def load(args) -> list[dict]:
    if args.store and Path(args.store).exists():
        from desk.poolstore import load_paths
        return [{"name": p.get("address") or p.get("name"), "candles": p["candles"]}
                for p in load_paths(args.store, min_bars=70)]
    if args.cache and Path(args.cache).exists():
        d = json.loads(Path(args.cache).read_text(encoding="utf-8"))
        return [{"name": p.get("address") or p.get("name"), "candles": p["candles"]}
                for p in d.get("pools") or [] if len(p.get("candles") or []) >= 70]
    out = []
    for f in sorted(glob.glob(str(ROOT / "data" / "g87e" / "*_5m.json"))):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        if len(d.get("candles") or []) >= 70:
            out.append({"name": d.get("label") or d.get("pool"), "candles": d["candles"]})
    return out


def agg(rets: list[float], pools: int) -> dict:
    n = len(rets)
    if not n:
        return {"trades": 0, "pools": pools, "win_rate": None, "profit_factor": None, "avg_ret_pct": None}
    gw = sum(x for x in rets if x > 0)
    gl = -sum(x for x in rets if x <= 0)
    return {"trades": n, "pools": pools, "win_rate": round(sum(1 for x in rets if x > 0) / n, 3),
            "profit_factor": round(gw / gl, 2) if gl else None,
            "avg_ret_pct": round(sum(rets) / n, 2)}


def run(paths: list[dict]) -> dict:
    saved = config.MEME.get("pullback_entry")
    config.MEME["pullback_entry"] = False      # the lab must own the ladder it tests
    results: dict = {}
    try:
        fired = [(p, _entries(p["candles"])) for p in paths]
        for plan, over in PLANS.items():
            prof = exit_rules.profile("scalp", overrides=dict(over))
            for rule in RULES:
                rets, pools = [], set()
                for p, ent in fired:
                    for i in ent[rule]:
                        t = simulate(p["candles"], i, "scalp", prof=prof)
                        if t:
                            rets.append(t["ret_pct"])
                            pools.add(p["name"])
                results[(plan, rule)] = agg(rets, len(pools))
    finally:
        config.MEME["pullback_entry"] = saved
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="data/poolstore.json")
    ap.add_argument("--cache", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    paths = load(a)
    if not paths:
        print("no candle data found")
        return 1
    res = run(paths)
    print(f"{len(paths)} pools, {sum(len(p['candles']) for p in paths)} candles\n")
    for plan in PLANS:
        print(f"== exit plan: {plan}, stop 30% ==")
        print(f"{'entry rule':36} {'trades':>6} {'pools':>5} {'win%':>6} {'PF':>6} {'avg%':>7}")
        for rule in RULES:
            r = res[(plan, rule)]
            wr = f"{r['win_rate']*100:.1f}" if r["win_rate"] is not None else "-"
            thin_row = r["pools"] < MIN_POOLS or r["trades"] < MIN_TRADES
            print(f"{rule:36} {r['trades']:>6} {r['pools']:>5} {wr:>6} {str(r['profit_factor']):>6} "
                  f"{str(r['avg_ret_pct']):>7}{'  *thin' if thin_row else ''}")
        print()
    print("*thin = fewer than %d pools or %d trades behind that row: a hint, not evidence. "
          "Only unmarked rows can support a verdict." % (MIN_POOLS, MIN_TRADES))
    if a.out:
        Path(a.out).write_text(json.dumps({"pools": len(paths),
            "rows": [{"plan": k[0], "rule": k[1], **v} for k, v in res.items()]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
