"""Does the meme scorecard actually raise the win rate? And which settings?

The old backtest (scripts/backtest_100.py) answered a narrower question: "given
an entry, what do the exit rules earn?" It got 13% win rate and said, in its own
report, that the fix is *entry gating*, not looser exits.

This script answers the next question: **if the desk only traded what its gates
would have let through, what would the win rate be?**

It is a two-phase backtest so the network is touched once and every experiment
after that is free and reproducible:

    python scripts/backtest_gates.py fetch --pools 400
    python scripts/backtest_gates.py run

`fetch` pulls real Solana pools from GeckoTerminal (trending + new + top), keeps
each pool's attribute snapshot and its 5-minute OHLCV history, and writes
`backtest_cache.json`.

`run` replays that cache offline. For every pool it:

  1. cuts the candles at ENTRY_IDX (2h in) and builds features from
     `candles[:ENTRY_IDX+1]` ONLY -- no look-ahead, no future volume, no
     future price. Every number the gates see existed at the entry minute.
  2. reconstructs the inputs `desk.meme.scorecard()` needs and scores the coin.
  3. walks a 7-rung gate ladder, simulating the desk's real exit rules
     (`desk.exits.evaluate`) on the real forward price path at each rung, so
     you can see exactly which gate buys how much win rate.
  4. buckets outcomes by meme score band, to show whether a higher score
     really means a higher win rate.
  5. sweeps each exit setting on the fully-gated set, using the `prof`
     override in `exits.evaluate` -- config is never mutated.

Honesty rules this script follows:

  * Every price is real GeckoTerminal OHLCV. Nothing is invented.
  * Features are point-in-time. Liquidity, mcap, buys/sells/buyers are a
    *current* snapshot from the pool attributes, not historical, because
    GeckoTerminal does not serve historical liquidity. They are used as a
    static pool-quality filter and are labelled as such in the output.
  * Holders, socials and top-wallet concentration are unknown in historical
    data. They are passed as None, which `meme.scorecard` already treats
    neutrally (half credit for holders, no credit for socials, no distribution
    penalty). That makes the meme score *conservative*, not flattering.
  * The deployer investigation (devcheck) cannot be replayed either, so the
    scorecard is run twice: once with dev unknown (dev_score 0) and once with
    dev assumed to pass (dev_score 55, legit True). The truth is between them.
  * Returns are capped at +1000% so one 400x candle cannot define the mean.
  * Fees 1% + slippage 2% are charged on both sides, same as the paper broker.

Usage:
    python scripts/backtest_gates.py fetch --pools 400
    python scripts/backtest_gates.py run [--cache backtest_cache.json]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from desk import exits, meme  # noqa: E402

GT = "https://api.geckoterminal.com/api/v2"
HDRS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 desk-backtest"}
SESSION = requests.Session()
SESSION.headers.update(HDRS)

CANDLE_MIN = 5
ENTRY_IDX = 24          # 24 * 5m = 2h of price history before we are allowed to buy
RET_CAP = 1000.0        # cap a single trade's return at +1000%
MIN_CANDLES = ENTRY_IDX + 12   # need at least an hour of forward path to be fair


# --------------------------------------------------------------------------
# phase 1: fetch
# --------------------------------------------------------------------------
# GeckoTerminal's free tier is ~30 requests/minute. Hammering it returns 429
# and every pool silently disappears, which would quietly bias the sample.
# One global limiter, and a patient 429 handler, keeps the fetch honest.
MIN_INTERVAL_S = 4.0
_last_call = [0.0]


def _throttle() -> None:
    wait = MIN_INTERVAL_S - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def get(url: str, params: dict | None = None, tries: int = 10) -> dict | None:
    for i in range(tries):
        _throttle()
        try:
            r = SESSION.get(url, params=params, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                back = 30.0
                print(f"    429 rate-limited, sleeping {back}s", flush=True)
                time.sleep(back)
                continue
            return None
        except requests.RequestException:
            time.sleep(2.0)
    return None


def _f(v) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def pool_mcap(pool: dict) -> float | None:
    """Market cap, falling back to FDV: GeckoTerminal returns null mcap for many
    young pools but always has fdv. Using 0 here would silently reject them."""
    return pool.get("mcap") or pool.get("fdv")


def collect_pools(want: int) -> list[dict]:
    """Gather pool rows from trending + new + top pools, deduped by address."""
    pools: dict[str, dict] = {}
    # new_pools first: the desk's scan window is max 48h old, and only a pool
    # whose candles start near its creation date has a point-in-time age that
    # the age gate can actually evaluate.
    for endpoint, pages in (("trending_pools", 6), ("new_pools", 2)):
        for page in range(1, pages + 1):
            body = get(f"{GT}/networks/solana/{endpoint}", {"page": page})
            rows = (body or {}).get("data") or []
            if not rows:
                break
            for row in rows:
                addr = (row.get("id") or "").replace("solana_", "")
                if not addr or addr in pools:
                    continue
                a = row.get("attributes") or {}
                vol = a.get("volume_usd") or {}
                tx = a.get("transactions") or {}
                chg = a.get("price_change_percentage") or {}
                pools[addr] = {
                    "address": addr,
                    "name": a.get("name"),
                    "created_at": a.get("pool_created_at"),
                    "liq": _f(a.get("reserve_in_usd")),
                    "mcap": _f(a.get("market_cap_usd")),
                    "fdv": _f(a.get("fdv_usd")),
                    "vol_h1": _f(vol.get("h1")),
                    "vol_h6": _f(vol.get("h6")),
                    "vol_h24": _f(vol.get("h24")),
                    "chg_h1": _f(chg.get("h1")),
                    "chg_h24": _f(chg.get("h24")),
                    "buys_h1": _f((tx.get("h1") or {}).get("buys")),
                    "sells_h1": _f((tx.get("h1") or {}).get("sells")),
                    "buyers_h1": _f((tx.get("h1") or {}).get("buyers")),
                    "sellers_h1": _f((tx.get("h1") or {}).get("sellers")),
                    "buys_h24": _f((tx.get("h24") or {}).get("buys")),
                    "sells_h24": _f((tx.get("h24") or {}).get("sells")),
                }
            if len(pools) >= want * 2:
                break
        if len(pools) >= want * 2:
            break
    return list(pools.values())


def fetch_ohlcv(addr: str, limit: int = 1000) -> list[dict]:
    body = get(f"{GT}/networks/solana/pools/{addr}/ohlcv/minute",
               {"aggregate": CANDLE_MIN, "limit": limit, "currency": "usd"})
    if not body:
        return []
    lst = (((body.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
    out = []
    for c in lst:
        if not c or len(c) < 6:
            continue
        o, h, l, cl, v = _f(c[1]), _f(c[2]), _f(c[3]), _f(c[4]), _f(c[5])
        if not cl or cl <= 0:
            continue
        out.append({"ts": int(c[0]), "open": o, "high": h, "low": l,
                    "close": cl, "vol": v or 0.0})
    out.sort(key=lambda c: c["ts"])
    return out


def cmd_fetch(args) -> int:
    existing: list[dict] = []
    have: set[str] = set()
    if args.append and Path(args.cache).exists():
        try:
            old = json.loads(Path(args.cache).read_text(encoding="utf-8"))
            existing = old.get("pools") or []
            have = {p["address"] for p in existing}
            print(f"Appending to {len(existing)} cached pools", flush=True)
        except Exception:
            pass
    print(f"Collecting pools (want ~{args.pools} new)...", flush=True)
    pools = [p for p in collect_pools(args.pools + len(have) // 2)
             if p["address"] not in have]
    print(f"  {len(pools)} unique pools found", flush=True)

    kept: list[dict] = list(existing)
    target = len(existing) + args.pools
    for i, p in enumerate(pools, 1):
        candles = fetch_ohlcv(p["address"])
        if len(candles) < MIN_CANDLES:
            continue
        p["candles"] = candles
        kept.append(p)
        if len(kept) % 5 == 0:
            Path(args.cache).write_text(json.dumps({
                "fetched_at": int(time.time()), "candle_minutes": CANDLE_MIN,
                "entry_idx": ENTRY_IDX, "partial": True, "pools": kept,
            }), encoding="utf-8")
        if len(kept) % 25 == 0:
            print(f"  {len(kept)} pools with usable history "
                  f"({i}/{len(pools)} scanned)", flush=True)
        if len(kept) >= target:
            break

    Path(args.cache).write_text(json.dumps({
        "fetched_at": int(time.time()),
        "candle_minutes": CANDLE_MIN,
        "entry_idx": ENTRY_IDX,
        "pools": kept,
    }), encoding="utf-8")
    print(f"\nSaved {len(kept)} pools -> {args.cache}", flush=True)
    return 0


# --------------------------------------------------------------------------
# phase 2: features (point-in-time only)
# --------------------------------------------------------------------------
def prefix(candles: list[dict]) -> list[float]:
    ps = [0.0]
    for c in candles:
        ps.append(ps[-1] + (c["vol"] or 0.0))
    return ps


def window_sum(ps: list[float], i: int, n: int) -> float:
    """Sum of vol over the n candles ending at index i (inclusive)."""
    lo = max(0, i - n + 1)
    return ps[i + 1] - ps[lo]


def features_at(candles: list[dict], ps: list[float], i: int,
                created_ts: float | None) -> dict:
    """Everything the gates are allowed to know at candle i. Nothing after i."""
    price = candles[i]["close"]
    ts = candles[i]["ts"]

    def ago(n: int) -> float | None:
        j = i - n
        if j < 0:
            return None
        p0 = candles[j]["close"]
        return (price / p0 - 1.0) if p0 else None

    vol_5m = candles[i]["vol"] or 0.0
    vol_30m = window_sum(ps, i, 6)
    vol_1h = window_sum(ps, i, 12)
    vol_6h = window_sum(ps, i, 72)
    vol_24h = window_sum(ps, i, 288)

    rets = []
    for k in range(max(1, i - 12), i + 1):
        p0, p1 = candles[k - 1]["close"], candles[k]["close"]
        if p0:
            rets.append(p1 / p0 - 1.0)
    volatility = statistics.pstdev(rets) if len(rets) > 1 else 0.0

    if created_ts:
        age_min = (ts - created_ts) / 60.0
    else:
        age_min = (ts - candles[0]["ts"]) / 60.0 + CANDLE_MIN

    # is volume still arriving? recent half-hour vs the average half-hour of the hour
    avg_half = vol_1h / 2.0
    vol_trend = (vol_30m / avg_half) if avg_half > 0 else 1.0

    # memecoin-tactic inputs (point-in-time only)
    lo20 = max(0, i + 1 - 20)
    high_20 = max((c["high"] for c in candles[lo20:i + 1] if c.get("high")),
                  default=None)
    chg_5m = None
    if i >= 1 and candles[i - 1].get("close"):
        chg_5m = price / candles[i - 1]["close"] - 1.0

    return {
        "price": price, "ts": ts, "high_20": high_20, "chg_5m": chg_5m,
        "vol_5m": vol_5m, "vol_30m": vol_30m, "vol_1h": vol_1h,
        "vol_6h": vol_6h, "vol_24h": vol_24h,
        "chg_30m": ago(6), "chg_1h": ago(12), "chg_2h": ago(24),
        "volatility": volatility, "age_min": age_min, "vol_trend": vol_trend,
    }


def split_symbol(name: str | None, addr: str) -> tuple[str, str]:
    if name and "/" in name:
        base = name.split("/")[0].strip()
        return base, base
    return (name or addr[:6]), (name or addr[:6])


def build_scorecard_inputs(pool: dict, f: dict, dev_score: float,
                           dev_legit: bool) -> tuple[dict, dict, dict, dict]:
    """Reconstruct (token, facts, ds, dev) for desk.meme.scorecard().

    Point-in-time where the candles allow it (volume, price moves, age).
    Snapshot where they do not (liquidity, mcap, buys/sells/buyers) -- labelled
    as a static pool-quality filter in the report.
    """
    sym, nm = split_symbol(pool.get("name"), pool["address"])
    token = {
        "symbol": sym, "name": nm, "mint": pool["address"],
        "liquidity_usd": pool.get("liq"),
        "mcap_usd": pool_mcap(pool),
        "volume_h1": f["vol_1h"],
        "buys_h1": pool.get("buys_h1"),
        "sells_h1": pool.get("sells_h1"),
        "buyers_h1": pool.get("buyers_h1"),
        "age_minutes": f["age_min"],
    }
    facts = {                       # unknown historically -> neutral in the scorecard
        "holder_count": None,
        "wallets_counted": 0,
        "top_wallet_pct": None,
        "top_10_pct": None,
    }
    ds = {
        "liquidity_usd": pool.get("liq"),
        "mcap_usd": pool_mcap(pool),
        "volume_h1": f["vol_1h"],
        "buys_h1": pool.get("buys_h1"),
        "sells_h1": pool.get("sells_h1"),
        "has_x": False, "has_telegram": False, "has_website": False,
        "boosts_active": False, "websites": [],
    }
    dev = {
        "dev_score": dev_score,
        "legit": dev_legit,
        "reasons": [],
        "sniper_pct": None,
        "holder_sample": 0,
        "holder_count": None,
    }
    return token, facts, ds, dev


# --------------------------------------------------------------------------
# phase 2: simulation
# --------------------------------------------------------------------------
def simulate(candles: list[dict], ps: list[float], i0: int, prof: dict) -> dict | None:
    """Enter at candle i0 close, walk forward with the desk's real exit rules."""
    entry = candles[i0]["close"]
    if not entry or entry <= 0:
        return None
    fee = config.PAPER["fee_pct"]
    slip = config.PAPER["slippage_pct"]

    pos = {"entry": entry, "opened": candles[i0]["ts"]}
    exits.init_state(pos, "scalp")
    # force the swept profile's stop and tag onto the position
    pos["stop_pct"] = prof["stop_pct"]
    pos["stop_price"] = entry * (1 - prof["stop_pct"])

    # Buy 1 unit of notional: qty tokens at `entry`, paying fee+slippage up front.
    # NOTE: this must be done in *units*, not dollars. The older backtest compared
    # the raw token price to a $1.03 cost basis, so a coin that went +2% from
    # $0.0005 to $0.00051 was recorded as -99.95%. Every return here is
    # (cash_out / cash_in - 1), which is the only thing that means anything.
    qty = 1.0 / entry
    cost_basis = 1.0 * (1 + fee + slip)
    remaining, cash = 1.0, 0.0
    reasons: list[str] = []
    last_price = entry

    for j in range(i0 + 1, len(candles)):
        c = candles[j]
        price = c["close"]
        last_price = price
        q = {
            "price_usd": price,
            "volume_h6": window_sum(ps, j, 72),
            "volume_h24": max(window_sum(ps, j, 288), window_sum(ps, j, 72)),
        }
        sig = exits.evaluate(pos, q, now=c["ts"], prof=prof)
        if not sig:
            continue
        frac, why = sig
        sell_qty = remaining * qty * frac
        cash += sell_qty * price * (1 - fee - slip)
        remaining -= remaining * frac
        reasons.append(why)
        if remaining <= 1e-9:
            break

    if remaining > 1e-9:
        cash += remaining * qty * last_price * (1 - fee - slip)
        reasons.append("data end close")

    pnl = cash - cost_basis
    ret = pnl / cost_basis * 100.0
    ret = max(-100.0, min(RET_CAP, ret))
    return {"pnl": pnl, "ret_pct": ret, "reasons": reasons,
            "entry": entry, "exit_px": last_price}


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0, "win_rate": None, "mean_ret": None,
                "median_ret": None, "profit_factor": None, "rug_rate": None}
    rets = [r["ret_pct"] for r in rows]
    wins = sum(1 for x in rets if x > 0)
    gross_w = sum(x for x in rets if x > 0)
    gross_l = -sum(x for x in rets if x <= 0)
    rugs = sum(1 for x in rets if x <= -90.0)
    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4),
        "mean_ret": round(sum(rets) / n, 2),
        "median_ret": round(statistics.median(rets), 2),
        "profit_factor": round(gross_w / gross_l, 3) if gross_l else None,
        "rug_rate": round(rugs / n, 4),
        "best": round(max(rets), 1),
        "worst": round(min(rets), 1),
    }


