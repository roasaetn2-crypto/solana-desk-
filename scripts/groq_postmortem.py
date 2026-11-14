#!/usr/bin/env python3
"""Run the Groq post-mortem on your own closed trades.

Reads data/journal.jsonl, joins each exit to the decision that opened it, and
asks Groq what the losers share that the winners do not. Offline and safe: it
cannot touch an order.

    python scripts/groq_postmortem.py            # run if new trades exist
    python scripts/groq_postmortem.py --force    # re-run regardless
    python scripts/groq_postmortem.py --status   # what the dashboard sees
    python scripts/groq_postmortem.py --cases    # just the joined rows, no API call

Exit code 0 when a report exists (fresh or cached), 1 when it could not run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import groq_postmortem as pm  # noqa: E402
from desk.ops import load_env  # noqa: E402


DASH = "\u2014"


def _pct(x):
    return DASH if x is None else f"{x * 100:.1f}%"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Groq post-mortem over closed trades")
    ap.add_argument("--force", action="store_true", help="re-run even if the trade count is unchanged")
    ap.add_argument("--status", action="store_true", help="print the dashboard contract and exit")
    ap.add_argument("--cases", action="store_true", help="print the joined trade rows, no API call")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    ap.add_argument("--journal", default=None, help="path to journal.jsonl")
    args = ap.parse_args(argv)

    load_env()

    if args.cases:
        cases = pm.build_cases(args.journal)
        print(json.dumps(cases, indent=2, default=str))
        print(f"\n{len(cases)} closed trade(s) in the journal.", file=sys.stderr)
        return 0 if cases else 1

    if args.status:
        print(json.dumps(pm.status(args.journal), indent=2, default=str))
        return 0

    result = pm.run(force=args.force, path=args.journal,
                    log=lambda s: print(s, file=sys.stderr))

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1

    if not result.get("ok"):
        print(f"post-mortem could not run: {result.get('reason')}")
        print(f"  closed trades: {result.get('n', 0)}  "
              f"(wins {result.get('wins', 0)} / losses {result.get('losses', 0)})")
        if result.get("n", 0) < pm.MIN_TRADES:
            print(f"  need {pm.MIN_TRADES} before a model is allowed to look for patterns.")
        return 1

    cached = " (cached)" if result.get("cached") else ""
    print(f"Groq post-mortem{cached} \u2014 {result['n']} closed trades, "
          f"{result['wins']}W / {result['losses']}L "
          f"({_pct(result['wins'] / result['n'] if result['n'] else None)})")
    model = result.get("model") or DASH
    print(f"model: {model}   ran: {result.get('at')}")
    print()

    print("VERDICT")
    print("  " + (result.get("verdict") or DASH))
    if result.get("null_warning"):
        print(f"  nulls: {result['null_warning']}")
    print()

    for title, key in (("WHAT LOSERS SHARE", "loser_traits"),
                       ("WHAT WINNERS SHARE", "winner_traits")):
        rows = result.get(key) or []
        print(title)
        if not rows:
            print("  (nothing reported)")
        for r in rows:
            conf = f" [{r['confidence']}]" if r.get("confidence") else ""
            print(f"  \u2022 {r['trait']}{conf}")
            if r.get("evidence"):
                print(f"    {r['evidence']}")
        print()

    if result.get("next_test") and result["next_test"].lower() != "none":
        print("NEXT TEST")
        print(f"  {result['next_test']}")
        print()
        print("  A hypothesis, not an edge. Change config.py, then measure it with")
        print("  scripts/winrate_lab.py before believing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
