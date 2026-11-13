"""Replay EXIT RULES over the old desk's recorded price ticks (read-only). HYPOTHETICAL entries:
the old desk never filled a trade, so entry = first (or Nth) tick of each memecoin it polled.
This measures exit-rule behaviour on a small, biased sample. It is not a profit forecast.

usage: python scripts/replay_exits.py [/workspace/desk/desk.db]
"""
import sqlite3, sys, tempfile, json, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk import exits
from desk.broker import PaperBroker

DB = sys.argv[1] if len(sys.argv) > 1 else "/workspace/desk/desk.db"


def load():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute("select mint, ts, price_usd from prices where price_usd>0 order by mint, ts").fetchall()
    by = {}
    for m, ts, p in rows:
        by.setdefault(m, []).append((ts, p))
    # memecoins only: drop stables/stocks (price >= 0.5) and coins with too few ticks
    return {m: v for m, v in by.items() if len(v) >= 15 and max(p for _, p in v) < 0.5}


def legacy_new_desk(pos, q, now):  # original solana-desk rules (stop 35%, half at 1.5x, 24h)
    p = pos
    if q is None: return (1.0, "noquote")
    price = q["price_usd"]; p["last"] = price
    if price <= p["entry"] * 0.65: return (1.0, "stop")
    if not p.get("half_taken") and price >= p["entry"] * 1.5:
        p["half_taken"] = True; return (0.5, "half")
    if (now - p["opened"]) / 60 >= 24 * 60: return (1.0, "max")


def old_desk(pos, q, now):  # old desk: stop 6%, trail arms +20% trails 12% from peak, timeout 3h if < +5%
    p = pos; price = q["price_usd"]; p["last"] = price
    p["peak"] = max(p.get("peak", p["entry"]), price)
    if price <= p["entry"] * 0.94: return (1.0, "stop")
    if not p.get("trail") and price >= p["entry"] * 1.20: p["trail"] = True
    if p.get("trail") and price <= p["peak"] * 0.88: return (1.0, "trail")
    if (now - p["opened"]) >= 3 * 3600 and price < p["entry"] * 1.05: return (1.0, "timeout")


def merged(over):
    def f(pos, q, now):
        return exits.evaluate(pos, q, now, exits.profile(pos.get("horizon"), over))
    return f


def run(ticks_by_coin, rule, entry_idx_frac, horizon="scalp", stop_for_init=None):
    res = []
    for m, tk in ticks_by_coin.items():
        i0 = int(len(tk) * entry_idx_frac)
        d = tempfile.mkdtemp(); b = PaperBroker(Path(d) / "l.json")
        ts0, p0 = tk[i0]
        b.buy(m, m[:5], 50, p0, horizon=horizon)
        pos = b.s["positions"][m]; pos["opened"] = ts0
        if stop_for_init: pos["stop_price"] = pos["entry"] * (1 - stop_for_init)
        for ts, p in tk[i0 + 1:]:
            sig = rule(pos, {"price_usd": p, "volume_h6": 0, "volume_h24": 0}, ts)
            if sig:
                fr, why = sig
                b.sell(m, fr, p, why)
                if m not in b.s["positions"]: break
        if m in b.s["positions"]:           # still open at end of data: mark to last tick (conservative: close)
            b.sell(m, 1.0, tk[-1][1], "end_of_data")
        res.append(b.s["closed"][-1]["pnl_usd"])
    return res


def summ(name, r):
    n = len(r); w = sum(1 for x in r if x > 0)
    return {"rule": name, "coins": n, "win%": round(100 * w / n, 1), "total_pnl_usd(50/ticket)": round(sum(r), 2),
            "avg": round(statistics.mean(r), 2), "worst": round(min(r), 2), "best": round(max(r), 2)}


if __name__ == "__main__":
    data = load()
    print(f"coins with >=15 ticks and price<0.5: {len(data)}; ticks total {sum(len(v) for v in data.values())}")
    out = []
    for frac in (0.0, 0.33):
        print(f"\n--- entry at {int(frac*100)}% of each coin's recorded ticks ---")
        rules = [("new desk original (stop35, half@1.5x)", legacy_new_desk, None),
                 ("old desk (stop6, trail20/12, timeout3h)", old_desk, None)]
        for nm, ov in [("MERGED scalp default (stop20, TP1 1.5x 33%, TP2 2x, trail20)", {}),
                       ("merged stop 10", {"stop_pct": 0.10}), ("merged stop 15", {"stop_pct": 0.15}),
                       ("merged stop 30", {"stop_pct": 0.30}),
                       ("merged trail 12", {"trail_pct": 0.12}), ("merged trail 30", {"trail_pct": 0.30}),
                       ("merged tp1 1.3x", {"tp1_at": 1.3})]:
            rules.append((nm, merged(ov), ov.get("stop_pct")))
        for nm, rule, st in rules:
            s = summ(nm, run(data, rule, frac, stop_for_init=st)); out.append({"entry_frac": frac, **s})
            print(json.dumps(s))
    Path(__file__).with_name("replay_exits_result.json").write_text(json.dumps(out, indent=1))
