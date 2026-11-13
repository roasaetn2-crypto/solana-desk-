"""Historical 100-trade backtest on REAL GeckoTerminal price data.

There is no recorded journal.jsonl in the repo, so desk/backtest.py has nothing
to replay. This script instead pulls real Solana pool OHLCV history from the
public GeckoTerminal API and replays the desk's OWN exit rules (desk/exits.py)
plus PAPER fees/slippage over each real price path, then reports the win rate.

It is a backtest of the EXIT/RISK rules given an entry, not of the entry gates
(score/security/liquidity are not reconstructable historically). Every price is
real; nothing is invented.

Usage:
    python scripts/backtest_100.py --trades 100 --out backtest_100.json
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

import config  # noqa: E402
from desk import exits  # noqa: E402

GT = "https://api.geckoterminal.com/api/v2"
HDRS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 desk-backtest"}
SESSION = requests.Session()
SESSION.headers.update(HDRS)


def get(url: str, params: dict | None = None, tries: int = 4) -> dict | None:
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 + i * 2)
                continue
            return None
        except requests.RequestException:
            time.sleep(1.5)
    return None


def collect_pools(want: int) -> list[dict]:
    """Gather pool addresses from trending + new + top pools, deduped."""
    pools: dict[str, dict] = {}
    sources = [
        ("trending_pools", 5),
        ("new_pools", 5),
        ("pools", 5),
    ]
    for endpoint, pages in sources:
        for page in range(1, pages + 1):
            body = get(f"{GT}/networks/solana/{endpoint}", {"page": page})
            for row in (body or {}).get("data", []) or []:
                addr = (row.get("id") or "").replace("solana_", "")
                if not addr or addr in pools:
                    continue
                a = row.get("attributes") or {}
                pools[addr] = {
                    "address": addr,
                    "name": a.get("name"),
                    "created_at": a.get("pool_created_at"),
                    "liquidity": a.get("reserve_in_usd"),
                    "volume_h24": a.get("volume_usd", {}).get("h24") if isinstance(a.get("volume_usd"), dict) else None,
                }
            time.sleep(0.4)
            if len(pools) >= want * 2:
                return list(pools.values())
    return list(pools.values())


def fetch_ohlcv(addr: str, timeframe: str = "minute", aggregate: int = 5, limit: int = 1000):
    body = get(f"{GT}/networks/solana/pools/{addr}/ohlcv/{timeframe}",
               {"aggregate": aggregate, "limit": limit, "currency": "usd"})
    if not body:
        return []
    lst = (((body.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
    # GeckoTerminal returns newest first; make chronological
    candles = [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                "low": float(c[3]), "close": float(c[4]), "vol": float(c[5])}
               for c in lst if c and len(c) >= 6]
    candles.sort(key=lambda c: c["ts"])
    return candles


def simulate_trade(candles: list[dict], horizon: str) -> dict | None:
    """Enter at the first candle close, walk forward applying desk exit rules."""
    if len(candles) < 20:
        return None
    entry = candles[0]["close"]
    if not entry or entry <= 0:
        return None
    opened = candles[0]["ts"]
    pos = {"entry": entry, "opened": opened}
    exits.init_state(pos, horizon)
    remaining = 1.0
    cash = 0.0
    fee = config.PAPER["fee_pct"]
    slip = config.PAPER["slippage_pct"]
    # entry cost: 1 unit notional, pay fee+slippage on entry
    cost_basis = 1.0 * (1 + fee + slip)
    realized_reasons = []
    last_price = entry
    for c in candles[1:]:
        price = c["close"]
        last_price = price
        q = {"price_usd": price, "volume_h6": c["vol"], "volume_h24": c["vol"] * 4}
        sig = exits.evaluate(pos, q, now=c["ts"])
        if not sig:
            continue
        frac, why = sig
        sell_frac = remaining * frac
        proceeds = sell_frac * price * (1 - fee - slip)
        cash += proceeds
        remaining -= sell_frac
        realized_reasons.append(why)
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        # still open at data end -> close at last price (mark to market)
        cash += remaining * last_price * (1 - fee - slip)
        remaining = 0.0
        realized_reasons.append("data end close")
    pnl = cash - cost_basis
    return {"entry": entry, "exit_px": last_price, "pnl": pnl,
            "ret_pct": pnl / cost_basis * 100, "reasons": realized_reasons,
            "horizon": horizon}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", type=int, default=100)
    ap.add_argument("--out", default="backtest_100.json")
    ap.add_argument("--timeframe", default="minute")
    ap.add_argument("--aggregate", type=int, default=5)
    args = ap.parse_args()

    print(f"Collecting pools (need ~{args.trades * 2})...", flush=True)
    pools = collect_pools(args.trades * 2)
    print(f"  got {len(pools)} unique pools", flush=True)

    trades: list[dict] = []
    used = 0
    for p in pools:
        if len(trades) >= args.trades:
            break
        used += 1
        candles = fetch_ohlcv(p["address"], args.timeframe, args.aggregate)
        time.sleep(0.35)
        if len(candles) < 20:
            continue
        # horizon: scalp if the pool is young (<6h) else swing; unknown -> scalp
        horizon = "scalp"
        try:
            from datetime import datetime
            if p.get("created_at"):
                created = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")).timestamp()
                age_h = (candles[0]["ts"] - created) / 3600
                horizon = "swing" if age_h >= config.EXITS["scalp_max_age_hours"] else "scalp"
        except Exception:
            horizon = "scalp"
        t = simulate_trade(candles, horizon)
        if not t:
            continue
        t["pool"] = p["name"] or p["address"][:8]
        t["address"] = p["address"]
        t["candles"] = len(candles)
        trades.append(t)
        if len(trades) % 10 == 0:
            print(f"  simulated {len(trades)} trades ({used} pools scanned)", flush=True)

    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = n - wins
    avg_ret = sum(t["ret_pct"] for t in trades) / n if n else 0
    gross_win = sum(t["ret_pct"] for t in trades if t["pnl"] > 0)
    gross_loss = -sum(t["ret_pct"] for t in trades if t["pnl"] <= 0)
    pf = gross_win / gross_loss if gross_loss else None
    reason_counts: dict[str, int] = {}
    for t in trades:
        for r in t["reasons"]:
            key = r.split(" at ")[0].split(" -")[0]
            reason_counts[key] = reason_counts.get(key, 0) + 1

    result = {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n, 4) if n else None,
        "total_return_pct_sum": round(sum(t["ret_pct"] for t in trades), 2),
        "avg_return_pct": round(avg_ret, 3),
        "profit_factor": round(pf, 3) if pf else None,
        "best_pct": round(max((t["ret_pct"] for t in trades), default=0), 2),
        "worst_pct": round(min((t["ret_pct"] for t in trades), default=0), 2),
        "exit_reasons": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])),
        "pools_scanned": used,
        "config": {
            "stop_pct": config.EXITS["stop_pct"],
            "profiles": config.EXITS["profiles"],
            "fee_pct": config.PAPER["fee_pct"],
            "slippage_pct": config.PAPER["slippage_pct"],
        },
        "trades_detail": trades,
    }
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  BACKTEST RESULT  ({n} trades, real GeckoTerminal data)")
    print("=" * 60)
    print(f"  Win rate      : {result['win_rate'] * 100:.1f}%  ({wins}W / {losses}L)")
    print(f"  Avg return    : {result['avg_return_pct']:+.2f}% per trade")
    print(f"  Profit factor : {result['profit_factor']}")
    print(f"  Best / Worst  : {result['best_pct']:+.1f}% / {result['worst_pct']:+.1f}%")
    print(f"  Exit reasons  : {result['exit_reasons']}")
    print(f"  Saved to      : {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
