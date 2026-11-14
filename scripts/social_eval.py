#!/usr/bin/env python3
"""Does social attention actually predict forward returns? Measure, don't assume.

`desk/social.py` can see three things about a Solana token for free:

  boosted   someone paid DexScreener to promote it
  profiled  the project made a token profile with its own X / Telegram links
  mentioned how often the ticker shows up in the Telegram watchlist

The first two are snapshots we can test *today*: take every currently-boosted
(or profiled) Solana mint, pull its real GeckoTerminal candles, and run the same
signal + exit simulation the rest of the repo uses. Then compare against the
random-pool baseline from `indicator_eval_fresh.json` (n=3,629, win 15.4%,
PF 0.503).

The bar is stated before the run, exactly like every other gate in this repo:

    MIN_TRADES   = 30        (per group)
    MIN_POOLS    = 10        (independent pools -- see below)
    MIN_WIN_LIFT = 0.05      (+5pp over the random baseline)
    MIN_PF       = 1.20      (and not worse than the baseline PF)

MIN_POOLS exists because trades inside one pool are not independent: they share
the same coin, the same day and the same move. Three pools that each produced 15
trades is three observations, not forty-five. The first run of this script
returned "ADD" on exactly that mistake (45 trades, 3 pools), which is why the
pool count is now part of the bar.

Anything else is DO-NOT-ADD and `SOCIAL["gate"]` stays False.

Honest caveats, printed with the result rather than hidden:

  * Boosts are "latest", so this is a *current* snapshot, not a historical
    sample. It is a fair test of "does a boost predict the next few hours",
    which is the only question the live desk would ever ask.
  * Survivorship: a token that got boosted and then died may have dropped off
    the feed before we looked. That biases the boost group *upward*, so a
    failure here is a strong failure and a pass is weak evidence.
  * Telegram mention velocity cannot be tested this way -- it needs snapshots
    collected over time. That is what shadow mode is for.

Usage:
    python scripts/social_eval.py --out social_eval.json
    python scripts/social_eval.py --groups boost profile --limit 25
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from desk import indicator_backtest as ib          # noqa: E402
from desk import social                            # noqa: E402

# ---- the bar, stated before the data is read ----
MIN_TRADES = 30
MIN_POOLS = 10
MIN_WIN_LIFT = 0.05
MIN_PF = 1.20

GT = "https://api.geckoterminal.com/api/v2"


def _gt(path: str, params: dict | None = None):
    """Paced GeckoTerminal GET. Reuses indicator_eval's limiter and 429 logic."""
    try:
        from indicator_eval import _get
    except Exception:
        return None
    return _get(f"{GT}{path}", params)


def pools_for_mint(mint: str) -> list[str]:
    """GeckoTerminal pool addresses for a token mint, best-liquidity first."""
    body = _gt(f"/networks/solana/tokens/{mint}/pools", {"page": 1})
    out = []
    for row in (body or {}).get("data", []) or []:
        addr = (row.get("id") or "").replace("solana_", "")
        if addr:
            out.append(addr)
    return out[:2]


