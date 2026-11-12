"""Analyze liquidity tiers from the cached OHLCV. No network."""
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
from backtest_100 import simulate_trade  # noqa: E402
from datetime import datetime  # noqa: E402

pools = {p["address"]: p for p in json.load(open("pools_cache.json"))}
cache = json.load(open("ohlcv_cache.json"))


def horizon_for(pool, candles):
    try:
        if pool.get("created_at"):
            created = datetime.fromisoformat(pool["created_at"].replace("Z", "+00:00")).timestamp()
            age_h = (candles[0]["ts"] - created) / 3600
            return "swing" if age_h >= config.EXITS["scalp_max_age_hours"] else "scalp"
    except Exception:
        pass
    return "scalp"


def run_tier(min_liq, cap=1000.0):
    trades = []
    for addr, candles in cache.items():
        p = pools.get(addr, {})
        if (p.get("liquidity") or 0) < min_liq:
            continue
        if len(candles) < 20:
            continue
        t = simulate_trade(candles, horizon_for(p, candles))
        if not t:
            continue
        t["pool"] = p.get("name") or addr[:8]
        t["liq"] = p.get("liquidity")
        trades.append(t)
    n = len(trades)
    if not n:
        return None
    rets = [min(x["ret_pct"], cap) for x in trades]
    wins = sum(1 for r in rets if r > 0)
    gw = sum(r for r in rets if r > 0)
    gl = -sum(r for r in rets if r <= 0)
    rugs = sum(1 for r in rets if r <= -90)
    return {
        "min_liq": min_liq, "n": n, "wins": wins, "losses": n - wins,
        "win_rate": round(wins / n * 100, 1),
        "median": round(statistics.median(rets), 1),
        "mean": round(statistics.mean(rets), 1),
        "pf": round(gw / gl, 2) if gl else None,
        "rug_pct": round(rugs / n * 100, 1),
        "best": round(max(rets), 1), "worst": round(min(rets), 1),
    }


tiers = [0, 25000, 50000, 100000, 250000]
print(f"{'Liquidity':>14} {'trades':>7} {'win%':>6} {'median%':>9} {'mean%':>8} {'PF':>5} {'rug%':>6}")
print("-" * 62)
results = {}
for t in tiers:
    r = run_tier(t)
    if not r:
        continue
    results[t] = r
    label = f">= ${t:,}" if t else "all"
    print(f"{label:>14} {r['n']:>7} {r['win_rate']:>6} {r['median']:>9} {r['mean']:>8} {str(r['pf']):>5} {r['rug_pct']:>6}")

json.dump(results, open("liq_tiers.json", "w"), indent=2)
print("\nsaved liq_tiers.json")
