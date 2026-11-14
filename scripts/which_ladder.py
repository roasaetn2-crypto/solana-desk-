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

The same information is in GET /api/state under "exits", so if you have no
clone you can read it straight off the running desk instead.

Usage:
    python3 scripts/which_ladder.py          # human readable
    python3 scripts/which_ladder.py --json   # machine readable
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import meme_tactics  # noqa: E402

SOURCE_LABEL = {
    "env": "env BRAIN_EXIT_LADDER",
    "config": "config.py MEME['exit_ladder']",
    "default": "default (nothing configured)",
}


def resolve() -> dict:
    """The resolver's own answer, with the source spelled out for humans."""
    info = meme_tactics.ladder_info()
    return {**info, "source": SOURCE_LABEL.get(info["source"], info["source"])}


def main() -> int:
    r = resolve()
    if "--json" in sys.argv[1:]:
        print(json.dumps(r, indent=2))
        return 0 if r["valid"] else 1

    a = r["ladder"]
    print(f"repo        : {ROOT}")
    print(f"requested   : {r['requested']}   (from {r['source']})")
    if r["fallback_used"]:
        print(f"WARNING     : '{r['requested']}' is not a known ladder -> falling back to "
              f"'{r['name']}'")
    print(f"known       : {', '.join(r['known'])}")
    print()
    print(f"active ladder: {r['name']}")
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
