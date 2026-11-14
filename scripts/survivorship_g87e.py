"""Survivorship check: does the +7.9% fresh-meme median survive a fair sample?

The first backtest picked its "fresh meme" pools from the top of GeckoTerminal's
volume ranking - i.e. the coins that had already gone up 200-1700%. That is
survivorship bias, and it is the single easiest way to lie to yourself with a
backtest.

This script takes the opposite sample: pools from deep pages of the pool list,
low liquidity, low volume, no performance filter at all. This is the population
you actually face when you scan for something to buy. If the strategy only works
on the winners, it does not work.

    python3 scripts/survivorship_g87e.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desk import g87e

GT = "https://api.geckoterminal.com/api/v2"
UA = {"Accept": "application/json", "User-Agent": "solana-desk-g87e/1.0"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "backtest_g87e_survivorship.json")

PACE = 3.2          # seconds between requests -> ~19/min, safely under the free tier
_last = [0.0]

TUNED = dict(
    atr_len=14, risk_pct=1.0, stop_mult=2.5, max_bars=24, initial_capital=50.0,
    fee_pct=0.05, slippage_pct=0.30, cooldown_bars=3, notional_cap=0.90,
    atr_floor_pct=0.05, require_trend=True, ema_len=50, compounding=True,
    variant="fixed", trail_arm_mult=2.0, trail_offset_mult=1.0,
    min_notional_pct=0.25,
)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _pace() -> None:
    wait = PACE - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


def get(url: str, tries: int = 3):
    for a in range(tries):
        _pace()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:  # noqa: PERF203
            if e.code == 429:
                back = 30.0 * (a + 1)
                log(f"    [429] cooling down {back:.0f}s")
                time.sleep(back)
            elif e.code in (401, 403, 404):
                # free tier: only pages 1-10 are public; do not burn retries
                return None
            else:
                time.sleep(4.0 * (a + 1))
        except Exception:  # noqa: BLE001
            time.sleep(4.0 * (a + 1))
    return None


def sample_pools(pages=tuple(range(2, 11)), min_liq=15_000,
                 max_liq=300_000, want=24, min_age_days=2) -> list[dict]:
    """Mid-rank pools: tradable, but NOT the top-of-volume winners.

    The original sample took the top of GeckoTerminal's volume ranking - the
    coins that had already run 200-1700%. That is survivorship bias. Here we
    walk mid-rank pages (ranks ~40-200 by volume), require only that a pool is
    tradable (liquidity 15k-300k, 24h volume >= 25k) and at least a few days
    old so the 5m series has history. There is NO performance filter: we do not
    care whether the coin went up or down before we found it.
    """
    import datetime as _dt

    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=min_age_days)
    out = []
    seen = set()
    for p in pages:
        body = get(f"{GT}/networks/solana/pools?page={p}")
        if not body:
            log(f"  page {p}: no data")
            continue
        got = 0
        for x in body.get("data", []):
            a = x["attributes"]
            liq = float(a.get("reserve_in_usd") or 0)
            vol = float((a.get("volume_usd") or {}).get("h24") or 0)
            name = a.get("name") or ""
            created = (a.get("pool_created_at") or "")[:19]
            if liq < min_liq or liq > max_liq or vol < 25_000:
                continue
            if any(s in name.upper() for s in ("/ USDC", "/ USDT", "SOL /", "USD1",
                                               "USDG", "JITOSOL", "CBBTC", "WETH",
                                               "JLP", "PUMP /")):
                continue
            try:
                made = _dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
                if made.tzinfo is None:
                    made = made.replace(tzinfo=_dt.timezone.utc)
                if made > cutoff:
                    continue
            except ValueError:
                continue
            pool = x["id"].replace("solana_", "")
            if pool in seen:
                continue
            seen.add(pool)
            out.append({"pool": pool, "label": name, "liq": liq, "vol24": vol,
                        "created": created[:10]})
            got += 1
        log(f"  page {p}: +{got} pools (total {len(out)})")
        if len(out) >= want:
            break
    return out[:want]


def candles(pool: str, tf: str = "5m", limit: int = 1000) -> list[dict]:
    spec = {"5m": ("minute", 5), "15m": ("minute", 15), "1h": ("hour", 1)}[tf]
    url = (f"{GT}/networks/solana/pools/{pool}/ohlcv/{spec[0]}"
           f"?aggregate={spec[1]}&limit={limit}&currency=usd")
    body = get(url)
    if not body:
        return []
    raw = (body.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    out = [{"ts": float(t), "open": float(o), "high": float(h), "low": float(l),
            "close": float(c), "vol": float(v or 0)}
           for t, o, h, l, c, v in raw if c and float(c) > 0]
    out.sort(key=lambda x: x["ts"])
    return out


def main() -> int:
    log("== sampling pools with NO performance filter ==")
    pools = sample_pools(want=45)
    log(f"{len(pools)} candidate pools\n")

    rows = []
    for i, p in enumerate(pools, 1):
        if len(rows) >= 20:
            log("  reached 20 usable runs, stopping")
            break
        c = candles(p["pool"])
        if len(c) < 60:
            log(f"  [{i:2d}/{len(pools)}] skip {p['label'][:22]:22s} only {len(c)} bars")
            continue
        r = g87e.backtest(c, g87e.Config(**TUNED))
        s = r.stats()
        bh = 100.0 * (c[-1]["close"] / c[0]["close"] - 1.0)
        rows.append({**p, "bars": len(c), "buy_hold_pct": round(bh, 2), **s})
        log(f"  [{i:2d}/{len(pools)}] {p['label'][:22]:22s} liq {p['liq']:8.0f}  "
            f"bars {len(c):4d}  BH {bh:9.1f}%  strat {s['return_pct']:8.1f}%  "
            f"({s['trades']:3d} tr, {s['win_rate_pct']}% win)")

    if not rows:
        log("no usable pools")
        return 1

    rets = [r["return_pct"] for r in rows]
    bh = [r["buy_hold_pct"] for r in rows]
    summary = {
        "pools": len(rows),
        "trades": sum(r["trades"] for r in rows),
        "mean_return_pct": round(sum(rets) / len(rets), 2),
        "median_return_pct": round(sorted(rets)[len(rets) // 2], 2),
        "profitable_runs": sum(1 for r in rets if r > 0),
        "worst_return_pct": round(min(rets), 2),
        "best_return_pct": round(max(rets), 2),
        "mean_buy_hold_pct": round(sum(bh) / len(bh), 2),
        "median_buy_hold_pct": round(sorted(bh)[len(bh) // 2], 2),
        "pools_that_went_up": sum(1 for b in bh if b > 0),
    }
    with open(OUT, "w") as f:
        json.dump({"config": TUNED, "summary": summary, "runs": rows}, f, indent=1)

    log("\n== survivorship-free sample ==")
    for k, v in summary.items():
        log(f"  {k:24s} {v}")
    log(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
