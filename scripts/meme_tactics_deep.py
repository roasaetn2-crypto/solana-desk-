"""Deep sweep: can pullback entry + a tighter stop + a fast TP clear the bar?

The first pass (scripts/meme_tactics_eval.py) showed pullback-from-high is the
only guide rule that moves the win rate, but the default 1.5x/2x ladder still
loses money. This sweep asks the follow-up question honestly: is there ANY
combination of (pullback depth, stop width, take-profit plan) that reaches the
repo's stated bar on the same real entries?

Bar (unchanged, stated before the run):
  * >= 30 trades
  * win rate at least +5pp over the ungated baseline
  * profit factor >= 1.2 and not worse than baseline

Usage:
    python scripts/meme_tactics_deep.py --out meme_tactics_deep.json
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
from desk.signal_backtest import DEFAULT_TP_PLANS, simulate  # noqa: E402
from meme_tactics_eval import _baseline  # noqa: E402

DROPS = (0.05, 0.10, 0.20)
STOPS = (0.10, 0.15, 0.20, 0.30)


def sweep(base: list[dict], baseline: dict) -> dict:
    out = {}
    for drop in DROPS:
        kept = [r for r in base
                if r.get("recent_high") and r.get("close")
                and r["close"] <= r["recent_high"] * (1.0 - drop)]
        if not kept:
            continue
        for plan, overrides in DEFAULT_TP_PLANS.items():
            for stop in STOPS:
                rows = []
                for r in kept:
                    ov = dict(overrides)
                    ov["stop_pct"] = stop
                    prof = exit_rules.profile(r["horizon"], overrides=ov)
                    t = simulate(r["candles"], r["idx"], r["horizon"], prof=prof)
                    if t:
                        rows.append({"ret_pct": t["ret_pct"]})
                agg = ib._agg(rows)
                agg["win_lift"] = (round(agg["win_rate"] - baseline["win_rate"], 4)
                                   if agg.get("win_rate") is not None else None)
                agg["verdict"] = ib.verdict(agg, baseline)
                out[f"drop={int(100*drop)}% stop={int(100*stop)}% {plan}"] = agg
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--out", default="meme_tactics_deep.json")
    args = ap.parse_args()

    cache = ROOT / "data" / "indicator_paths.json"
    if not cache.exists():
        print("no candle cache; run scripts/indicator_eval.py first", file=sys.stderr)
        return 2
    paths = json.loads(cache.read_text(encoding="utf-8"))["paths"]
    base = _baseline(paths, 5, 30, args.step)
    baseline = ib._agg(base)
    res = sweep(base, baseline)
    adds = {k: v for k, v in res.items() if v["verdict"] == "ADD"}
    payload = {"baseline": baseline, "results": res, "adds": adds,
               "pools": len(paths)}
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("=" * 88)
    print(f"  DEEP SWEEP  baseline n={baseline['trades']} win "
          f"{100*baseline['win_rate']:.1f}% PF {baseline['profit_factor']}")
    print("=" * 88)
    top = sorted(res.items(), key=lambda kv: -(kv[1]["profit_factor"] or 0))[:15]
    print(f"  {'combo':52s} {'n':>5} {'win':>7} {'lift':>8} {'PF':>7}")
    for k, v in top:
        lift = "n/a" if v["win_lift"] is None else f"{100*v['win_lift']:+.1f}pp"
        print(f"  {k:52s} {v['trades']:>5} {100*v['win_rate']:>6.1f}% "
              f"{lift:>8} {str(v['profit_factor']):>7}")
    print("\n  ADD-WORTHY:", list(adds) or "none")
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
