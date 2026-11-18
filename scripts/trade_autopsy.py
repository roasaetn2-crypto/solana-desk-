#!/usr/bin/env python3
"""Why is my win rate low?  Group CLOSED trades by exit reason and say which setting causes it.

    python scripts/trade_autopsy.py            # reads the desk's own paper ledger
    python scripts/trade_autopsy.py PATH.json  # or any ledger file

Reads only. The closed-trade rows hold: symbol, pnl_usd, reason, closed (unix time).
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# reason family -> (what it means, where to change it)
HINTS = {
    "stop": ("hit the stop", "Control > Exits: stop loss (the ladder sets 30%)"),
    "breakeven stop": ("stopped at entry after TP1 (fees make it a small loss)", "normal after a TP1 win"),
    "timeout": ("sat too long without gaining enough, so it was closed at a loss after fees",
                "Control > Exits: scalp timeout hours / min gain (raise the hours or lower the gain)"),
    "volume decay": ("closed because volume faded", "Control > Exits: Volume decay exit (lower the ratio, e.g. 0.05)"),
    "no quote": ("closed because the price could not be read", "Quote misses before close (raise 3 -> 6) and a better price feed"),
    "bad quote": ("closed on a price it did not trust", "Quote misses before close (raise 3 -> 6)"),
    "max hold": ("hit the maximum hold time", "Control > Exits: max hold hours"),
    "TP": ("take profit", "working as intended"),
    "trail": ("trailing stop after TP1", "working as intended"),
    "runner": ("runner time cap after TP1", "working as intended"),
}


def family(reason: str) -> str:
    r = (reason or "unknown").strip()
    for key in ("breakeven stop", "volume decay", "no quote", "bad quote", "max hold", "timeout",
                "runner", "trail"):
        if r.lower().startswith(key):
            return key
    if r.lower().startswith("stop"):
        return "stop"
    if r.upper().startswith("TP"):
        return "TP"
    return re.sub(r"[-+]?\d+(\.\d+)?%?", "#", r)[:28]


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        path = Path(argv[1])
    else:
        from desk.store import data_file
        path = Path(data_file("paper_ledger" + ".json"))
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"could not read {path}: {e}")
        return 1
    closed = [c for c in ledger.get("closed") or [] if c.get("pnl_usd") is not None]
    if not closed:
        print("no closed trades yet")
        return 0
    wins = [c for c in closed if c["pnl_usd"] > 0]
    losses = [c for c in closed if c["pnl_usd"] <= 0]
    net = sum(c["pnl_usd"] for c in closed)
    aw = sum(c["pnl_usd"] for c in wins) / len(wins) if wins else 0.0
    al = sum(c["pnl_usd"] for c in losses) / len(losses) if losses else 0.0
    print(f"{len(closed)} closed | {len(wins)} wins ({len(wins) / len(closed) * 100:.0f}%) | net ${net:+.2f}")
    print(f"avg win ${aw:+.2f} | avg loss ${al:+.2f}\n")
    groups: dict[str, list[float]] = defaultdict(list)
    for c in closed:
        groups[family(c.get("reason"))].append(c["pnl_usd"])
    print(f"{'exit reason':16} {'n':>3} {'wins':>4} {'net $':>7}")
    ranked = sorted(groups.items(), key=lambda kv: sum(kv[1]))
    for fam, pnls in ranked:
        print(f"{fam:16} {len(pnls):>3} {sum(1 for x in pnls if x > 0):>4} {sum(pnls):>+7.2f}")
    worst, pnls = ranked[0]
    print(f"\nBiggest loss source: '{worst}' ({len(pnls)} trades, ${sum(pnls):+.2f})")
    if worst in HINTS:
        meaning, fix = HINTS[worst]
        print(f"  meaning: {meaning}\n  change:  {fix}")
    if len(closed) < 30:
        print(f"\nOnly {len(closed)} trades: a hint, not proof. Wait for ~30.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
