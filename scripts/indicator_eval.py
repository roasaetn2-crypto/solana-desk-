"""CLI: evaluate technical indicators on real (or synthetic) candle paths.

Test-first gate for the operator's request: try RSI / MACD / ATR / Bollinger /
EMA / Fibonacci / Stochastic / ADX, measure whether each lifts the win rate over
the desk's current signal, and only add the ones that clear the stated bar.

Usage:
    python scripts/indicator_eval.py --pools 80 --out indicator_eval.json
    python scripts/indicator_eval.py --offline --out indicator_eval_demo.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from desk import indicator_backtest as ib  # noqa: E402

GT = "https://api.geckoterminal.com/api/v2"
_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json",
                         "User-Agent": "Mozilla/5.0 desk-indicator-eval"})
_LAST = [0.0]
MIN_GAP_S = 3.0          # GeckoTerminal free tier is ~30 calls/min; stay under it


def _get(url: str, params: dict | None = None, tries: int = 6):
    """Paced GET that honours 429 Retry-After instead of hammering the host."""
    for i in range(tries):
        gap = MIN_GAP_S - (time.time() - _LAST[0])
        if gap > 0:
            time.sleep(gap)
        _LAST[0] = time.time()
        try:
            r = _SESSION.get(url, params=params, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                time.sleep(float(ra) if ra else 5.0 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(2.0)
    return None


def _collect_pools(want: int) -> list[dict]:
    pools: dict[str, dict] = {}
    for endpoint, pages in (("trending_pools", 5), ("new_pools", 3)):
        for page in range(1, pages + 1):
            body = _get(f"{GT}/networks/solana/{endpoint}", {"page": page})
            for row in (body or {}).get("data", []) or []:
                addr = (row.get("id") or "").replace("solana_", "")
                if addr and addr not in pools:
                    a = row.get("attributes") or {}
                    pools[addr] = {"address": addr, "name": a.get("name")}
            if len(pools) >= want:
                return list(pools.values())
    return list(pools.values())


def _fetch_ohlcv(addr: str, timeframe: str, aggregate: int) -> list[dict]:
    body = _get(f"{GT}/networks/solana/pools/{addr}/ohlcv/{timeframe}",
                {"aggregate": aggregate, "limit": 1000, "currency": "usd"})
    lst = (((body or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    candles = [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                "low": float(c[3]), "close": float(c[4]), "vol": float(c[5])}
               for c in lst if c and len(c) >= 6]
    candles.sort(key=lambda c: c["ts"])
    return candles


def real_paths(want: int, timeframe: str, aggregate: int,
               bar_minutes: int) -> list[dict]:
    print(f"Collecting pools (need ~{want})...", flush=True)
    pools = _collect_pools(want * 2)
    print(f"  got {len(pools)} unique pools", flush=True)
    paths = []
    for p in pools:
        if len(paths) >= want:
            break
        candles = _fetch_ohlcv(p["address"], timeframe, aggregate)
        if len(candles) < 70:
            continue
        paths.append({"name": p.get("name") or p["address"][:8],
                      "candles": candles, "horizon": "scalp"})
        print(f"  {len(paths):3d}/{want} {p.get('name')} ({len(candles)} bars)",
              flush=True)
    return paths


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def print_report(summary: dict, source: str) -> None:
    b = summary["baseline"]
    print("=" * 78)
    print(f"  INDICATOR EVALUATION  ({source})")
    print("=" * 78)
    print(f"  baseline (signal BUY only): n={b['trades']}  win {_pct(b['win_rate'])}  "
          f"avg {b['avg_ret_pct']}%  PF {b['profit_factor']}")
    print(f"  bar: n>={ib.MIN_TRADES}, win lift>={100*ib.MIN_WIN_LIFT:.0f}pp, "
          f"PF>={ib.MIN_PF}")
    print("\n  ENTRY FILTERS (added on top of signal BUY)")
    print(f"    {'indicator':34s} {'n':>5} {'win':>7} {'lift':>7} {'PF':>7}  verdict")
    for k, v in summary["entry_filters"].items():
        lift = "n/a" if v["win_lift"] is None else f"{100*v['win_lift']:+.1f}pp"
        print(f"    {k:34s} {v['trades']:>5} {_pct(v['win_rate']):>7} "
              f"{lift:>7} {str(v['profit_factor']):>7}  {v['verdict']}")
    print("\n  EXIT TIMING (extra exit on top of desk exits)")
    print(f"    {'indicator':34s} {'n':>5} {'win':>7} {'lift':>7} {'PF':>7}  verdict")
    for k, v in summary["exit_rules"].items():
        lift = "n/a" if v["win_lift"] is None else f"{100*v['win_lift']:+.1f}pp"
        print(f"    {k:34s} {v['trades']:>5} {_pct(v['win_rate']):>7} "
              f"{lift:>7} {str(v['profit_factor']):>7}  {v['verdict']}")
    if summary.get("tp_sweep"):
        print("\n  TAKE-PROFIT SWEEP (baseline BUYs, alternative TP plans)")
        print(f"    {'plan':26s} {'n':>5} {'win':>7} {'avg':>8} {'PF':>7}")
        for k, v in summary["tp_sweep"].items():
            print(f"    {k:26s} {v['trades']:>5} {_pct(v['win_rate']):>7} "
                  f"{str(v['avg_ret_pct']):>8} {str(v['profit_factor']):>7}")
    print("\n  ADD-WORTHY ENTRY :", summary["add_entry"] or "none")
    print("  ADD-WORTHY EXIT  :", summary["add_exit"] or "none")
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", type=int, default=80)
    ap.add_argument("--timeframe", default="minute")
    ap.add_argument("--aggregate", type=int, default=5)
    ap.add_argument("--bar-minutes", type=int, default=5)
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse the cached real candle paths instead of re-fetching")
    ap.add_argument("--out", default="indicator_eval.json")
    args = ap.parse_args()

    cache = ROOT / "data" / "indicator_paths.json"
    if args.offline:
        paths = ib.synth_paths(n=args.pools, bars=240, seed=7)
        source = "SYNTHETIC (plumbing only - not a performance claim)"
    elif args.reuse and cache.exists():
        paths = json.loads(cache.read_text(encoding="utf-8"))["paths"]
        source = "real GeckoTerminal OHLCV (cached)"
    else:
        paths = real_paths(args.pools, args.timeframe, args.aggregate,
                           args.bar_minutes)
        source = "real GeckoTerminal OHLCV"
        if paths:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"paths": paths}), encoding="utf-8")
            print(f"  cached {len(paths)} paths -> {cache}", flush=True)
    if not paths:
        print("No candle paths (network?). Re-run with --offline.", file=sys.stderr)
        return 2

    report = ib.evaluate(paths, bar_minutes=args.bar_minutes, step=args.step,
                         min_history=30)
    summary = ib.summarize(report)
    summary["source"] = source
    summary["config"] = {"pools": len(paths), "bars": report["bars"],
                         "bar_minutes": args.bar_minutes, "step": args.step,
                         "min_trades": ib.MIN_TRADES,
                         "min_win_lift": ib.MIN_WIN_LIFT, "min_pf": ib.MIN_PF}
    Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print_report(summary, source)
    print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
