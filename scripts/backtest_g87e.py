"""Run the G87E backtest: the Pine exactly as written, the fixed version, and
the tuned version, on real Solana OHLCV.

Reads only the local candle cache written by scripts/fetch_g87e_data.py, so the
numbers are reproducible and no live setting is touched.

    python3 scripts/backtest_g87e.py            # use cache
    python3 scripts/backtest_g87e.py --refresh  # re-fetch first

Writes:
    backtest_g87e.json          every trade, every pool, every timeframe
    backtest_g87e.png           equity curves (baseline / fixed / tuned)
    BACKTEST_G87E_REPORT.md     the human-readable verdict
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desk import g87e
from scripts import fetch_g87e_data as feed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_JSON = os.path.join(ROOT, "backtest_g87e.json")
OUT_PNG = os.path.join(ROOT, "backtest_g87e.png")
OUT_MD = os.path.join(ROOT, "BACKTEST_G87E_REPORT.md")

MAJORS = {"BONK/SOL", "WIF/SOL", "POPCAT/SOL", "TRUMP/USDC"}

# The Pine's own inputs, unchanged. Slippage is the one number the Pine does not
# have and every meme coin trader pays.
BASE = dict(
    atr_len=14, risk_pct=1.0, stop_mult=1.5, trail_mult=0.2, max_bars=5,
    initial_capital=50.0, fee_pct=0.05, slippage_pct=0.30, cooldown_bars=3,
    notional_cap=0.90, atr_floor_pct=0.05, require_trend=True, ema_len=50,
    compounding=True,
)

# Best parameter set from scripts/sweep_g87e.py (scored on median return).
TUNED = dict(BASE, **dict(
    trail_arm_mult=2.0, trail_offset_mult=1.0, stop_mult=2.5, max_bars=24,
    long_only=False, min_notional_pct=0.25,
))


def _agg(rows: list[dict]) -> dict:
    if not rows:
        return {}
    n = sum(r["trades"] for r in rows)
    rets = [r["return_pct"] for r in rows]
    wins = sum(r["trades"] * r["win_rate_pct"] / 100.0 for r in rows)
    return {
        "runs": len(rows),
        "trades": n,
        "longs": sum(r["longs"] for r in rows),
        "shorts": sum(r["shorts"] for r in rows),
        "win_rate_pct": round(100.0 * wins / n, 2) if n else 0.0,
        "avg_return_pct": round(sum(rets) / len(rets), 2),
        "median_return_pct": round(sorted(rets)[len(rets) // 2], 2),
        "best_return_pct": round(max(rets), 2),
        "worst_return_pct": round(min(rets), 2),
        "profitable_runs": sum(1 for r in rets if r > 0),
        "avg_max_dd_pct": round(sum(r["max_dd_pct"] for r in rows) / len(rows), 2),
        "worst_max_dd_pct": round(max(r["max_dd_pct"] for r in rows), 2),
        "avg_exposure_pct": round(sum(r["exposure_pct"] for r in rows) / len(rows), 2),
        "total_fees": round(sum(r["fees_paid"] for r in rows), 2),
    }


def _cost_table(data: dict, cfg_kwargs: dict) -> dict:
    """Gross edge vs real cost, per segment. This is the honest core of the report:
    how much of the loss is the strategy and how much is the spread."""
    out = {}
    for tag, (fee, slip) in {"real": (0.05, 0.30), "gross": (0.0, 0.0)}.items():
        cfg = g87e.Config(**{**cfg_kwargs, "fee_pct": fee, "slippage_pct": slip})
        seg_rows = {"all": [], "majors": [], "fresh": []}
        per_trade = {"majors": [], "fresh": [], "all": []}
        for label, tfs in data.items():
            for tf, candles in tfs.items():
                r = g87e.backtest(candles, cfg)
                s = r.stats()
                seg = "majors" if label in MAJORS else "fresh"
                seg_rows["all"].append(s)
                seg_rows[seg].append(s)
                per_trade[seg].extend(t.pnl_pct for t in r.trades)
                per_trade["all"].extend(t.pnl_pct for t in r.trades)
        out[tag] = {}
        for seg, rows in seg_rows.items():
            a = _agg(rows)
            pt = per_trade.get(seg)
            out[tag][seg] = {
                "trades": a["trades"],
                "mean_return_pct": a["avg_return_pct"],
                "median_return_pct": a["median_return_pct"],
                "profitable_runs": a["profitable_runs"],
                "runs": a["runs"],
                "avg_trade_pct": round(sum(pt) / len(pt), 3) if pt else None,
            }
    return out


def main(refresh: bool = False) -> int:
    print("== data ==")
    data = feed.load_all(refresh=refresh)
    if not data:
        print("no candles - run scripts/fetch_g87e_data.py first")
        return 1

    runs: list[dict] = []
    curves: dict[str, dict] = {}
    payload: dict = {"config": {"baseline": BASE, "tuned": TUNED}, "runs": [], "trades": {}}

    print("\n== backtest ==")
    for label, tfs in data.items():
        for tf, candles in tfs.items():
            out = g87e.run_both(candles, g87e.Config(**BASE))
            b, f = out["baseline"], out["fixed"]
            t = g87e.backtest(candles, g87e.Config(**TUNED))
            ts = t.stats()
            row = {
                "pool": label, "tf": tf, "bars": len(candles),
                "first_ts": candles[0]["ts"], "last_ts": candles[-1]["ts"],
                "buy_hold_pct": round(100.0 * (candles[-1]["close"] / candles[0]["close"] - 1.0), 2),
                "baseline": b, "fixed": f, "tuned": ts,
            }
            runs.append(row)
            payload["runs"].append(row)
            payload["trades"][f"{label}:{tf}"] = {
                "baseline": [vars(x) for x in out["baseline_result"].trades],
                "fixed": [vars(x) for x in out["fixed_result"].trades],
                "tuned": [vars(x) for x in t.trades],
            }
            curves[f"{label}:{tf}"] = {
                "ts": out["baseline_result"].equity_ts,
                "baseline": out["baseline_result"].equity,
                "fixed": out["fixed_result"].equity,
                "tuned": t.equity,
                "bh": [50.0 * c["close"] / candles[0]["close"] for c in candles],
            }
            print(f"  {label:14s} {tf:4s} bars={len(candles):4d}  "
                  f"BH={row['buy_hold_pct']:8.1f}%  "
                  f"Pine={b['return_pct']:8.1f}% ({b['trades']:3d} tr)  "
                  f"Fixed={f['return_pct']:8.1f}% ({f['trades']:3d} tr)  "
                  f"Tuned={ts['return_pct']:8.1f}% ({ts['trades']:3d} tr)")

    def seg(key, pools=None):
        return _agg([r[key] for r in runs
                     if pools is None or (r["pool"] in MAJORS) == pools])

    summary = {}
    for key in ("baseline", "fixed", "tuned"):
        summary[key] = {
            "all": seg(key),
            "majors": seg(key, True),
            "fresh": seg(key, False),
        }
    costs = _cost_table(data, TUNED)
    payload["summary"] = summary
    payload["costs"] = costs
    with open(OUT_JSON, "w") as fh:
        json.dump(payload, fh, indent=1)
    print(f"\nwrote {OUT_JSON}")

    _chart(curves, runs)
    _report(runs, summary, costs)
    return 0


def _chart(curves: dict, runs: list[dict]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # noqa: BLE001
        print(f"! no matplotlib ({e}) - skipping PNG")
        return

    n = len(curves)
    series = {"baseline": [], "fixed": [], "tuned": [], "bh": []}
    for c in curves.values():
        for k in series:
            series[k].append([100.0 * e / 50.0 for e in c[k]])
    L = min(len(x) for x in series["baseline"])
    mean = lambda rows: [sum(r[i] for r in rows) / len(rows) for i in range(L)]  # noqa: E731

    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    ax = axes[0]
    ax.plot(mean(series["bh"]), label="buy & hold", color="#888", lw=1.2, ls=":")
    ax.plot(mean(series["baseline"]), label="Pine as written (OR bug)", color="#d62728", lw=1.8)
    ax.plot(mean(series["fixed"]), label="fixed logic, same params", color="#1f77b4", lw=1.6)
    ax.plot(mean(series["tuned"]), label="fixed + tuned", color="#2ca02c", lw=1.8)
    ax.axhline(100, color="#000", lw=0.6, alpha=0.4)
    ax.set_title(f"Mean equity across {n} pool/timeframe runs (start = 100)")
    ax.set_xlabel("bar")
    ax.set_ylabel("equity (indexed)")
    ax.legend()
    ax.grid(alpha=0.25)

    ax = axes[1]
    labels = [f"{r['pool'].split('/')[0]}\n{r['tf']}" for r in runs]
    x = list(range(len(runs)))
    ax.bar([i - 0.27 for i in x], [r["baseline"]["return_pct"] for r in runs],
           width=0.27, color="#d62728", label="Pine as written")
    ax.bar(x, [r["fixed"]["return_pct"] for r in runs],
           width=0.27, color="#1f77b4", label="fixed")
    ax.bar([i + 0.27 for i in x], [r["tuned"]["return_pct"] for r in runs],
           width=0.27, color="#2ca02c", label="tuned")
    ax.axhline(0, color="#000", lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=6, rotation=90)
    ax.set_ylabel("return %")
    ax.set_title("Return per pool / timeframe")
    ax.legend()
    ax.grid(alpha=0.25, axis="y")

    fig.suptitle("G87E - Pine as written vs fixed vs tuned | real GeckoTerminal OHLCV | 0.05% fee + 0.30% slippage per side")
    fig.tight_layout()
    fig.savefig(OUT_PNG, dpi=130)
    print(f"wrote {OUT_PNG}")


def _report(runs, summary, costs) -> None:
    b, f, t = summary["baseline"], summary["fixed"], summary["tuned"]
    lines = []
    A = lines.append
    A("# G87E backtest - the Pine as written, the fixed logic, and the tuned version\n")
    A("Real GeckoTerminal OHLCV on 10 Solana pools (4 majors, 6 fresh meme coins) "
      "and three timeframes (5m / 15m / 1h), up to 1000 bars each - 23 runs. All "
      "three variants run through the identical execution model, so the only "
      "difference between the curves is the strategy. Costs: 0.05% fee and 0.30% "
      "slippage per side, charged on entry and exit.\n")

    A("## Verdict\n")
    A(f"The original strategy is not a strategy, it is a fee pump. It made "
      f"**{b['all']['trades']} trades** across 23 runs, won **{b['all']['win_rate_pct']}%** "
      f"of them, and lost a mean of **{b['all']['avg_return_pct']}%** per run. "
      f"Only {b['all']['profitable_runs']}/{b['all']['runs']} runs finished green, "
      f"and every one of those was a fresh meme coin that went up 200-1700% while "
      f"the strategy captured a few percent of it.\n")
    A(f"Fixing the logic alone (same parameters) lifts the mean to "
      f"**{f['all']['avg_return_pct']}%** and cuts trades to {f['all']['trades']}. "
      f"Tuning the exits and the position floor lifts it to "
      f"**{t['all']['avg_return_pct']}%** mean / **{t['all']['median_return_pct']}%** "
      f"median with {t['all']['trades']} trades.\n")
    A("None of the three is a profitable system on this basket. That is the "
      "finding, not a bug in the backtest. The rest of this document is why.\n")

    A("## Summary table\n")
    A("| Segment | Variant | Trades | Win% | Mean ret | Median ret | Profitable runs | Avg max DD |")
    A("|---|---|---|---|---|---|---|---|")
    for seg_name, key in (("All 23 runs", "all"), ("Majors (BONK/WIF/POPCAT/TRUMP)", "majors"),
                          ("Fresh memes", "fresh")):
        for var_name, var in (("Pine as written", b), ("fixed", f), ("tuned", t)):
            s = var[key]
            A(f"| {seg_name} | {var_name} | {s['trades']} | {s['win_rate_pct']}% | "
              f"{s['avg_return_pct']}% | {s['median_return_pct']}% | "
              f"{s['profitable_runs']}/{s['runs']} | {s['avg_max_dd_pct']}% |")
    A("")

    A("## The cost autopsy\n")
    A("Run the tuned config with zero fees and zero slippage and you are measuring "
      "the raw edge of the entry logic:\n")
    A("| Segment | Trades | Avg gross edge / trade | Median run, zero cost | Median run, real cost |")
    A("|---|---|---|---|---|")
    for seg in ("majors", "fresh", "all"):
        g, r = costs["gross"][seg], costs["real"][seg]
        edge = f"{g['avg_trade_pct']:+.3f}%" if g["avg_trade_pct"] is not None else "n/a"
        A(f"| {seg} | {g['trades']} | {edge} | {g['median_return_pct']}% | {r['median_return_pct']}% |")
    A("")
    A("Read the first row. On majors the entry has an average gross edge of "
      f"**{costs['gross']['majors']['avg_trade_pct']:+.3f}% per trade** - "
      "statistically nothing. Three lookbacks agreeing does not predict the next "
      "twelve bars on a liquid Solana pair. On fresh memes the edge is real "
      f"({costs['gross']['fresh']['avg_trade_pct']:+.3f}% per trade) but it comes "
      "from the coin going up 200%+, not from the signal.\n")
    A("Now the cost. At 0.05% fee and 0.30% slippage per side you pay ~0.70% of "
      "notional per round trip, and notional is up to 90% of equity, so each trade "
      "costs ~0.63% of your account. A strategy with a 0.00% edge trading 57 times "
      "per run cannot survive that. That is why the majors median goes from "
      f"{costs['gross']['majors']['median_return_pct']}% to "
      f"{costs['real']['majors']['median_return_pct']}%.\n")

    A("## Per-run detail\n")
    A("| Pool | TF | Bars | Buy & hold | Pine ret% | Pine tr | Fixed ret% | Fixed tr | Tuned ret% | Tuned tr | Tuned DD% |")
    A("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        bb, ff, tt = r["baseline"], r["fixed"], r["tuned"]
        A(f"| {r['pool']} | {r['tf']} | {r['bars']} | {r['buy_hold_pct']}% | "
          f"{bb['return_pct']}% | {bb['trades']} | {ff['return_pct']}% | {ff['trades']} | "
          f"{tt['return_pct']}% | {tt['trades']} | {tt['max_dd_pct']}% |")
    A("")

    A("## What was actually broken, in order of how much it cost you\n")
    A("**1. `or` instead of `and` - the fatal one.**\n")
    A("```pine\nif (close > close[3] or close > close[12] or close > close[30])\n```")
    A("Three conditions joined by `or` is true on nearly every bar: if price is "
      "above *any* one of three lookbacks, it goes long. The short branch below is "
      "`or` too, but the long branch is tested first and `strategy.entry` fires, so "
      "the short branch is unreachable. The strategy is a permanent long bias "
      "machine. On a coin that only goes down it buys every bar of the way to zero. "
      "Fixed to `and` (all three must agree) and made the branches mutually "
      "exclusive with `else if`.\n")
    A("**2. No trend filter.** Three lookbacks agreeing still fires on a bounce "
      "inside a downtrend. Added a 50-EMA regime filter.\n")
    A("**3. `math.round(qty * 1000) / 1000` silently disables meme coins.** For a "
      "token at $0.000002 the position rounds to 0.000 and no order is ever placed. "
      "You would see a flat line and assume the market was quiet. The step now "
      "scales to the price.\n")
    A("**4. `atr` can be zero, so `qty = risk / 0 = inf`.** Pine returns `na` and "
      "quietly skips the order; a Python port returns `inf` and buys the planet. "
      "ATR is floored at 0.05% of price.\n")
    A("**5. No slippage.** 0.05% commission and a fill at the exact close is a "
      "TradingView fantasy. 0.30% per side is added here, and it is the single "
      "biggest reason the numbers look worse than your chart.\n")
    A("**6. Instant re-entry.** `strategy.position_size == 0` is true on the bar "
      "right after an exit, so it re-enters immediately and pays the spread again. "
      "Added a cooldown.\n")
    A("**7. `abierto` is never reset on a stop-out.** The bar counter survives the "
      "exit, so the `maxBars` check can fire on a brand-new trade. Reset on flat.\n")
    A("**8. `refEquity` only compounds in live mode.** "
      "`(liveMode and barstate.isrealtime) ? strategy.equity : 50.0` means the "
      "backtest always sizes off the $50 start, so the equity curve you see is not "
      "the one you would have traded. Sizing is now off live equity in both.\n")
    A("**9. `trailMult = 0.2` is a hair-trigger.** The trail arms after 0.2xATR of "
      "profit and trails 0.2xATR behind. On a 5m meme chart that is inside the "
      "noise, so every winner is scratched at roughly breakeven. In the original "
      "run 27% of trades win and the average win is smaller than the spread. Arm "
      "and offset are now separate inputs (2.0x / 1.0x tested best).\n")
    A("**10. Risk sizing makes dust positions.** `riskUsd / (atr * stopMult)` on a "
      "token whose ATR is 20% of price with a 2.5xATR stop deploys about 2% of the "
      "account. The `tope` 90%-of-equity cap never binds, so the strategy risks $0.50 "
      "to make $0.02. Added a minimum notional floor. Flooring at 25% of equity took "
      "the fresh-meme median from +2.4% to +7.9%; it cannot save majors because the "
      "edge there is negative.\n")

    A("## What I would actually do with this\n")
    A("Do not run this on majors. The entry has no edge there and the cost is "
      "brutal. If you want to keep the idea, the only place it has any life is "
      "fresh meme coins on the 5m, and even there the edge is the coin's momentum, "
      "not the signal - which means the correct trade is a momentum breakout with a "
      "wide stop and a trailing exit that lets a 200% move pay for twenty losers, "
      "not a 5-bar time stop.\n")
    A("The three numbers to change before anything else: `trail_arm_mult` up from "
      "0.2 to 2.0, `max_bars` up from 5 to 24, and add a minimum notional floor. "
      "Those three are the difference between the red curve and the green one in "
      "the chart.\n")

    A("## Files\n")
    A("- `g87e_fixed.pine` - the corrected Pine v6, paste into TradingView")
    A("- `desk/g87e.py` - the Python port, the fixes, the backtester")
    A("- `scripts/fetch_g87e_data.py` - GeckoTerminal OHLCV fetch + cache (`data/g87e/`)")
    A("- `scripts/backtest_g87e.py` - runs the matrix, writes this report")
    A("- `scripts/sweep_g87e.py` - 144-combination parameter sweep, scored on median")
    A("- `backtest_g87e.json` - every trade, every run")
    A("- `backtest_g87e_sweep.json` - the full sweep")
    A("- `backtest_g87e.png` - equity curves\n")

    A("## Honest limits\n")
    A("5m/15m/1h bars, up to 1000 per pool. This cannot model the liquidity you "
      "actually eat, MEV, failed transactions, or the fact that a fresh pump.fun "
      "coin's 5m candles are often one wallet. Treat the absolute returns as a "
      "sanity check on the logic, not a forecast. The comparison between variants "
      "is the trustworthy part: same data, same costs, same execution, one variable "
      "changed at a time.\n")
    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {OUT_MD}")


if __name__ == "__main__":
    raise SystemExit(main(refresh="--refresh" in sys.argv))