# --------------------------------------------------------------------------
# phase 2: gates
# --------------------------------------------------------------------------
def scan_ok(pool: dict, f: dict, sc: dict) -> tuple[bool, str]:
    S = config.SCAN
    if (pool.get("liq") or 0) < S["min_liquidity_usd"]:
        return False, "liq"
    mcap = pool_mcap(pool)
    if mcap is not None and mcap < S["min_mcap_usd"]:
        return False, "mcap_low"
    if mcap is not None and mcap > S["max_mcap_usd"]:
        return False, "mcap_high"
    if f["vol_1h"] < S["min_volume_h1_usd"]:
        return False, "vol"
    if f["chg_1h"] is not None and f["chg_1h"] * 100.0 > S["max_h1_change_pct"]:
        return False, "chasing"
    if f["age_min"] < S["min_age_minutes"]:
        return False, "too_new"
    if f["age_min"] > S["max_age_hours"] * 60:
        return False, "too_old"
    return True, ""


def flow_ok(pool: dict, f: dict, sc: dict) -> tuple[bool, str]:
    S = config.SCAN
    buys, sells = pool.get("buys_h1") or 0, pool.get("sells_h1") or 0
    if buys + sells < S["min_trades_h1"]:
        return False, "few_trades"
    if buys > 0 and sells / buys < S["min_sells_ratio_h1"]:
        return False, "no_sells"
    return True, ""


