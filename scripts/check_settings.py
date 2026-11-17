#!/usr/bin/env python3
"""Compare the LIVE desk's settings with the 'strategy' preset and say which ones hurt results.

Reads the JSON from GET /api/settings on stdin (termux/check-settings.sh does the fetching).
Prints only what differs, worst first, so it fits on a phone screen.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from desk.settings import PRESETS  # noqa: E402

# Why each one matters, worst first. Only settings the strategy preset sets can be judged.
WHY = [
    ("scalp.enabled", "fast scalp lane: overrides your exits with its own tiny stop/targets"),
    ("meme.pullback_entry", "the dip entry is the only entry your repo's tests found profitable"),
    ("exits.ladder", "exit ladder (fast = more wins, patient = more per trade)"),
    ("risk.auto_continue_after_daily_stop", "keeps trading after the daily stop"),
    ("risk.max_daily_loss_pct", "daily loss stop"),
    ("risk.max_risk_per_trade", "risk per trade"),
    ("risk.max_open_positions", "too many slots multiplies losses"),
    ("signal.require_bullish", "buys without a bullish signal"),
    ("analyst.valid_score", "analyst bar for entries"),
    ("signal.min_confidence", "signal confidence bar"),
    ("signal.min_win_probability", "win-probability bar"),
    ("scan.min_liquidity_usd", "thin pools get drained; rugs"),
    ("swing.min_liquidity_usd", "swing lane's own liquidity floor"),
    ("scan.min_volume_h1_usd", "dead pools"),
    ("scan.min_trades_h1", "wash-traded pools"),
    ("scan.min_age_minutes", "pullback needs ~100 min of candles to be checked"),
    ("scan.min_mcap_usd", "sub-$100k lost 16 trades at 6% wins in the journal"),
    ("scan.max_mcap_usd", "no profitable results above $2M"),
]


def main() -> int:
    try:
        snap = json.load(sys.stdin)
    except Exception:
        print("could not read the settings (is the desk running, and is ~/.desk_key right?)")
        return 1
    cur = {}
    for items in (snap.get("groups") or {}).values():
        for it in items:
            cur[it["key"]] = it.get("current")
    want = PRESETS["strategy"]["values"]
    bad, shown = [], set()
    for key, why in WHY:
        if key in want and key in cur and cur[key] != want[key]:
            bad.append((key, cur[key], want[key], why)); shown.add(key)
    rest = [(k, cur[k], v) for k, v in want.items() if k in cur and cur[k] != v and k not in shown]
    if not bad and not rest:
        print("All checked settings already match the Strategy preset.")
        return 0
    print(f"{len(bad)} important + {len(rest)} minor settings differ from the Strategy preset\n")
    for k, now, tgt, why in bad:
        print(f"* {k}: {now} -> {tgt}\n    {why}")
    if rest:
        print("\nminor:")
        for k, now, tgt in rest:
            print(f"  {k}: {now} -> {tgt}")
    print("\nfix all at once:  bash termux/apply-preset.sh strategy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
