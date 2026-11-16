#!/usr/bin/env python3
"""Which score band actually makes money?

The desk's analyst scores a token 0-10 (desk/analyst.py::rules_score) and the
operator can set the floor with `analyst.valid_score` (registry bounds 3..9).
This script answers the only question that matters about that dial: if you raise
the floor, does profit go up?

Method
------
Every entry in research/backtest_g87e.json is re-scored from its OWN candle
context, using the parts of the analyst rubric that candles can actually
support -- volume surge, 1h momentum, 5m momentum, trend, and the "crowd" shape
test. The rubric's liquidity, volume/mcap, buy/sell ratio, buyer-diversity,
holder-concentration and socials blocks need DexScreener fields that candles do
not carry, so they are deliberately omitted rather than faked. That makes this a
*flow score*, not the full analyst score: it is the subset of the rubric that is
observable from price and volume alone.

Each entry is then replayed through the real desk.exits.evaluate() with the
super_legend_v2 exits, and profit is measured per score band and per threshold.

Run:
    python scripts/score_sweep.py
    python scripts/score_sweep.py --tf 5m
    python scripts/score_sweep.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from config import EXITS, SCALP  # noqa: E402

from scripts.backtest_presets import (  # noqa: E402
    load_candles,
    load_entries,
    profile_for,
    replay,
    round_trip_cost_pct,
)

PRESET = "super_legend_v2"
THRESHOLDS = (3, 4, 5, 6, 7, 8, 9)


# --------------------------------------------------------------------- scoring
def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b else 0.0


def _bars_for(minutes: int, tf: str) -> int:
    step = {"5m": 5, "15m": 15, "1h": 60}[tf]
    return max(1, round(minutes / step))


def flow_score(candles: list, i: int, tf: str) -> dict:
    """A 0-10 score built only from what a candle can prove.

    Mirrors desk/analyst.py's structure -- start at 3, add for volume, add for
    momentum, add for trend, add for the 'crowd' shape, subtract for a flush
    against the position -- but only the blocks candles can support.
    """
    n1h = _bars_for(60, tf)
    n5m = _bars_for(5, tf)
    lo = max(0, i - max(n1h, 20))
    window = candles[lo:i + 1]
    if len(window) < 5:
        return {"score": 3, "vol_z": None, "mom1h": None, "mom5m": None,
                "trend": None, "shape": "unknown"}

    close = candles[i]["close"]
    prior = candles[max(0, i - n1h)]
    mom1h = _pct(close, prior["close"])
    mom5m = _pct(close, candles[max(0, i - n5m)]["close"])

    # volume surge: this bar against the median of the bars before it
    hist = sorted(c["vol"] for c in candles[lo:i] if c.get("vol"))
    med = hist[len(hist) // 2] if hist else 0.0
    vol_z = (candles[i]["vol"] / med) if med else None

    # trend: close against the mean of the window
    ema = sum(c["close"] for c in window) / len(window)
    trend = _pct(close, ema)

    score = 3.0
    if vol_z is not None:
        if vol_z >= 3.0:
            score += 1.5
        elif vol_z >= 1.5:
            score += 0.8

    if mom1h >= 20:
        score += 1.5
    elif mom1h >= 5:
        score += 0.8

    if 0 < mom5m <= 15:
        score += 1.0          # early, not blown off
    elif mom5m > 15:
        score += 0.5          # already extended

    if trend > 0:
        score += 0.8          # above its own mean

    if mom1h > 40 and mom5m > 5:
        score += 0.7          # analyst's "crowd" shape
        shape = "crowd"
    elif mom5m <= -5:
        score -= 1.0          # flush against us
        shape = "flush"
    else:
        shape = "quiet"

    return {"score": max(0, min(10, int(round(score)))), "vol_z": vol_z,
            "mom1h": mom1h, "mom5m": mom5m, "trend": trend, "shape": shape}


# ------------------------------------------------------------------ reporting
def band_table(rows: list, cost: float) -> None:
    print(f"\n{'score':>5} {'n':>6} {'share':>7} {'win%':>6} {'gross%':>8} "
          f"{'net%':>7} {'total$':>9}")
    print("-" * 56)
    total = len(rows)
    for band in range(0, 11):
        sub = [r for r in rows if r["score"] == band]
        if not sub:
            continue
        rets = [r["ret"] for r in sub]
        net = [x - cost for x in rets]
        wins = sum(1 for x in net if x > 0)
        gross = sum(rets) / len(rets) * 100
        netp = sum(net) / len(net) * 100
        print(f"{band:>5} {len(sub):>6} {len(sub)/total:>6.1%} "
              f"{wins/len(sub):>5.1%} {gross:>+8.2f} {netp:>+7.2f} "
              f"{sum(net)*5:>+9.2f}")


def threshold_table(rows: list, cost: float) -> None:
    print(f"\n{'floor':>6} {'kept':>6} {'cut%':>6} {'win%':>6} {'gross%':>8} "
          f"{'net%':>7} {'PF':>6} {'total$':>9}")
    print("-" * 62)
    for t in THRESHOLDS:
        sub = [r for r in rows if r["score"] >= t]
        if not sub:
            print(f"{t:>6} {0:>6}")
            continue
        rets = [r["ret"] for r in sub]
        net = [x - cost for x in rets]
        wins = [x for x in net if x > 0]
        loss = abs(sum(x for x in net if x <= 0))
        gross = sum(rets) / len(rets) * 100
        netp = sum(net) / len(net) * 100
        pf = (sum(wins) / loss) if loss else float("inf")
        cut = 1 - len(sub) / len(rows)
        print(f"{t:>6} {len(sub):>6} {cut:>5.0%} {len(wins)/len(sub):>5.1%} "
              f"{gross:>+8.2f} {netp:>+7.2f} {pf:>6.2f} {sum(net)*5:>+9.2f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default=None)
    ap.add_argument("--ticket", type=float, default=5.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    candles = load_candles()
    entries = load_entries(candles, args.tf)
    if not entries:
        print("no entries found")
        return 1
    cost = round_trip_cost_pct(args.ticket)

    saved = {k: EXITS.get(k) for k in ("quote_max_jump_x", "quote_max_drop_x",
                                       "missing_quote_closes")}
    EXITS["quote_max_jump_x"] = 1e9
    EXITS["quote_max_drop_x"] = 1e9
    EXITS["missing_quote_closes"] = 10 ** 9

    snap = dict(SCALP)
    rows = []
    try:
        pr = profile_for(PRESET)
        for label, tf, t, cs in entries:
            i = t.get("entry_i")
            if i is None or i >= len(cs):
                continue
            sc = flow_score(cs, i, tf)
            r = replay(t, cs, pr, tf)
            if r is None:
                continue
            rows.append({**sc, "ret": r["ret"], "reason": r["reason"],
                         "label": label, "tf": tf})
    finally:
        SCALP.clear()
        SCALP.update(snap)
        for k, v in saved.items():
            if v is None:
                EXITS.pop(k, None)
            else:
                EXITS[k] = v

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(f"scored and replayed {len(rows)} real entries with {PRESET} exits"
          f"{' on ' + args.tf if args.tf else ' (5m/15m/1h)'}")
    print(f"round-trip cost deducted: {cost*100:.2f}% per trade "
          f"(${args.ticket:.0f} ticket)")

    band_table(rows, cost)
    threshold_table(rows, cost)

    # correlation: does a higher score actually predict a better trade?
    n = len(rows)
    xs = [r["score"] for r in rows]
    ys = [r["ret"] for r in rows]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    r = cov / (vx * vy) if vx and vy else 0.0
    print(f"\ncorrelation(score, gross return) = {r:+.4f}  (n={n})")
    print("  >0 means a higher score really did pick better trades.")
    print("  ~0 means the score has no predictive power on this population.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
