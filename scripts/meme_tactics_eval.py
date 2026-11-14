"""Evaluate memecoin-specific tactics from the public "high-profit meme bot" guide.

The guide's *indicator* toolkit (RSI / MACD / MA / Bollinger) has already been
tested on real GeckoTerminal candles by ``scripts/indicator_eval.py`` and lost
(ADD entry: none, ADD exit: none). What it also proposes is a set of
memecoin-specific *behavioural* rules that this repo has never measured:

  A. ATR-scaled dynamic stop      -- widen the stop when the coin is wild,
                                     tighten it when it is calm
  B. Pullback-from-high entry     -- refuse to buy the top; wait for a dip
  C. Flash-pump circuit breaker   -- skip entries right after a vertical candle
  D. Volatility ceiling           -- skip entries when ATR% is insane

Each is scored against the repo's own pre-stated bar (the same one the
indicator gate uses): at least MIN_TRADES trades, at least +5pp win rate over
baseline, and a profit factor >= 1.2 that is not worse than baseline.

Nothing here trades, writes settings or touches the live desk. It only reads
candle dicts and prints a verdict.

Usage:
    python scripts/meme_tactics_eval.py --pools 60 --out meme_tactics.json
    python scripts/meme_tactics_eval.py --offline --out meme_tactics_demo.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from desk import exits as exit_rules  # noqa: E402
from desk import indicator_backtest as ib  # noqa: E402
from desk import indicators as ind  # noqa: E402
from desk import signal_engine  # noqa: E402
from desk.signal_backtest import (  # noqa: E402
    DEFAULT_TP_PLANS, features_from_candles, simulate,
)

LOOK_HIGH = 20          # bars used for the "recent high" in the pullback rule
ATR_LOOK = 14


# --------------------------------------------------------------------------- #
# Feature helpers (all use only bars up to and including i)
# --------------------------------------------------------------------------- #
def _atr_pct(candles: list[dict], i: int) -> float | None:
    """ATR as a fraction of price at bar i."""
    lo = max(0, i + 1 - ib.WINDOW)
    snap = ind.snapshot(candles[lo:i + 1])
    a, c = snap.get("atr14"), snap.get("close")
    if a is None or not c:
        return None
    return a / c


def _recent_high(candles: list[dict], i: int, look: int = LOOK_HIGH) -> float | None:
    lo = max(0, i + 1 - look)
    highs = [c["high"] for c in candles[lo:i + 1] if c.get("high")]
    return max(highs) if highs else None


def _bar_move(candles: list[dict], i: int) -> float | None:
    """Close-to-close return of the most recent bar (the flash-pump signal)."""
    if i < 1:
        return None
    prev = candles[i - 1].get("close")
    cur = candles[i].get("close")
    if not prev or not cur:
        return None
    return cur / prev - 1.0


# --------------------------------------------------------------------------- #
# Baseline: every bar where the desk signal says BUY
# --------------------------------------------------------------------------- #
def _entry_idxs(candles: list[dict], bar_minutes: int, min_history: int,
                step: int) -> list[int]:
    out = []
    for i in range(min_history, len(candles) - 1, max(1, step)):
        token = features_from_candles(candles, i, bar_minutes)
        if signal_engine.evaluate(token)["action"] == "BUY":
            out.append(i)
    return out


def _baseline(paths: list[dict], bar_minutes: int, min_history: int,
              step: int) -> list[dict]:
    rows = []
    for p in paths:
        candles = p["candles"]
        horizon = p.get("horizon") or "scalp"
        for i in _entry_idxs(candles, bar_minutes, min_history, step):
            trade = simulate(candles, i, horizon)
            if not trade:
                continue
            rows.append({
                "candles": candles, "idx": i, "horizon": horizon,
                "ret_pct": trade["ret_pct"], "pnl": trade["pnl"],
                "atr_pct": _atr_pct(candles, i),
                "recent_high": _recent_high(candles, i),
                "bar_move": _bar_move(candles, i),
                "close": candles[i]["close"],
            })
    return rows


# --------------------------------------------------------------------------- #
# Tactics
# --------------------------------------------------------------------------- #
def tactic_atr_stop(base: list[dict], k: float) -> dict:
    """A. Re-run every baseline entry with stop = clamp(k * ATR%, 8%, 60%)."""
    rows = []
    for r in base:
        atr = r.get("atr_pct")
        if atr is None:
            continue
        stop = max(0.08, min(0.60, k * atr))
        prof = exit_rules.profile(r["horizon"], overrides={"stop_pct": stop})
        t = simulate(r["candles"], r["idx"], r["horizon"], prof=prof)
        if t:
            rows.append({"ret_pct": t["ret_pct"]})
    return ib._agg(rows)


def tactic_pullback(base: list[dict], drop: float) -> dict:
    """B. Only enter if price is at least ``drop`` below the recent high."""
    kept = []
    for r in base:
        hi, close = r.get("recent_high"), r.get("close")
        if hi and close and close <= hi * (1.0 - drop):
            kept.append(r)
    return ib._agg(kept)


def tactic_flash_breaker(base: list[dict], thr: float) -> dict:
    """C. Skip entries where the last bar already ran more than ``thr``."""
    kept = [r for r in base
            if r.get("bar_move") is not None and r["bar_move"] <= thr]
    return ib._agg(kept)


def tactic_vol_ceiling(base: list[dict], ceil: float) -> dict:
    """D. Skip entries when ATR% exceeds ``ceil``."""
    kept = [r for r in base
            if r.get("atr_pct") is not None and r["atr_pct"] <= ceil]
    return ib._agg(kept)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
GRID = {
    "A_atr_stop": ("stop = clamp(k*ATR%, 8%, 60%)",
                   [(f"k={k}", lambda b, k=k: tactic_atr_stop(b, k))
                    for k in (1.0, 1.5, 2.0, 2.5)]),
    "B_pullback": ("enter only if price <= recent20high*(1-drop)",
                   [(f"drop={int(100*d)}%", lambda b, d=d: tactic_pullback(b, d))
                    for d in (0.05, 0.10, 0.20, 0.30)]),
    "C_flash_breaker": ("skip if last bar move > thr",
                        [(f"thr={int(100*t)}%", lambda b, t=t: tactic_flash_breaker(b, t))
                         for t in (0.15, 0.25, 0.50)]),
    "D_vol_ceiling": ("skip if ATR% > ceil",
                      [(f"ceil={int(100*c)}%", lambda b, c=c: tactic_vol_ceiling(b, c))
                       for c in (0.10, 0.20, 0.40)]),
}


def combo_pullback_tp(base: list[dict], drop: float, overrides: dict) -> dict:
    """B combined with an alternative take-profit ladder.

    The guide's second claim is "take profits fast on memecoins". Pullback
    entries lift the win rate but the default 1.5x/2x ladder still loses, so we
    replay the same pullback entries under each standard TP plan to see whether
    the pair clears the bar together.
    """
    rows = []
    for r in base:
        hi, close = r.get("recent_high"), r.get("close")
        if not (hi and close and close <= hi * (1.0 - drop)):
            continue
        prof = exit_rules.profile(r["horizon"], overrides=overrides)
        t = simulate(r["candles"], r["idx"], r["horizon"], prof=prof)
        if t:
            rows.append({"ret_pct": t["ret_pct"]})
    return ib._agg(rows)


def run(paths: list[dict], bar_minutes: int = 5, min_history: int = 30,
        step: int = 2) -> dict:
    base = _baseline(paths, bar_minutes, min_history, step)
    baseline = ib._agg(base)
    out = {}
    for tactic, (desc, variants) in GRID.items():
        results = {}
        for label, fn in variants:
            agg = fn(base)
            agg["kept_pct"] = (round(agg["trades"] / len(base), 4)
                               if base and agg.get("trades") else None)
            agg["win_lift"] = (round(agg["win_rate"] - baseline["win_rate"], 4)
                               if agg.get("win_rate") is not None
                               and baseline.get("win_rate") is not None else None)
            agg["verdict"] = ib.verdict(agg, baseline)
            results[label] = agg
        out[tactic] = {"desc": desc, "variants": results}

    combos = {}
    for drop in (0.05, 0.10, 0.20):
        for plan, overrides in DEFAULT_TP_PLANS.items():
            agg = combo_pullback_tp(base, drop, overrides)
            agg["win_lift"] = (round(agg["win_rate"] - baseline["win_rate"], 4)
                               if agg.get("win_rate") is not None
                               and baseline.get("win_rate") is not None else None)
            agg["verdict"] = ib.verdict(agg, baseline)
            combos[f"drop={int(100*drop)}% + {plan}"] = agg

    return {
        "baseline": baseline,
        "tactics": out,
        "combos": combos,
        "paths": len(paths),
        "bars": sum(len(p["candles"]) for p in paths),
        "bar": {"min_trades": ib.MIN_TRADES, "min_win_lift": ib.MIN_WIN_LIFT,
                "min_pf": ib.MIN_PF},
    }


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def print_report(rep: dict, source: str) -> None:
    b = rep["baseline"]
    print("=" * 82)
    print(f"  MEMECOIN TACTICS EVALUATION  ({source})")
    print("=" * 82)
    print(f"  baseline (signal BUY only): n={b['trades']}  win {_pct(b['win_rate'])}  "
          f"avg {b['avg_ret_pct']}%  PF {b['profit_factor']}")
    print(f"  bar: n>={ib.MIN_TRADES}, win lift>={100*ib.MIN_WIN_LIFT:.0f}pp, "
          f"PF>={ib.MIN_PF} and >= baseline")
    for tactic, block in rep["tactics"].items():
        print(f"\n  {tactic}  --  {block['desc']}")
        print(f"    {'variant':14s} {'n':>5} {'win':>7} {'lift':>8} {'PF':>7} "
              f"{'kept':>7}  verdict")
        for label, v in block["variants"].items():
            lift = "n/a" if v["win_lift"] is None else f"{100*v['win_lift']:+.1f}pp"
            kept = "n/a" if v.get("kept_pct") is None else f"{100*v['kept_pct']:.0f}%"
            print(f"    {label:14s} {v['trades']:>5} {_pct(v['win_rate']):>7} "
                  f"{lift:>8} {str(v['profit_factor']):>7} {kept:>7}  {v['verdict']}")
    if rep.get("combos"):
        print("\n  PULLBACK x TAKE-PROFIT (the guide's two claims together)")
        print(f"    {'combo':34s} {'n':>5} {'win':>7} {'lift':>8} {'PF':>7}  verdict")
        for label, v in rep["combos"].items():
            lift = "n/a" if v["win_lift"] is None else f"{100*v['win_lift']:+.1f}pp"
            print(f"    {label:34s} {v['trades']:>5} {_pct(v['win_rate']):>7} "
                  f"{lift:>8} {str(v['profit_factor']):>7}  {v['verdict']}")
    print("=" * 82)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", type=int, default=60)
    ap.add_argument("--timeframe", default="minute")
    ap.add_argument("--aggregate", type=int, default=5)
    ap.add_argument("--bar-minutes", type=int, default=5)
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default="meme_tactics.json")
    args = ap.parse_args()

    cache = ROOT / "data" / "indicator_paths.json"
    if args.offline:
        paths = ib.synth_paths(n=args.pools, bars=240, seed=7)
        source = "SYNTHETIC (plumbing only - not a performance claim)"
    elif cache.exists():
        paths = json.loads(cache.read_text(encoding="utf-8"))["paths"]
        source = f"real GeckoTerminal OHLCV (cached, {len(paths)} pools)"
    else:
        from indicator_eval import real_paths
        paths = real_paths(args.pools, args.timeframe, args.aggregate,
                           args.bar_minutes)
        source = "real GeckoTerminal OHLCV (fresh fetch)"
        if paths:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"paths": paths}), encoding="utf-8")
    if not paths:
        print("No candle paths (network?). Re-run with --offline.", file=sys.stderr)
        return 2

    rep = run(paths, bar_minutes=args.bar_minutes, step=args.step)
    rep["source"] = source
    Path(args.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print_report(rep, source)
    print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
