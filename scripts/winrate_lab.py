#!/usr/bin/env python3
"""Find the cheapest honest path to a >=50% win rate.

The desk's win rate is not a property of the signal alone. It is the joint
product of *which* entries you take (gates / pullback) and *when* you sell (the
take-profit ladder). A fast ladder mechanically raises the win rate by banking
small moves before they reverse; a good entry filter keeps the losers small
enough that the fast ladder still makes money. This script sweeps both axes at
once and reports only configurations that clear BOTH bars:

    win rate  >= 50%          (what the operator asked for)
    profit factor >= 1.20     (otherwise a 50% win rate is just a slow bleed)
    independent pools >= 10   (trades inside one pool are one observation)
    trades >= 30

It is deliberately stricter than "show me 50%": a config that wins 50% of the
time and loses money is not a recommendation, it is a trap with a nice number.

Method
------
1. Load candle paths from the pool store (``scripts/pool_harvest.py``), or fall
   back to ``data/indicator_paths.json`` / ``backtest_cache.json``.
2. Take every bar where the desk signal says BUY (the repo's baseline).
3. Precompute the forward outcome of each entry under every (TP ladder, stop)
   pair once. Filtering is then free, which keeps a 240-cell sweep fast.
4. Sweep entry filters x exit ladders, aggregate, and rank.

Usage
-----
    python scripts/winrate_lab.py --store data/poolstore.json --out winrate_lab.json
    python scripts/winrate_lab.py --cache backtest_cache.json
    python scripts/winrate_lab.py --demo        # synthetic plumbing check only
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
from desk import exits as exit_rules  # noqa: E402
from desk import indicator_backtest as ib  # noqa: E402
from desk.poolstore import load_paths  # noqa: E402
from desk.signal_backtest import DEFAULT_TP_PLANS, simulate  # noqa: E402
from meme_tactics_eval import _baseline  # noqa: E402

BAR_MINUTES = 5
MIN_HISTORY = 30
STEP = 1

# ---- the two bars the operator cares about, stated before the run ----
TARGET_WIN = 0.50
MIN_PF = 1.20
MIN_POOLS = 10
MIN_TRADES = 30

# Exit ladders to sweep. "profile" = the desk's current per-horizon ladder.
PLANS = {
    "profile (default)": {},
    "quick 1.25x half": {"tp1_at": 1.25, "tp1_frac": 0.5, "tp2_at": 1.6,
                          "tp2_frac": 0.5, "trail_pct": 0.15},
    "single 1.35x": {"tp1_at": 1.35, "tp1_frac": 0.5, "tp2_at": 1e9,
                      "tp2_frac": 0.0, "trail_pct": 0.18},
    "tight trail 1.4x": {"tp1_at": 1.4, "tp1_frac": 0.4, "tp2_at": 2.2,
                          "tp2_frac": 0.5, "trail_pct": 0.12},
    "patient 2x/3x": {"tp1_at": 2.0, "tp1_frac": 0.33, "tp2_at": 3.0,
                       "tp2_frac": 0.5, "trail_pct": 0.25},
}
STOPS = (0.15, 0.20, 0.25, 0.30)
PULLBACKS = (0.0, 0.10, 0.20)
GATE_SETS = ("none", "scan", "scan+flow", "full")


# --------------------------------------------------------------------------- #
# entry filters (all evaluated from data available at the entry bar)
# --------------------------------------------------------------------------- #
def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _chg(candles, i, back):
    if i - back < 0:
        return None
    a, b = candles[i - back]["close"], candles[i]["close"]
    return (b / a - 1.0) if a else None


def _age_min(created_at, ts):
    if not created_at:
        return None
    try:
        c = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return (ts - c) / 60.0


def gate_ok(row, gate_set: str) -> bool:
    if gate_set == "none":
        return True
    S = config.SCAN
    a = row.get("attrs") or {}
    liq = _num(a.get("liq"))
    mcap = _num(a.get("mcap"))
    if liq is not None and liq < S["min_liquidity_usd"]:
        return False
    if mcap is not None and not (S["min_mcap_usd"] <= mcap <= S["max_mcap_usd"]):
        return False
    candles, i = row["candles"], row["idx"]
    chg1h = _chg(candles, i, 12)
    if chg1h is not None and chg1h * 100.0 > S["max_h1_change_pct"]:
        return False
    age = _age_min(a.get("created_at"), candles[i]["ts"])
    if age is not None and not (S["min_age_minutes"] <= age <= S["max_age_hours"] * 60):
        return False
    if gate_set == "scan":
        return True
    buys, sells = _num(a.get("buys_h1")), _num(a.get("sells_h1"))
    if buys is not None and sells is not None:
        if buys + sells < S["min_trades_h1"]:
            return False
        if buys > 0 and sells / buys < S["min_sells_ratio_h1"]:
            return False
    if gate_set == "scan+flow":
        return True
    buyers = _num(a.get("buyers_h1"))
    if buys and buyers and buys / buyers > 4.0:
        return False
    return True


def pullback_ok(row, drop: float) -> bool:
    if drop <= 0:
        return True
    hi, close = row.get("recent_high"), row.get("close")
    return bool(hi and close and close <= hi * (1.0 - drop))


# --------------------------------------------------------------------------- #
# data loading
# --------------------------------------------------------------------------- #
def load_from_store(path: str, min_bars: int = 70) -> list[dict]:
    return load_paths(path, min_bars=min_bars)


def load_from_cache(path: str) -> list[dict]:
    """backtest_cache.json shape: {"pools":[{address,name,candles,...}]}"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for p in d.get("pools") or []:
        cs = p.get("candles") or []
        if len(cs) < 70:
            continue
        out.append({"name": (p.get("name") or p.get("address") or "?")[:12],
                    "address": p.get("address"), "candles": cs, "horizon": "scalp",
                    "liq": p.get("liq"), "mcap": p.get("mcap") or p.get("fdv"),
                    "vol_h1": p.get("vol_h1"), "buys_h1": p.get("buys_h1"),
                    "sells_h1": p.get("sells_h1"), "buyers_h1": p.get("buyers_h1"),
                    "created_at": p.get("created_at")})
    return out


