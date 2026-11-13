#!/usr/bin/env python3
"""Prove Groq is wired into the desk -- through the desk's own code, not the SDK.

The desk never imports the `groq` package. It posts to api.groq.com with
`requests` via desk/brain_advisor.py:Groq. So a working `from groq import Groq`
snippet tells you nothing about whether the desk can call Groq. This does.

Three checks, cheapest first:

  1. key    -- is GROQ_API_KEY set, and does it look like a Groq key?
  2. wired  -- does the Advisor actually hold a Groq provider, in what order,
               with what budget, and does it consider itself ready?
  3. live   -- one real call through the desk's Groq class, with the desk's
               exact payload, parsed by the desk's own parse_opinion().

Usage:
    python scripts/check_groq.py          # all three
    python scripts/check_groq.py --offline  # skip the network call
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desk import brain_advisor as ba
from desk.ops import load_env

OK = "  ok   "
BAD = "  FAIL "
WARN = "  warn "

SYSTEM = (
    "You are a risk reviewer for a Solana memecoin desk. Reply with JSON only: "
    '{"verdict":"buy|watch|skip","confidence":0-100,"reason":"one short sentence"}'
)
USER = (
    "Token: CHECK\nPrice: 0.00123 SOL\nLiquidity: 42 SOL\n"
    "Age: 3 minutes\nHolders: 120\nRecent move: +18% in 5m\n"
)


def main() -> int:
    offline = "--offline" in sys.argv
    load_env()
    fails = 0

    print("1. key")
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        print(BAD + "GROQ_API_KEY is not set.")
        print("       fix: bash termux/set-ai-keys.sh - gsk_yourkey")
        return 1
    if not key.startswith("gsk_"):
        print(BAD + f"GROQ_API_KEY starts with {key[:4]!r}, not 'gsk_'.")
        print("       A non-gsk_ key here means it landed in the wrong slot.")
        fails += 1
    else:
        print(OK + f"GROQ_API_KEY set, gsk_ prefix, {len(key)} chars")

    print("\n2. wired")
    adv = ba.Advisor()
    order = [p.name for p in adv.providers()]
    print(OK + "provider order: " + " -> ".join(order))
    if "groq" not in order:
        print(BAD + "no Groq provider in the chain")
        return 1
    if order.index("groq") > order.index("ollama"):
        print(BAD + "Groq sits after the paid provider -- free calls would be skipped")
        fails += 1
    else:
        print(OK + f"groq at position {order.index('groq') + 1} of {len(order)}, ahead of paid")

    g = adv.groq
    print(OK + f"model {g.model}")
    print(OK + f"budget {g.budget}/day, {ba.RPM['groq']}/min, spread={g.spread}")
    if not g.available():
        print(WARN + "groq is cooling or out of budget right now")
    else:
        print(OK + "groq reports available()")

    pair = [p.name for p in adv.pair()]
    print(OK + f"would be asked first: {pair or 'nothing'}")
    if "groq" not in pair and pair:
        print(WARN + "groq is not in the first-choice pair (another provider is ahead)")

    if offline:
        print("\n3. live -- skipped (--offline)")
        return 1 if fails else 0

    print("\n3. live")
    try:
        raw = g.call(SYSTEM, USER, 400)
    except ba.ProviderError as e:
        print(BAD + f"{e}")
        if e.status in (401, 403):
            print("       key rejected -- wrong key, or revoked in the Groq console")
        elif e.limited:
            print("       quota/credit limit -- not a wiring problem")
        return 1
    print(OK + f"got {len(raw)} chars back")
    op = ba.parse_opinion(raw)
    if not op:
        print(BAD + f"reply was not parseable JSON: {raw[:200]!r}")
        return 1
    print(OK + f"parsed: verdict={op.get('verdict')} confidence={op.get('confidence')}")
    print(OK + "Groq is live inside the desk.")

    snap = g.snapshot()
    print(f"\n   used today: {snap['used_today']}/{snap['budget']}  errors: {snap['errors']}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
