"""Does the desk's discipline layer rescue G87E on the unbiased sample?

The survivorship run (scripts/survivorship_g87e.py) proved the G87E entry has no
edge: 0/9 meme pools profitable, median -22.50%. The natural next question is
whether the desk's *existing* filters fix that, because if they do the desk has
something shippable and if they don't it has an answer.

What can honestly be tested here
--------------------------------
* ``desk/guard.py`` - pure functions, no I/O. The circuit breaker (3 losses in a
  row -> pause, 2 -> half size, day P&L past -2% -> half size) is fully
  replayable against history. This is a REAL test of real desk code.
* ``desk/signals.py`` - GoPlus / GMGN / PumpPortal / DexScreener / Jupiter. All
  live network calls. There is no historical snapshot of those flags, so they
  CANNOT be backtested. Claiming otherwise would be fabricating data.

So for the signals layer we test the one component that is derivable from
OHLCV: **volume expansion versus its own baseline**. That is a proxy for the
volume chip, not the chip itself. Labelled as such everywhere.

Variants
--------
  A  control        - G87E fixed exits, no gate (must reproduce -22.50%)
  B  breaker        - + desk/guard.py circuit breaker (real code)
  C  volgate        - + volume expansion >= 1.5x its 20-bar mean (proxy)
  D  breaker+vol    - both

    python3 scripts/guard_g87e.py
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from desk import g87e, guard

GT = "https://api.geckoterminal.com/api/v2"
UA = {"Accept": "application/json", "User-Agent": "solana-desk-g87e/1.0"}
CACHE = os.path.join(ROOT, "data", "g87e_surv")
SURV = os.path.join(ROOT, "backtest_g87e_survivorship.json")
OUT = os.path.join(ROOT, "backtest_g87e_guard.json")
PACE, _last = 3.2, [0.0]

TUNED = dict(
    atr_len=14, risk_pct=1.0, stop_mult=2.5, max_bars=24, initial_capital=50.0,
    fee_pct=0.05, slippage_pct=0.30, cooldown_bars=3, notional_cap=0.90,
    atr_floor_pct=0.05, require_trend=True, ema_len=50, compounding=True,
    variant="fixed", trail_arm_mult=2.0, trail_offset_mult=1.0,
    min_notional_pct=0.25,
)
VOL_LB = 20
VOL_MULT = 1.5


def log(m=""):
    print(m, flush=True)


def _pace():
    w = PACE - (time.time() - _last[0])
    if w > 0:
        time.sleep(w)
    _last[0] = time.time()


def get(url, tries=3):
    for a in range(tries):
        _pace()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(30.0 * (a + 1))
            elif e.code in (401, 403, 404):
                return None
            else:
                time.sleep(4.0 * (a + 1))
        except Exception:  # noqa: BLE001
            time.sleep(4.0 * (a + 1))
    return None


def candles(pool):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{pool[:8]}.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    url = (f"{GT}/networks/solana/pools/{pool}/ohlcv/minute"
           f"?aggregate=5&limit=1000&currency=usd")
    body = get(url)
    if not body:
        return []
    raw = (body.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    out = [{"ts": float(t), "open": float(o), "high": float(h), "low": float(l),
            "close": float(c), "vol": float(v or 0)}
           for t, o, h, l, c, v in raw if c and float(c) > 0]
    out.sort(key=lambda x: x["ts"])
    with open(p, "w") as f:
        json.dump(out, f)
    return out


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def make_breaker_gate():
    """Real desk/guard.py circuit breaker, replayed bar by bar."""
    day_start = {}

    def gate(i, row, ctx):
        ts = ctx["ts"]
        day = int(ts // 86400)
        eq = ctx["equity"]
        day_start.setdefault(day, eq)
        closed = [{"pnl_usd": t.pnl, "closed": t.exit_ts} for t in ctx["trades"]]
        day_pnl = sum(t.pnl for t in ctx["trades"] if int(t.exit_ts // 86400) == day)
        br = guard.circuit_breaker(closed, day_pnl, day_start[day], now=ts)
        return br["mult"]

    return gate


def make_vol_gate(mult=VOL_MULT, lb=VOL_LB):
    """Proxy for the volume-expansion chip: vol must be >= mult x its lb-mean."""
    def gate(i, row, ctx):
        c = ctx["candles"]
        if i < lb:
            return 1.0
        base = sum(float(x["vol"]) for x in c[i - lb:i]) / lb
        if base <= 0:
            return 1.0
        return 1.0 if float(c[i]["vol"]) >= mult * base else 0.0

    return gate


def both(*gates):
    def gate(i, row, ctx):
        m = 1.0
        for g in gates:
            m *= g(i, row, ctx)
            if m <= 0:
                return 0.0
        return m

    return gate


# --------------------------------------------------------------------------- #
def summarise(rows, label):
    r = [x["return_pct"] for x in rows]
    b = [x["buy_hold_pct"] for x in rows]
    return {
        "label": label,
        "pools": len(rows),
        "trades": sum(x["trades"] for x in rows),
        "median_return_pct": round(st.median(r), 2),
        "mean_return_pct": round(sum(r) / len(r), 2),
        "profitable_runs": sum(1 for x in r if x > 0),
        "worst_return_pct": round(min(r), 2),
        "best_return_pct": round(max(r), 2),
        "avg_win_rate_pct": round(sum(x["win_rate_pct"] for x in rows) / len(rows), 1),
        "avg_max_dd_pct": round(sum(x["max_dd_pct"] for x in rows) / len(rows), 1),
        "mean_buy_hold_pct": round(sum(b) / len(b), 1),
        "blocked": sum(x.get("blocked", 0) for x in rows),
        "halved": sum(x.get("halved", 0) for x in rows),
    }


def main():
    with open(SURV) as f:
        surv = json.load(f)
    pools = [{"pool": x["pool"], "label": x["label"], "liq": x["liq"]}
             for x in surv["runs"]]
    log(f"replaying the SAME {len(pools)} unbiased pools as the survivorship run\n")

    data = {}
    for p in pools:
        c = candles(p["pool"])
        if len(c) >= 60:
            data[p["pool"]] = c
            log(f"  {p['label'][:20]:20s} {len(c):4d} bars")
        else:
            log(f"  {p['label'][:20]:20s} SKIP {len(c)} bars")

    variants = {
        "A control": None,
        "B breaker": make_breaker_gate(),
        "C volgate": make_vol_gate(),
        "D breaker+vol": both(make_breaker_gate(), make_vol_gate()),
    }

    all_rows, summary = {}, []
    for name, gate in variants.items():
        rows = []
        for p in pools:
            c = data.get(p["pool"])
            if not c:
                continue
            r = g87e.backtest(c, g87e.Config(**TUNED), gate=gate)
            s = r.stats()
            bh = 100.0 * (c[-1]["close"] / c[0]["close"] - 1.0)
            rows.append({**p, "bars": len(c), "buy_hold_pct": round(bh, 2), **s})
        all_rows[name] = rows
        summary.append(summarise(rows, name))

    log("\n" + "=" * 78)
    log(f"{'variant':16s} {'pools':>5s} {'trades':>7s} {'median':>9s} {'mean':>9s} "
        f"{'green':>7s} {'win%':>6s} {'DD%':>6s} {'blk':>5s}")
    log("=" * 78)
    for s in summary:
        log(f"{s['label']:16s} {s['pools']:5d} {s['trades']:7d} "
            f"{s['median_return_pct']:8.2f}% {s['mean_return_pct']:8.2f}% "
            f"{s['profitable_runs']:3d}/{s['pools']:<3d} {s['avg_win_rate_pct']:5.1f} "
            f"{s['avg_max_dd_pct']:5.1f} {s['blocked']:5d}")

    ctrl = summary[0]
    log("\nvs control (A):")
    for s in summary[1:]:
        log(f"  {s['label']:16s} median {s['median_return_pct'] - ctrl['median_return_pct']:+7.2f} pp   "
            f"mean {s['mean_return_pct'] - ctrl['mean_return_pct']:+7.2f} pp   "
            f"green {s['profitable_runs'] - ctrl['profitable_runs']:+d}")

    # static liquidity-floor sub-analysis on the control
    log("\nliquidity floor on the control (static, pool-level):")
    liq_rows = []
    for floor in (0, 50_000, 100_000):
        sub = [x for x in all_rows["A control"] if x["liq"] >= floor]
        if not sub:
            continue
        med = st.median([x["return_pct"] for x in sub])
        liq_rows.append({"floor": floor, "pools": len(sub),
                         "median_return_pct": round(med, 2),
                         "profitable_runs": sum(1 for x in sub if x["return_pct"] > 0)})
        log(f"  liq >= ${floor:>7,}: {len(sub):2d} pools, median {med:+7.2f}%, "
            f"green {sum(1 for x in sub if x['return_pct'] > 0)}/{len(sub)}")

    with open(OUT, "w") as f:
        json.dump({"config": TUNED, "vol_gate": {"lookback": VOL_LB, "mult": VOL_MULT},
                   "summary": summary, "liquidity_floor": liq_rows,
                   "runs": all_rows}, f, indent=1)
    log(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
