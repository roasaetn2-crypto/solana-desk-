"""CLI: backtest the BUY/SELL signal engine on real (or synthetic) candle paths.

Answers two questions the operator asked:
  1. Does the entry signal's bullish/bearish read predict the forward move and a
     higher win rate?  -> direction accuracy + win rate by confidence bucket.
  2. Which take-profit plan turns that edge into the most P&L?  -> TP sweep.

Data:
  * default: real Solana pool OHLCV from the public GeckoTerminal API (same source
    as scripts/backtest_100.py).
  * --offline: a labelled synthetic set (clearly marked) so the script runs with no
    network. Synthetic numbers are for plumbing only, never a performance claim.

Outputs a JSON report and a dependency-free SVG chart.

Usage:
    python scripts/backtest_signals.py --trades 120 --out backtest_signals.json
    python scripts/backtest_signals.py --offline --out backtest_signals_demo.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from desk import signal_backtest  # noqa: E402


# --------------------------------------------------------------------------- #
# Synthetic (offline) paths — clearly labelled, plumbing only
# --------------------------------------------------------------------------- #
def synthetic_paths(n: int, bars: int = 160, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    paths = []
    for k in range(n):
        trend = rng.choice([0.006, 0.003, 0.0, -0.003, -0.006])
        vol = rng.uniform(0.012, 0.03)
        price = rng.uniform(0.0002, 0.02)
        ts = 1_700_000_000 + k * 10_000
        candles = []
        for i in range(bars):
            drift = trend + rng.gauss(0, vol)
            o = price
            price = max(1e-9, price * (1 + drift))
            hi = max(o, price) * (1 + abs(rng.gauss(0, vol / 2)))
            lo = min(o, price) * (1 - abs(rng.gauss(0, vol / 2)))
            candles.append({"ts": ts + i * 300, "open": o, "high": hi, "low": lo,
                            "close": price, "vol": abs(rng.gauss(5000, 2500))})
        paths.append({"candles": candles, "horizon": "scalp", "name": f"SYN-{k:03d}"})
    return paths


# --------------------------------------------------------------------------- #
# Real paths via GeckoTerminal (reuse backtest_100 helpers)
# --------------------------------------------------------------------------- #
def real_paths(want: int, timeframe: str, aggregate: int) -> list[dict]:
    sys.path.insert(0, str(ROOT / "scripts"))
    import backtest_100 as bt  # noqa: E402

    print(f"Collecting pools (need ~{want})...", flush=True)
    pools = bt.collect_pools(want * 2)
    print(f"  got {len(pools)} unique pools", flush=True)
    paths = []
    for p in pools:
        if len(paths) >= want:
            break
        candles = bt.fetch_ohlcv(p["address"], timeframe, aggregate)
        time.sleep(0.35)
        if len(candles) < 40:
            continue
        horizon = "scalp"
        try:
            if p.get("created_at"):
                created = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")).timestamp()
                age_h = (candles[0]["ts"] - created) / 3600
                horizon = "swing" if age_h >= config.EXITS["scalp_max_age_hours"] else "scalp"
        except Exception:
            horizon = "scalp"
        paths.append({"candles": candles, "horizon": horizon,
                      "name": p.get("name") or p["address"][:8]})
        if len(paths) % 10 == 0:
            print(f"  loaded {len(paths)} paths", flush=True)
    return paths


# --------------------------------------------------------------------------- #
# Dependency-free SVG chart
# --------------------------------------------------------------------------- #
def _bar_chart_svg(title: str, rows: list[tuple[str, float, str]]) -> str:
    """rows: (label, value 0..1, caption). Horizontal bars."""
    w, bar_h, gap, left = 720, 26, 12, 210
    h = 60 + len(rows) * (bar_h + gap)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
             f'font-family="system-ui,Arial" font-size="13">',
             f'<rect width="{w}" height="{h}" fill="#0f172a"/>',
             f'<text x="16" y="30" fill="#e2e8f0" font-size="16" font-weight="600">{title}</text>']
    max_w = w - left - 90
    for i, (label, val, cap) in enumerate(rows):
        y = 56 + i * (bar_h + gap)
        bw = max(2, int(max_w * max(0.0, min(1.0, val))))
        color = "#22c55e" if val >= 0.5 else "#f59e0b" if val >= 0.35 else "#ef4444"
        parts.append(f'<text x="16" y="{y + 18}" fill="#cbd5e1">{label}</text>')
        parts.append(f'<rect x="{left}" y="{y}" width="{max_w}" height="{bar_h}" fill="#1e293b" rx="4"/>')
        parts.append(f'<rect x="{left}" y="{y}" width="{bw}" height="{bar_h}" fill="{color}" rx="4"/>')
        parts.append(f'<text x="{left + max_w + 10}" y="{y + 18}" fill="#e2e8f0">{cap}</text>')
    parts.append("</svg>")
    return "".join(parts)


def build_chart(report: dict, out_svg: str) -> None:
    rows = []
    gate = report.get("gate", {})
    all_e = gate.get("all_entries", {})
    buy_e = gate.get("signal_buys_only", {})
    if all_e.get("win_rate") is not None:
        rows.append(("All entries", all_e["win_rate"], f"{all_e['win_rate']*100:.1f}%"))
    if buy_e.get("win_rate") is not None:
        rows.append(("Signal BUYs only", buy_e["win_rate"], f"{buy_e['win_rate']*100:.1f}%"))
    for k, v in (report.get("by_confidence") or {}).items():
        if v.get("win_rate") is not None:
            rows.append((f"Confidence {k}%", v["win_rate"], f"{v['win_rate']*100:.1f}% (n={v['trades']})"))
    if not rows:
        return
    svg = _bar_chart_svg("Signal backtest — win rate", rows)
    Path(out_svg).write_text(svg, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _fmt(v, pct=False, nd=2):
    if v is None:
        return "n/a"
    return f"{v*100:.1f}%" if pct else f"{v:+.{nd}f}"


def print_report(report: dict, source: str) -> None:
    o = report.get("overall", {})
    g = report.get("gate", {})
    a, b = g.get("all_entries", {}), g.get("signal_buys_only", {})
    print("\n" + "=" * 68)
    print(f"  SIGNAL BACKTEST  ({source})")
    print("=" * 68)
    print(f"  paths / entries : {report.get('paths')} / {report.get('entries')}")
    print(f"  direction acc.  : {_fmt(o.get('direction_accuracy'), pct=True)} "
          f"(n={o.get('direction_n')})")
    print(f"  all entries     : win {_fmt(a.get('win_rate'), pct=True)} · "
          f"avg {_fmt(a.get('avg_ret_pct'))}% · PF {a.get('profit_factor')}")
    print(f"  signal BUYs only: win {_fmt(b.get('win_rate'), pct=True)} · "
          f"avg {_fmt(b.get('avg_ret_pct'))}% · PF {b.get('profit_factor')} "
          f"· kept {_fmt(g.get('kept_pct'), pct=True)}")
    print("\n  win rate by confidence bucket:")
    for k, v in (report.get("by_confidence") or {}).items():
        print(f"    {k:>6}% : win {_fmt(v.get('win_rate'), pct=True):>6} · "
              f"dir {_fmt(v.get('direction_accuracy'), pct=True):>6} · n={v.get('trades')}")
    print("\n  win rate by direction:")
    for k, v in (report.get("by_direction") or {}).items():
        print(f"    {k:<8} : win {_fmt(v.get('win_rate'), pct=True):>6} · "
              f"dir {_fmt(v.get('direction_accuracy'), pct=True):>6} · n={v.get('trades')}")
    print("\n  take-profit sweep (on signal BUYs):")
    for k, v in (report.get("tp_sweep") or {}).items():
        print(f"    {k:<20} win {_fmt(v.get('win_rate'), pct=True):>6} · "
              f"avg {_fmt(v.get('avg_ret_pct')):>7}% · PF {v.get('profit_factor')}")
    print("=" * 68)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", type=int, default=120, help="number of candle paths")
    ap.add_argument("--out", default="backtest_signals.json")
    ap.add_argument("--chart", default="backtest_signals.svg")
    ap.add_argument("--timeframe", default="minute")
    ap.add_argument("--aggregate", type=int, default=5)
    ap.add_argument("--bar-minutes", type=int, default=5)
    ap.add_argument("--step", type=int, default=3, help="entry every N candles")
    ap.add_argument("--lookahead", type=int, default=None, help="forward window in candles")
    ap.add_argument("--offline", action="store_true", help="use labelled synthetic paths")
    args = ap.parse_args()

    if args.offline:
        paths = synthetic_paths(args.trades)
        source = "SYNTHETIC (plumbing only — not a performance claim)"
    else:
        paths = real_paths(args.trades, args.timeframe, args.aggregate)
        source = "real GeckoTerminal OHLCV"

    if not paths:
        print("No candle paths available (network?). Re-run with --offline.", file=sys.stderr)
        return 2

    report = signal_backtest.run_backtest(
        paths, bar_minutes=args.bar_minutes, lookahead=args.lookahead,
        step=args.step, min_history=30)
    report["source"] = source
    report["config"] = {
        "signal": {k: config.SIGNAL.get(k) for k in
                   ("enabled", "min_confidence", "min_win_probability",
                    "require_bullish", "sell_on_bearish", "sell_confidence")},
        "take_profit_mode": config.EXITS.get("take_profit_mode"),
        "fee_pct": config.PAPER["fee_pct"], "slippage_pct": config.PAPER["slippage_pct"],
    }
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    try:
        build_chart(report, args.chart)
    except Exception as e:
        print(f"chart skipped ({type(e).__name__}: {e})", file=sys.stderr)

    print_report(report, source)
    print(f"\n  saved report : {args.out}")
    print(f"  saved chart  : {args.chart}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
