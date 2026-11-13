"""Reproduce the confirm-quote cache bug.

The tokens-v1 quote endpoint caches its response body under a single key,
"ds:tokens-v1", even though the URL contains the mint list. So the first mint
quoted in a 90s window poisons the cache for every other mint: the cached body
does not contain them, _quote_from_pairs returns nothing, quote() returns None,
and the desk rejects the entry as "no_confirm_quote".

Run: python3 scripts/repro_confirm_quote.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from desk import scanner  # noqa: E402

MINTS = [
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",   # BONK
    "So11111111111111111111111111111111111111112",     # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
]

s = requests.Session()
print("quoting three different mints back to back, same process:\n")
results = []
for m in MINTS:
    q = scanner.quote(m, s)
    ok = bool(q and q.get("price_usd"))
    results.append(ok)
    px = "" if not ok else "${:.8g}".format(q["price_usd"])
    print(f"  {'OK  ' if ok else 'NONE'}  {m[:16]}...  {px}")

print()
if all(results):
    print("PASS: every mint quoted")
    sys.exit(0)
if results[0] and not any(results[1:]):
    print("BUG CONFIRMED: the first quote succeeded, every later mint returned None.")
    print("Cause: _EP_CACHE['ds:tokens-v1'] is keyed by endpoint id, not by mint list.")
    sys.exit(1)
print(f"INCONCLUSIVE (network?): {results}")
sys.exit(2)
