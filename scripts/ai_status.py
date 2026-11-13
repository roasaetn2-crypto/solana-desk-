#!/usr/bin/env python3
"""Which AI providers does this desk have, and how much free quota is left?

Runs without the server: it reads .env, builds the advisor, and prints one row
per provider. Nothing here calls a provider, so it costs no quota and works
while the desk is down.

    python scripts/ai_status.py

Exit code 0 if at least one provider can answer right now, 1 if none can.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk.ops import load_env  # noqa: E402

# cost is what the provider charges once the free allowance is gone. "free"
# means there is no card on file and no way to be billed.
COST = {
    "gemini": "free tier",
    "groq": "free tier",
    "cerebras": "free tier",
    "cloudflare": "free 10k Neurons/day",
    "openrouter": "free ':free' models",
    "ollama": "PAID - needs credits",
}


def main() -> int:
    load_env(os.getenv("DESK_ENV", ".env"))

    from desk import brain_advisor as ba

    adv = ba.Advisor()
    quota = adv.quota()
    rows = quota["providers"]

    print(f"{'provider':<12} {'cost':<24} {'keys':>4} {'used':>5} {'left':>5} {'state':<10}")
    print("-" * 68)

    ready = []
    for name, row in rows.items():
        if not row["keys"]:
            state = "no key"
        elif row["cooling_s"] > 0:
            state = f"cooling {int(row['cooling_s'])}s"
        elif not row["enabled"]:
            state = "off"
        elif name == "ollama":
            # A key is not the same as credits. This one bills, so it is never
            # counted as "can answer at no cost" no matter what it reports.
            state = "key ok, paid"
        else:
            state = "ready"
            ready.append(name)
        print(
            f"{name:<12} {COST.get(name, '?'):<24} {row['keys']:>4} "
            f"{row['used']:>5} {row['left']:>5} {state:<10}"
        )

    print("-" * 68)
    print(f"free calls left today: {quota['total_left']} of {quota['total_budget']}")

    if ready:
        print(f"can answer now, at no cost: {', '.join(ready)}")
    elif rows["ollama"]["keys"]:
        print("only the PAID provider (ollama) has a key - it needs credits to work")
    else:
        print("nothing can answer: the desk will fall back to the rule-based heuristic")

    missing = [n for n, r in rows.items() if not r["keys"] and n != "ollama"]
    if missing:
        print()
        print("free providers with no key yet (each one is a separate daily bucket):")
        for n in missing:
            print(f"  {n:<11} {COST.get(n, '')}")

    # A cooling provider is not a broken one; it is out of quota for the day.
    resting = [n for n, r in rows.items() if r["cooling_s"] > 0]
    if resting:
        print()
        print(f"resting (out of quota, back later): {', '.join(resting)}")

    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