def load_from_indicator_paths(path: str) -> list[dict]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for p in d.get("paths") or []:
        cs = p.get("candles") or []
        if len(cs) < 70:
            continue
        out.append({"name": p.get("name"), "address": p.get("name"),
                    "candles": cs, "horizon": p.get("horizon") or "scalp",
                    "attrs": {}})
    return out


# --------------------------------------------------------------------------- #
# core sweep
# --------------------------------------------------------------------------- #
def build_rows(paths: list[dict]) -> list[dict]:
    rows = _baseline(paths, BAR_MINUTES, MIN_HISTORY, STEP)
    # attach pool attributes and a stable pool id
    by_name = {}
    for p in paths:
        by_name[p.get("name")] = p
    for r in rows:
        # _baseline does not carry the path name, so recover it by identity
        r["pool_id"] = None
        for p in paths:
            if p["candles"] is r["candles"]:
                r["pool_id"] = p.get("address") or p.get("name")
                r["attrs"] = {"liq": p.get("liq"), "mcap": p.get("mcap"),
                              "buys_h1": p.get("buys_h1"),
                              "sells_h1": p.get("sells_h1"),
                              "buyers_h1": p.get("buyers_h1"),
                              "created_at": p.get("created_at")}
                break
    return rows


def precompute(rows: list[dict]) -> dict:
    """For every row and every (plan, stop): the forward return once."""
    table: dict[tuple[str, float], list[float]] = {}
    for plan_name, over in PLANS.items():
        for stop in STOPS:
            rets = []
            for r in rows:
                ov = dict(over)
                ov["stop_pct"] = stop
                prof = exit_rules.profile(r["horizon"], overrides=ov)
                t = simulate(r["candles"], r["idx"], r["horizon"], prof=prof)
                rets.append(t["ret_pct"] if t else 0.0)
            table[(plan_name, stop)] = rets
    return table


def agg(rets: list[float]) -> dict:
    n = len(rets)
    if not n:
        return {"trades": 0, "win_rate": None, "profit_factor": None,
                "avg_ret_pct": None}
    wins = sum(1 for x in rets if x > 0)
    gw = sum(x for x in rets if x > 0)
    gl = -sum(x for x in rets if x <= 0)
    return {"trades": n, "win_rate": round(wins / n, 4),
            "profit_factor": round(gw / gl, 3) if gl else None,
            "avg_ret_pct": round(sum(rets) / n, 3)}


