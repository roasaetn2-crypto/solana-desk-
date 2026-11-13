# Entry & Exit Test Report — Solana Desk

Date: 2026-11-13 · Branch: `main` (fix applied locally, **not pushed**)

## Verdict

**Entry works. Exit works.** Entry was broken in production by a scanner cache bug;
that bug is now found, fixed, and proven fixed with live fills.

---

## 1. The bug that was silently killing every trade

Symptom in the journal: tokens were **approved** by the analyst and risk engine, then
died at the confirm stage with `reason: no_confirm_quote` and `confirm_price: null`.

```
DECIDE PICO     True   approved, size x0.5 (regime)
REJECT confirm  no_confirm_quote  PICO
DECIDE FOOD     True
REJECT confirm  no_confirm_quote  FOOD
DECIDE TITTY    True
REJECT confirm  no_confirm_quote  TITTY
```

162 of these. 5 approvals → 5 rejects → 0 entries. A 100% block rate at the last gate.

**Root cause** — `desk/scanner.py`, `quote_many()`:

```python
body, fresh = _fetch_endpoint(s, ep, now)   # ep["id"] == "ds:tokens-v1"
```

`_EP_CACHE` is keyed by **endpoint id only**, but the DexScreener URL embeds the
**mint list**:

```
https://api.dexscreener.com/tokens/v1/solana/{mintA,mintB,...}
```

So the first chunk of up to 30 mints quoted in a 90-second window was cached under
`"ds:tokens-v1"` and then served to **every** later quote. The new mint was not in
that cached body, `_quote_from_pairs()` returned nothing, `quote()` returned `None`,
and the desk correctly refused to buy what it could not price.

**Fix** — the varying URL must not use the shared body cache:

```python
# The URL embeds the mint list, so this body must NOT go through the shared
# endpoint cache: _EP_CACHE is keyed by endpoint id alone, and caching a
# chunk of 30 mints under "ds:tokens-v1" served that same body to every
# later quote for 90s.
body = _fetch_live(s, ep, now)
```

Per-mint results are still cached by `_QUOTE_CACHE` and `_PAIR_CACHE`, which *are*
correctly keyed by mint, so no caching was lost.

### Before / after, same three mints, same process

`scripts/repro_confirm_quote.py` — quote BONK, wSOL, USDC back to back:

| | BONK | wSOL | USDC | exit |
|---|---|---|---|---|
| before | OK | **NONE** | **NONE** | 1 |
| after | OK $2.629e-06 | OK $78.82 | OK $1.00026 | 0 |

---

## 2. Live proof: entries land again

After restarting with the fix, within the first ~2 minutes:

```
DECIDE Tourettes True  →  ENTRY Tourettes  $5.95  @ 1.08222e-05  (scalp)
DECIDE Huang     True  →  ENTRY Huang      $3.96  @ 9.82464e-05  (swing)
DECIDE MRPENIS   True  →  ENTRY MRPENIS    $3.96  @ 7.62552e-05  (swing)
```

Plus TITTY and PICO. Final tally since the restart:

| metric | value |
|---|---|
| entries | **6** |
| exits | 0 (positions still young — scalp stop −20%, timeout 3h) |
| `no_confirm_quote` rejects | **0** |
| errors | 0 |

Book: cash $128.87, equity $158.14, 6 open, day P&L −$1.86 (fees + 2% slippage on
six round-trip entries — expected).

---

## 3. Live proof: exits fire

The first exit ever recorded in the journal fired in production, not in a test:

```json
{"kind":"exit","symbol":"GIGAPIG","reason":"stop -30%","pnl_usd":-1.4}
```

The exit engine ran the stop branch against a real quote and closed the position.
Every open position is carrying `stop_price`, `peak`, `last` and `stage` correctly
(e.g. TITTY: entry 7.67652e-07, last 7.884e-07, peak 7.884e-07, stop 6.141216e-07).

---

## 4. Deterministic proof: 30/30

`scripts/verify_entry_exit.py` runs the real broker + exit engine on a throwaway book
(`DESK_DATA_DIR` in a temp dir, `DESK_OFFLINE=1`):

```
1  ENTRY ................. buy fills, qty/cost/fee correct, position opened
2  SIZING DEADLOCK ....... two caution cuts still clear the $2 fee floor
3  STOP LOSS ............. −20% scalp stop closes at 1.0
4  TP1 THEN BREAKEVEN .... 1.5x sells 33%, stop moves to entry
5  TP2 ................... 2.0x sells 50% of the remainder
6  TRAIL AFTER TP1 ....... −20% off peak protects the runner, never below entry
7  TIMEOUT ............... flat after 3h closes
8  MISSING QUOTE GUARD ... 1 miss holds, 3 misses closes
9  ROUND TRIP WINNER ..... profit booked, cash and realized equity above start
10 RESET ................. re-bases to $160 and clears history

30 passed, 0 failed
```

## 5. Regression + full suite

- `tests/test_quote_cache.py` — 3 new tests: the varying URL must not be served from
  the shared body cache, and the per-mint quote/pair caches must stay mint-keyed.
  **3 passed.**
- Full suite: **448 passed, 17 skipped.**

---

## 6. What is NOT done

- **FOMO is not merged and not on `main`.** `desk/fomo.py` exists and is fully
  self-tested, but it is untracked and unwired — no `devcheck`, no `meme` hook.
  That is deliberate: you asked for the entry/exit test first.
- The scanner fix is **local only** (`M desk/scanner.py`). Nothing pushed.
- Your phone still needs `git pull` + restart + **one Reset tap** to re-base to $160
  once this is pushed.

Say the word and I will wire FOMO into the desk (thesis score as a hard kill in
`devcheck`, bounded ±8 nudge on `community` in `meme`), add `tests/test_fomo.py`,
and push the whole thing.
