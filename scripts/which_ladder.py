#!/usr/bin/env python3
"""Print the exit ladder the desk will actually use, and where that choice came from.

The ladder is resolved in this order (first one that is set wins):

    1. env  BRAIN_EXIT_LADDER
    2. config.py  MEME["exit_ladder"]
    3. default  "fast"

That order is why "I changed config.py and nothing happened" is such a common
surprise: a stale BRAIN_EXIT_LADDER in the shell or in the host's env vars
silently outranks the file. This script shows which one won, so you never have
to guess.

It also works from any directory -- it puts the repo root on sys.path itself --
so it is safe to run from ~ in Termux:

    python3 ~/solana-desk-/scripts/which_ladder.py

Usage:
    python3 scripts/which_ladder.py          # human readable
    python3 scripts/which_ladder.py --json   # machine readable
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import MEME  # noqa: E402
from desk import meme_tactics  # noqa: E402


def resolve() -> dict:
    """Mirror active_ladder() but keep the provenance, which active_ladder drops."""
    env_raw = os.getenv("BRAIN_EXIT_LADDER", "")
    cfg_raw = MEME.get("exit_ladder", "")
    if str(env_raw).strip():
        source, raw = "env BRAIN_EXIT_LADDER", env_raw
    elif str(cfg_raw).strip():
        source, raw = "config.py MEME['exit_ladder']", cfg_raw
    else:
        source, raw = "default (nothing configured)", meme_tactics.DEFAULT_LADDER

    name = str(raw).strip().lower()
    known = sorted(meme_tactics.LADDERS)
    valid = name in meme_tactics.LADDERS
    return {
        "requested": name,
        "source": source,
        "valid": valid,
        "known": known,
        "active": meme_tactics.active_ladder(),
        "fallback_used": not valid,
    }


def main() -> int:
    r = resolve()
    if "--json" in sys.argv[1:]:
        print(json.dumps(r, indent=2))
        return 0 if r["valid"] else 1

    a = r["active"]
    print(f"repo        : {ROOT}")
    print(f"requested   : {r['requested']}   (from {r['source']})")
    if r["fallback_used"]:
        print(f"WARNING     : '{r['requested']}' is not a known ladder -> falling back to "
              f"'{meme_tactics.DEFAULT_LADDER}'")
    print(f"known       : {', '.join(r['known'])}")
    print()
    print("active ladder")
    print(f"  stop      : -{a['stop_pct']:.0%}")
    print(f"  TP1       : +{a['tp1_at'] - 1:.0%}  sell {a['tp1_frac']:.0%}")
    tp2 = a.get("tp2_at")
    if tp2 is None or tp2 >= 1e8:
        print("  TP2       : none (single take-profit)")
    else:
        print(f"  TP2       : +{tp2 - 1:.0%}  sell {a['tp2_frac']:.0%}")
    print(f"  trail     : {a['trail_pct']:.0%} off the high")
    return 0 if r["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
