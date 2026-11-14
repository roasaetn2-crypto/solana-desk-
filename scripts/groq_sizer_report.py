"""Does Groq have ranking skill? Reads the journal and answers with numbers.

    python3 scripts/groq_sizer_report.py

Joins every `groq_sizer` decision against the `groq_sizer_pnl` row written when
the position closed, then reports expectancy per multiplier band.

The reading:
  - expectancy falls as the multiplier falls  -> the model ranks. Worth keeping.
  - expectancy is flat or inverted            -> no signal. Set BRAIN_SIZER_ON=0.

Until there are at least ~30 closes per band this cannot answer anything, and it
will say so rather than pretend.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desk import groq_sizer  # noqa: E402


def main() -> int:
    path = os.getenv("DESK_JOURNAL", "data/journal.jsonl")
    res = groq_sizer.buckets(path)
    if "error" in res:
        print(res["error"])
        print("\nThe journal is written by the desk as it trades. Run the desk first.")
        return 1

    print(f"journal: {path}")
    print(f"decisions: {res['decisions']}   matched closes: {res['matched_closes']}\n")
    print(f"{'mult':>6} {'n':>5} {'avg P&L $':>11} {'win rate':>9}")
    for k in ("1.0", "0.75", "0.5", "0.25"):
        b = res["bands"][k]
        avg = f"{b['avg_pnl_usd']:.4f}" if b["avg_pnl_usd"] is not None else "-"
        wr = f"{b['win_rate']*100:.0f}%" if b["win_rate"] is not None else "-"
        print(f"{k:>6} {b['n']:>5} {avg:>11} {wr:>9}")

    print(f"\nverdict: {res['verdict']}")

    thin = [k for k in ("1.0", "0.75", "0.5", "0.25") if res["bands"][k]["n"] < 30]
    if thin:
        print(f"\ncaution: bands {', '.join(thin)} have fewer than 30 closes. "
              f"Treat any pattern here as noise until they fill in.")
    print("\nconfig: BRAIN_SIZER_ON=0 disables the layer entirely.")
    print("        BRAIN_SIZER_BUDGET caps calls/day (default 40).")
    json.dump(res, open("groq_sizer_report.json", "w"), indent=1)
    print("wrote groq_sizer_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
