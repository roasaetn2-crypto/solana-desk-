# Ladder replay — measured, not argued

Replayed **4,200 real entries** from `backtest_g87e.json` against the actual candles
in `data/g87e/` (23 pool/timeframe pairs), walking bar by bar with intrabar
high/low. Only the exit rule changes between runs, so the comparison isolates the
ladder. Reproduce with `python3 scripts/ladder_replay.py --sweep`.

Conservative bias: when a bar contains both the stop and a take-profit, the stop
is assumed to fill first. That understates every ladder equally.

## The headline

**Ladder choice barely moves win rate. It moves expectancy by 3×.** On identical
entries, at every stop width tested:

| stop | patient 2x/3x | slow 1.5/2.0 | fast 1.25/1.6 |
|---:|---|---|---|
| 1.5% | 44.8% / +12.61 | 44.9% / +6.20 | 45.0% / +4.27 |
| 3.0% | 53.4% / +14.19 | 53.5% / +7.04 | 53.7% / +4.86 |
| 10.0% | 57.6% / +14.63 | 57.8% / +7.18 | 58.3% / +4.90 |
| 30.0% | 60.2% / +15.24 | 60.4% / +7.58 | 60.9% / +5.19 |

(cells are `win% / expectancy%`)

Win rates sit within **1 point** of each other in every row. Expectancy does not:
the patient ladder earns **~3× the fast ladder** at a 1.5% stop and **~2.9×** at
30%. The fast ladder — the one promoted on 2026-11-14 — is the **worst of the
three on expectancy at every stop width**.

## Win rate is set by the STOP, not the ladder

Fast ladder, 24-bar horizon, stop swept:

| stop | win% | avgW | avgL | R:R | exp% | PF |
|---:|---:|---:|---:|---:|---:|---:|
| 1.5% | 45.0% | 11.17 | 1.37 | 8.15 | +4.27 | 6.67 |
| 3.0% | 53.7% | 10.90 | 2.14 | 5.08 | +4.86 | 5.89 |
| 5.0% | 56.2% | 10.85 | 2.71 | 4.00 | +4.90 | 5.12 |
| 10.0% | 58.3% | 10.92 | 3.50 | 3.12 | +4.90 | 4.35 |
| 20.0% | 60.0% | 11.06 | 4.20 | 2.64 | +4.95 | 3.95 |
| 30.0% | 60.9% | 11.17 | 4.10 | 2.73 | +5.19 | 4.24 |

Win rate climbs **45% → 61%** as the stop widens; expectancy moves **+4.3% → +5.2%**.
Widening the stop buys a nicer number and almost no extra money.

## The calibration caveat — read this before trusting any absolute number

The repo's own comment in `desk/meme_tactics.py` says the patient 2x/3x ladder
measured **win 30.3%**. This replay puts it at **60.2%** on the same nominal
ladder. That is a factor of two apart, so **this engine does not reproduce that
measurement** and its absolute win rates must not be read as forecasts.

The likely reason is population: the 30.3% came from pullback entries on the
120-pool store, while these are G87E trend entries on 23 pools. Different coins,
different entry rule.

What survives is the **relative** comparison, because entries are held constant
and the ordering is stable across four stop widths. The ranking is trustworthy;
the levels are not.

## What this means for 40W / 92L (30.3%)

No ladder in this replay produces anything near 30% — the floor is ~45% at the
tightest stop. So **your 30.3% cannot be explained by ladder choice at all.**
Something upstream is different: entry selection, live slippage, or the 7 open
positions.

That is the useful conclusion. Chasing the exit ladder is chasing the wrong
variable.

## Recommendation

1. **Do not expect win rate to move from exit tuning.** Three ladders, four stop
   widths, twelve combinations — win rate stayed inside a 1-point band per row.
2. **If you change anything on exits, move toward the patient ladder**, which
   earns roughly 3× the fast one on expectancy. The 2026-11-14 promotion went the
   other way and cost expectancy without buying win rate.
3. **Attack entry quality.** It is the only variable that changes *which* trades
   you take, and therefore the only one that can move a population's win rate
   from 30% to 50%.

## Caveats, stated plainly

- These are the backtest's entries, not your 135 live trades.
- `total%` in the CLI is the sum of per-trade percentages, not account growth.
  Ignore it.
- "timeout" is a large share of exits. Those trades neither stopped nor reached a
  rung inside 24 bars and were closed at market; the share is sensitive to the
  bar horizon.