def organic_ok(pool: dict, f: dict, sc: dict) -> tuple[bool, str]:
    buys, buyers = pool.get("buys_h1") or 0, pool.get("buyers_h1") or 0
    if buys and buyers and buys / buyers > 4.0:
        return False, "wash"
    return True, ""


def meme_ok(pool: dict, f: dict, sc: dict) -> tuple[bool, str]:
    if sc["total"] < config.MEME["bands"]["watch"]:
        return False, "meme_score"
    return True, ""


def community_ok(pool: dict, f: dict, sc: dict) -> tuple[bool, str]:
    if sc["parts"]["community"] < config.MEME["min_community_score"]:
        return False, "no_community"
    return True, ""


def voltrend_ok(pool: dict, f: dict, sc: dict) -> tuple[bool, str]:
    if f["vol_trend"] < 0.8:
        return False, "vol_fade"
    return True, ""


def pullback_ok(pool: dict, f: dict, sc: dict) -> tuple[bool, str]:
    """Meme-guide rule B: do not buy the top of the recent range."""
    hi = f.get("high_20")
    if hi and f.get("price") and f["price"] > hi * (1.0 - config.MEME.get("pullback_min_drop", 0.10)):
        return False, "at_high"
    return True, ""


def flash_ok(pool: dict, f: dict, sc: dict) -> tuple[bool, str]:
    """Meme-guide rule C: skip entries right after a vertical candle."""
    thr = config.MEME.get("flash_pump_pct", 0.25)
    if f.get("chg_5m") is not None and f["chg_5m"] > thr:
        return False, "flash_pump"
    return True, ""