def candles_for_pool(pool: str, timeframe: str = "minute", aggregate: int = 5) -> list[dict]:
    body = _gt(f"/networks/solana/pools/{pool}/ohlcv/{timeframe}",
               {"aggregate": aggregate, "limit": 1000, "currency": "usd"})
    lst = (((body or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    candles = [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                "low": float(c[3]), "close": float(c[4]), "vol": float(c[5])}
               for c in lst if c and len(c) >= 6]
    candles.sort(key=lambda c: c["ts"])
    return candles


def group_mints(group: str) -> list[str]:
    if group == "boost":
        return list((social.dex_boosts().get("mints") or {}))
    if group == "profile":
        return list((social.dex_profiles().get("mints") or {}))
    return []


def build_paths(mints: list[str], limit: int, min_bars: int = 70) -> list[dict]:
    paths = []
    for mint in mints[:limit]:
        for pool in pools_for_mint(mint):
            cs = candles_for_pool(pool)
            if len(cs) < min_bars:
                continue
            paths.append({"name": mint[:8], "candles": cs, "horizon": "scalp",
                          "mint": mint})
            break
        print(f"  {len(paths):3d} paths  ({mint[:8]})", flush=True)
    return paths


def measure(paths: list[dict], bar_minutes: int = 5) -> dict:
    """Run the repo's standard signal+exit simulation, per pool and pooled."""
    from meme_tactics_eval import _baseline
    per_pool = []
    all_rows = []
    for p in paths:
        rows = _baseline([p], bar_minutes=bar_minutes, min_history=30, step=1)
        if not rows:
            continue
        a = ib._agg(rows)
        per_pool.append({"name": p.get("name"), "trades": a.get("trades", 0),
                         "win_rate": a.get("win_rate", 0.0),
                         "profit_factor": a.get("profit_factor", 0.0),
                         "avg_ret_pct": a.get("avg_ret_pct", 0.0)})
        all_rows.extend(rows)
    if not all_rows:
        return {"trades": 0, "pools": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "avg_ret_pct": 0.0, "per_pool": []}
    agg = ib._agg(all_rows)
    agg["pools"] = len(per_pool)
    agg["per_pool"] = per_pool
    return agg


def baseline_reference() -> dict:
    """The random-pool baseline this must beat, from the committed artifact."""
    for name in ("indicator_eval_fresh.json", "indicator_eval_120.json",
                 "indicator_eval.json"):
        p = ROOT / name
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        b = d.get("baseline")
        if b and b.get("trades"):
            return {"trades": b["trades"], "win_rate": b.get("win_rate", 0.0),
                    "profit_factor": b.get("profit_factor", 0.0), "source": name}
    return {"trades": 3629, "win_rate": 0.154, "profit_factor": 0.503,
            "source": "documented default"}


def verdict(res: dict, base: dict) -> str:
    if res["trades"] < MIN_TRADES:
        return f"DO-NOT-ADD (only {res['trades']} trades, need {MIN_TRADES})"
    if res.get("pools", 0) < MIN_POOLS:
        return (f"INSUFFICIENT ({res['trades']} trades but only {res.get('pools', 0)} "
                f"independent pools, need {MIN_POOLS})")
    lift = res["win_rate"] - base["win_rate"]
    pf = res["profit_factor"]
    if lift >= MIN_WIN_LIFT and pf >= MIN_PF and pf >= base["profit_factor"]:
        return f"ADD (win {lift:+.1%}, PF {pf:.3f})"
    bits = []
    if lift < MIN_WIN_LIFT:
        bits.append(f"win lift {lift:+.1%} < {MIN_WIN_LIFT:+.0%}")
    if pf < MIN_PF:
        bits.append(f"PF {pf:.3f} < {MIN_PF:.2f}")
    if pf < base["profit_factor"]:
        bits.append(f"PF worse than baseline {base['profit_factor']:.3f}")
    return "DO-NOT-ADD (" + "; ".join(bits) + ")"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure whether social attention predicts returns.")
    ap.add_argument("--groups", nargs="+", default=["boost", "profile"],
                    choices=["boost", "profile"])
    ap.add_argument("--limit", type=int, default=25, help="mints per group")
    ap.add_argument("--out", default=str(ROOT / "social_eval.json"))
    args = ap.parse_args(argv)

    base = baseline_reference()
    print("=" * 70)
    print("SOCIAL ATTENTION vs FORWARD RETURNS")
    print(f"bar: {MIN_TRADES}+ trades, {MIN_POOLS}+ pools, "
          f"win lift >= {MIN_WIN_LIFT:+.0%}, PF >= {MIN_PF}")
    print(f"baseline: n={base['trades']} win {base['win_rate']*100:.1f}% "
          f"PF {base['profit_factor']:.3f}  ({base['source']})")
    print("=" * 70)

    out = {"bar": {"min_trades": MIN_TRADES, "min_pools": MIN_POOLS,
                   "min_win_lift": MIN_WIN_LIFT, "min_pf": MIN_PF},
           "baseline": base, "groups": {}}

    for group in args.groups:
        mints = group_mints(group)
        print(f"\n[{group}] {len(mints)} mints from DexScreener")
        if not mints:
            print("  no mints (offline or feed empty)")
            out["groups"][group] = {"mints": 0, "result": None, "verdict": "NO DATA"}
            continue
        paths = build_paths(mints, args.limit)
        print(f"  {len(paths)} usable candle paths")
        res = measure(paths)
        v = verdict(res, base)
        print(f"  trades={res['trades']}  pools={res.get('pools', 0)}  "
              f"win={res['win_rate']*100:.1f}%  PF={res['profit_factor']:.3f}  "
              f"avg={res.get('avg_ret_pct', 0):+.2f}%")
        for pp in res.get("per_pool", []):
            print(f"    {pp['name']:<10} trades={pp['trades']:>4} "
                  f"win={pp['win_rate']*100:>5.1f}% PF={pp['profit_factor']:>6.3f}")
        print(f"  VERDICT: {v}")
        out["groups"][group] = {"mints": len(mints), "paths": len(paths),
                                "result": res, "verdict": v}

    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
