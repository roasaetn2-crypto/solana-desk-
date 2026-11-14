# Win-Rate Lab — the honest path to ≥50%

## The question

"Can we get the win rate to 50%?" is the wrong question on its own. A 50% win
rate that loses money is a trap with a nice number. So this lab states the bar
**before** it runs, and only reports configs that clear all four:

| bar | value | why |
|-----|-------|-----|
| win rate | **≥ 50%** | what the operator asked for |
| profit factor | **≥ 1.20** | otherwise the fast ladder is a slow bleed |
| independent pools | **≥ 10** | trades inside one pool are one observation |
| trades | **≥ 30** | below this it is a coin flip with a story |

The pool bar is the one that keeps biting. The committed caches cover ~60–101
pools, but the gate ladder collapses that to **5 fully-gated trades**, and the
"profile" signal reaches 50% on only **4 pools**. Four pools is not evidence.

## What it does

Win rate is the *joint* product of **which entries you take** and **when you
sell**. A fast take-profit ladder mechanically raises the win rate by banking
small moves before they reverse; a good entry filter keeps the losers small
enough that the fast ladder still makes money. The lab sweeps both axes at once:

- **Exits:** profile (default), quick 1.25x half, single 1.35x, tight trail
  1.4x, patient 2x/3x.
- **Stops:** 15%, 20%, 25%, 30%.
- **Entries:** no pullback / 10% / 20% pullback, × gate sets (none, scan,
  scan+flow, full).

That is 5 × 4 × 3 × 4 = 240 cells. To keep it fast it precomputes each entry's
forward outcome under every (ladder, stop) pair once, then filtering is free.

## Reproduce

```bash
# on the harvested pool store
python3 scripts/winrate_lab.py --store data/poolstore.json --out winrate_lab.json

# plumbing check with synthetic data (no network)
python3 scripts/winrate_lab.py --demo
```

Output is `winrate_lab.json`: `winners` (clears all four bars), `best_profitable`
(positive PF, ranked by win), `best_at_target` (≥50% win, ranked by PF), and the
full 240-row `all` table.

## Reading the result

The lab is deliberately stingy. On a 10-pool seed of liquid majors
(BONK/WIF/POPCAT/TRUMP) it reported **no** config clearing all four bars: the
only ≥50% configs (pullback 20% + 30% stop + fast TP, 76–81% win, PF 5–6.5) fired
in just **4 pools / 38 trades** — the same overfit cluster `MIN_POOLS` exists to
reject. The lesson is not "no edge"; it is "not enough independent pools yet."

After growing the store to **120 pools / 114,033 candles**, the lab found a
config that clears all four bars (see `RECOMMENDATION.md`):

> 20% pullback entry + fast ladder (sell half at 1.25×, rest at 1.6× or 15%
> trail) + 30% stop → **74 trades, 11 pools, 68.9% win, PF 3.75**.

Leave-one-pool-out on the top config: dropping the most influential pool still
leaves **58.5% win / PF 2.71** across 10 pools. The edge survives.

The one caveat: that 11-pool result uses all-age pools. With the desk's scan gate
(age ≤48h) on, only 4–5 pools survive and the config fails the independence bar
on 4 pools. The gap is pool supply in the tradeable-age band — which is the
collector's job, not the signal's.

Grow the store, then re-run. The bar does not move.