# Cumulative: rung N applies gates 1..N. That is the only way the ladder means
# "what did this gate add on top of the ones before it".
GATES = [scan_ok, flow_ok, organic_ok, meme_ok, community_ok, voltrend_ok,
         pullback_ok, flash_ok]
LADDER = [
    ("0. no gates (old baseline)", []),
    ("1. + SCAN floors", GATES[:1]),
    ("2. + trades & sell ratio", GATES[:2]),
    ("3. + organic buyers", GATES[:3]),
    ("4. + meme not-bad (>=50)", GATES[:4]),
    ("5. + community >= 35", GATES[:5]),
    ("6. + volume still arriving", GATES[:6]),
    ("7. + not at 20-bar high (pullback)", GATES[:7]),
    ("8. + no flash pump (>25%/5m)", GATES[:8]),
]


def passes(gates, pool, f, sc) -> tuple[bool, str]:
    for g in gates:
        ok, why = g(pool, f, sc)
        if not ok:
            return False, why
    return True, ""


def cmd_run(args) -> int:
    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    pools = cache["pools"]
    i0 = cache.get("entry_idx", ENTRY_IDX)
    print(f"Loaded {len(pools)} pools from {args.cache} "
          f"(entry at candle {i0} = {i0 * CANDLE_MIN}m)\n", flush=True)

    # --- build the per-pool record once ---
    recs = []
    for p in pools:
        candles = p["candles"]
        created = _created_ts(p)
        idx = entry_index(candles, created, i0)
        if idx is None:
            continue
        ps = prefix(candles)
        f = features_at(candles, ps, idx, created)
        sym, nm = split_symbol(p.get("name"), p["address"])
        # two dev assumptions: unknown (pessimistic) and passing (optimistic)
        sc_unk = meme.scorecard(*build_scorecard_inputs(p, f, 0.0, False))
        sc_ok = meme.scorecard(*build_scorecard_inputs(p, f, 55.0, True))
        recs.append({"pool": p, "candles": candles, "ps": ps, "f": f, "idx": idx,
                     "sym": sym, "sc_unk": sc_unk, "sc_ok": sc_ok})

    print(f"{len(recs)} pools have enough forward path to trade\n", flush=True)

    # --- gate ladder, run under both dev assumptions ---
    ladder_out = {}
    for label, gates in LADDER:
        for dev_name, key in (("dev unknown", "sc_unk"), ("dev passes", "sc_ok")):
            rows, rej = [], {}
            for r in recs:
                ok, why = passes(gates, r["pool"], r["f"], r[key])
                if not ok:
                    rej[why] = rej.get(why, 0) + 1
                    continue
                t = simulate(r["candles"], r["ps"], r["idx"], exits.profile("scalp"))
                if t:
                    rows.append(t)
            ladder_out.setdefault(label, {})[dev_name] = {
                **summarize(rows), "rejected": dict(sorted(rej.items(), key=lambda kv: -kv[1]))}

    # --- meme score bands, on the rung-3 set (scan+flow+organic) ---
    bands = [(0, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 101)]
    band_out = {}
    for dev_name, key in (("dev unknown", "sc_unk"), ("dev passes", "sc_ok")):
        for lo, hi in bands:
            rows = []
            for r in recs:
                if not passes(GATES[:3], r["pool"], r["f"], r[key])[0]:
                    continue
                if not (lo <= r[key]["total"] < hi):
                    continue
                t = simulate(r["candles"], r["ps"], r["idx"], exits.profile("scalp"))
                if t:
                    rows.append(t)
            band_out.setdefault(dev_name, {})[f"{lo}-{hi-1}"] = summarize(rows)

    # --- settings sweep on the fully-gated set ---
    # The gated set uses the "dev passes" scorecard: in live trading devcheck
    # runs before the meme seat, so a coin only reaches the scorecard if the
    # deployer investigation did not already veto it. Scoring it as if the dev
    # were unknown would kill every coin and tell us nothing.
    gated = []
    for r in recs:
        if passes(GATES, r["pool"], r["f"], r["sc_ok"])[0]:
            gated.append(r)
    print(f"{len(gated)} pools survive all 6 gates (dev passes)\n", flush=True)

    base = exits.profile("scalp")
    sweep_space = {
        "stop_pct": [0.15, 0.20, 0.25, 0.30, 0.40, 0.60, 0.99],
        "tp1_at": [1.2, 1.3, 1.5, 2.0, 3.0],
        "tp2_at": [1.5, 2.0, 3.0, 5.0],
        "trail_pct": [0.10, 0.15, 0.20, 0.30, 0.50],
        "timeout_hours": [1.0, 2.0, 3.0, 6.0, 12.0],
        "max_hold_hours": [4.0, 8.0, 24.0, 48.0],
        "tp1_frac": [0.0, 0.33, 0.5, 0.75],
    }
    # Exit rules act on whatever you bought, so the sweep is run on BOTH:
    #   "all"   = every pool in the cache (the statistically meaningful set)
    #   "gated" = only what the desk's gates would have let through
    sweep_out = {}
    for scope_name, universe in (("all", recs), ("gated", gated)):
        sweep_out[scope_name] = {}
        for param, values in sweep_space.items():
            table = []
            for v in values:
                prof = dict(base)
                prof[param] = v
                if param == "tp2_at" and v <= prof["tp1_at"]:
                    prof["tp2_at"] = prof["tp1_at"] + 0.1
                rows = []
                for r in universe:
                    t = simulate(r["candles"], r["ps"], r["idx"], prof)
                    if t:
                        rows.append(t)
                table.append({"value": v, **summarize(rows)})
            sweep_out[scope_name][param] = table

    # ---- named combinations: single-parameter sweeps hide interactions ----
    COMBOS = [
        ("current (stop 20%, hold 4h)", {}),
        ("no stop, hold 4h", {"stop_pct": 0.99}),
        ("no stop, hold 12h", {"stop_pct": 0.99, "max_hold_hours": 12, "timeout_hours": 12}),
        ("no stop, hold 48h", {"stop_pct": 0.99, "max_hold_hours": 48, "timeout_hours": 48}),
        ("stop 40%, hold 48h", {"stop_pct": 0.40, "max_hold_hours": 48, "timeout_hours": 48}),
        ("stop 40%, hold to data end", {"stop_pct": 0.40, "max_hold_hours": 10000,
                                        "timeout_hours": 10000}),
        ("stop 25%, tp1 1.3x, hold 12h", {"stop_pct": 0.25, "tp1_at": 1.3,
                                          "max_hold_hours": 12, "timeout_hours": 12}),
    ]
    combos_out = {}
    for scope_name, universe in (("all", recs), ("gated", gated)):
        combos_out[scope_name] = []
        for label, over in COMBOS:
            prof = dict(base)
            prof.update(over)
            rows = [t for t in (simulate(r["candles"], r["ps"], r["idx"], prof)
                                for r in universe) if t]
            combos_out[scope_name].append({"label": label, "overrides": over,
                                           **summarize(rows)})

    result = {
        "generated_at": int(time.time()),
        "cache": args.cache,
        "pools_in_cache": len(pools),
        "pools_tradeable": len(recs),
        "pools_fully_gated": len(gated),
        "entry_minutes": i0 * CANDLE_MIN,
        "ladder": ladder_out,
        "score_bands": band_out,
        "settings": sweep_out,
        "combos": combos_out,
        "baseline_profile": base,
    }
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")

    # ---------------- print ----------------
    def line(s=""):
        print(s, flush=True)

    line("=" * 78)
    line("  GATE LADDER  -- win rate as each gate is switched on")
    line("=" * 78)
    line(f"  {'rung':<30}{'trades':>7}{'win%':>8}{'mean%':>8}{'med%':>8}{'PF':>7}{'rug%':>7}")
    for label, _ in LADDER:
        for dev_name in ("dev unknown", "dev passes"):
            s = ladder_out[label][dev_name]
            if not s["trades"]:
                line(f"  {label:<30}{'--':>7}  (no trades)   "
                     f"rejects={s.get('rejected')}")
                continue
            line(f"  {label:<30}{s['trades']:>7}{s['win_rate']*100:>7.1f}%"
                 f"{s['mean_ret']:>8.1f}{s['median_ret']:>8.1f}"
                 f"{s['profit_factor'] if s['profit_factor'] is not None else 0:>7.2f}"
                 f"{s['rug_rate']*100:>6.0f}%   [{dev_name}]")
            if s.get("rejected"):
                line(f"  {'':<30}  dropped: {s['rejected']}")
        line()

    line("=" * 78)
    line("  MEME SCORE BANDS  -- does a higher score really win more?")
    line("=" * 78)
    for dev_name in ("dev unknown", "dev passes"):
        line(f"  [{dev_name}]")
        line(f"  {'score':<10}{'trades':>7}{'win%':>8}{'mean%':>8}{'med%':>8}{'PF':>7}")
        for lo, hi in bands:
            s = band_out[dev_name][f"{lo}-{hi-1}"]
            if not s["trades"]:
                line(f"  {f'{lo}-{hi-1}':<10}{'0':>7}      --")
                continue
            line(f"  {f'{lo}-{hi-1}':<10}{s['trades']:>7}{s['win_rate']*100:>7.1f}%"
                 f"{s['mean_ret']:>8.1f}{s['median_ret']:>8.1f}"
                 f"{s['profit_factor'] if s['profit_factor'] is not None else 0:>7.2f}")
        line()

    line("=" * 78)
    line("  SETTINGS SWEEP  -- best value per exit parameter (fully gated set)")
    line("=" * 78)
    for scope_name, sc_table in sweep_out.items():
        line(f"  --- scope: {scope_name} "
             f"({'%d pools' % (len(recs) if scope_name == 'all' else len(gated))}) ---")
        for param, table in sc_table.items():
            _print_sweep(line, param, table)
        line()


    line("=" * 78)
    line("  COMBINATIONS  -- single-parameter sweeps hide interactions")
    line("=" * 78)
    for scope_name, rows in combos_out.items():
        line(f"  --- scope: {scope_name} ---")
        for row in rows:
            if not row["trades"]:
                line(f"    {row['label']:<34} (no trades)")
                continue
            line(f"    {row['label']:<34} n={row['trades']:<4} "
                 f"win {row['win_rate']*100:>5.1f}%  mean {row['mean_ret']:>7.1f}%  "
                 f"med {row['median_ret']:>7.1f}%  PF {row['profit_factor']}")
        line()

    line(f"Saved full results -> {args.out}")
    return 0