def sweep(rows: list[dict], table: dict) -> list[dict]:
    out = []
    for gate_set in GATE_SETS:
        for drop in PULLBACKS:
            keep = [i for i, r in enumerate(rows)
                    if gate_ok(r, gate_set) and pullback_ok(r, drop)]
            pools = len({rows[i].get("pool_id") for i in keep})
            for (plan_name, stop), rets in table.items():
                sel = [rets[i] for i in keep]
                a = agg(sel)
                a.update({"gate": gate_set, "pullback": drop, "plan": plan_name,
                          "stop": stop, "pools": pools})
                a["clears"] = bool(
                    a["trades"] >= MIN_TRADES and pools >= MIN_POOLS
                    and a["win_rate"] is not None and a["win_rate"] >= TARGET_WIN
                    and a["profit_factor"] is not None and a["profit_factor"] >= MIN_PF)
                out.append(a)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sweep entries x exits for a real 50% win rate.")
    ap.add_argument("--store", default=str(ROOT / "data/poolstore.json"))
    ap.add_argument("--cache", help="backtest_cache.json instead of the store")
    ap.add_argument("--paths", help="indicator_paths.json instead of the store")
    ap.add_argument("--demo", action="store_true", help="synthetic plumbing check")
    ap.add_argument("--out", default=str(ROOT / "winrate_lab.json"))
    args = ap.parse_args(argv)

    if args.demo:
        paths = ib.synth_paths(n=60, bars=240, seed=7)
        source = "SYNTHETIC (plumbing only, not a claim)"
    elif args.cache:
        paths = load_from_cache(args.cache)
        source = f"backtest_cache.json ({len(paths)} pools)"
    elif args.paths:
        paths = load_from_indicator_paths(args.paths)
        source = f"indicator_paths.json ({len(paths)} pools)"
    else:
        paths = load_from_store(args.store)
        source = f"poolstore ({len(paths)} pools)"
    if not paths:
        print(f"no usable candle paths ({source}). Run scripts/pool_harvest.py first.",
              file=sys.stderr)
        return 2

    print("=" * 78)
    print(f"  WIN-RATE LAB  --  {source}")
    print(f"  bar: win >= {TARGET_WIN:.0%}, PF >= {MIN_PF}, pools >= {MIN_POOLS}, "
          f"trades >= {MIN_TRADES}")
    print("=" * 78, flush=True)

    rows = build_rows(paths)
    if not rows:
        print("no baseline BUY entries found.", file=sys.stderr)
        return 2
    print(f"  baseline entries: {len(rows)} across {len(paths)} pools", flush=True)
    table = precompute(rows)
    print(f"  precomputed {len(PLANS) * len(STOPS)} exit variants", flush=True)
    res = sweep(rows, table)

    winners = [r for r in res if r["clears"]]
    winners.sort(key=lambda r: (-(r["win_rate"] or 0), -(r["profit_factor"] or 0)))
    # best PF among configs that still hit the 50% win target
    at_target = [r for r in res if (r["win_rate"] or 0) >= TARGET_WIN
                 and r["trades"] >= MIN_TRADES and r["pools"] >= MIN_POOLS]
    at_target.sort(key=lambda r: -(r["profit_factor"] or 0))
    # best win rate that is still profitable
    profitable = [r for r in res if (r["profit_factor"] or 0) >= MIN_PF
                  and r["trades"] >= MIN_TRADES and r["pools"] >= MIN_POOLS]
    profitable.sort(key=lambda r: -(r["win_rate"] or 0))

    print("\n  CONFIGS THAT CLEAR BOTH BARS (win>=50% AND PF>=1.2)")
    if not winners:
        print("    none. Closest profitable configs below.")
    else:
        print(f"    {'gate':<10}{'pull':>6}{'plan':<20}{'stop':>6}{'n':>6}"
              f"{'pools':>6}{'win':>8}{'PF':>7}{'avg%':>8}")
        for r in winners[:12]:
            print(f"    {r['gate']:<10}{r['pullback']*100:>5.0f}%{r['plan']:<20}"
                  f"{r['stop']*100:>5.0f}%{r['trades']:>6}{r['pools']:>6}"
                  f"{r['win_rate']*100:>7.1f}%{r['profit_factor']:>7.2f}"
                  f"{r['avg_ret_pct']:>8.2f}")

    print("\n  BEST PROFITABLE CONFIGS BY WIN RATE")
    print(f"    {'gate':<10}{'pull':>6}{'plan':<20}{'stop':>6}{'n':>6}"
          f"{'pools':>6}{'win':>8}{'PF':>7}{'avg%':>8}")
    for r in profitable[:10]:
        print(f"    {r['gate']:<10}{r['pullback']*100:>5.0f}%{r['plan']:<20}"
              f"{r['stop']*100:>5.0f}%{r['trades']:>6}{r['pools']:>6}"
              f"{r['win_rate']*100:>7.1f}%{r['profit_factor']:>7.2f}"
              f"{r['avg_ret_pct']:>8.2f}")

    print("\n  BEST PF AMONG CONFIGS THAT HIT 50% WIN")
    print(f"    {'gate':<10}{'pull':>6}{'plan':<20}{'stop':>6}{'n':>6}"
          f"{'pools':>6}{'win':>8}{'PF':>7}{'avg%':>8}")
    for r in at_target[:10]:
        print(f"    {r['gate']:<10}{r['pullback']*100:>5.0f}%{r['plan']:<20}"
              f"{r['stop']*100:>5.0f}%{r['trades']:>6}{r['pools']:>6}"
              f"{r['win_rate']*100:>7.1f}%{r['profit_factor']:>7.2f}"
              f"{r['avg_ret_pct']:>8.2f}")

    payload = {
        "source": source, "pools": len(paths), "entries": len(rows),
        "bar": {"target_win": TARGET_WIN, "min_pf": MIN_PF,
                "min_pools": MIN_POOLS, "min_trades": MIN_TRADES},
        "winners": winners,
        "best_profitable": profitable[:25],
        "best_at_target": at_target[:25],
        "all": res,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