def entry_index(candles: list[dict], created_ts: float | None,
                default: int) -> int | None:
    """The candle where the desk could first have bought: 2h after the pool was
    created. Anchoring to creation (not to the start of the OHLCV window) is what
    makes the age gate meaningful -- otherwise a pool born 11h ago is evaluated
    as if it were 3 days old."""
    if not created_ts:
        idx = default
        return idx if idx + 12 < len(candles) else None
    start = 0
    for i, c in enumerate(candles):
        if c["ts"] >= created_ts:
            start = i
            break
    idx = start + ENTRY_IDX
    return idx if idx + 12 < len(candles) else None


def _print_sweep(line, param: str, table: list[dict]) -> None:
    line(f"  {param}:")
    for row in table:
        if not row["trades"]:
            continue
        line(f"    {str(row['value']):<6} n={row['trades']:<4} "
             f"win {row['win_rate']*100:>5.1f}%  mean {row['mean_ret']:>7.1f}%  "
             f"PF {row['profit_factor'] if row['profit_factor'] is not None else 0:>5.2f}")
    best = max((r for r in table if r["trades"]), key=lambda r: r["mean_ret"], default=None)
    if best:
        line(f"    -> best by mean return: {best['value']} "
             f"(win {best['win_rate']*100:.1f}%, mean {best['mean_ret']:+.1f}%)")


def _created_ts(pool: dict) -> float | None:
    ca = pool.get("created_at")
    if not ca:
        return None
    try:
        return datetime.fromisoformat(ca.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="pull pools + OHLCV from GeckoTerminal")
    pf.add_argument("--pools", type=int, default=400)
    pf.add_argument("--cache", default="backtest_cache.json")
    pf.add_argument("--append", action="store_true",
                    help="add to an existing cache instead of replacing it")
    pf.set_defaults(func=cmd_fetch)

    pr = sub.add_parser("run", help="replay the cache offline")
    pr.add_argument("--cache", default="backtest_cache.json")
    pr.add_argument("--out", default="backtest_gates.json")
    pr.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
