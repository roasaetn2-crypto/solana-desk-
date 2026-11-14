# Ladder replay — measured, not argued

Replayed **4,200 real entries** from `backtest_g87e.json` against the actual candles
in `data/g87e/` (23 pool/timeframe pairs), walking bar by bar with intrabar
high/low. Only the exit rule changes between runs, so the comparison isolates the
ladder. Reproduce with `python3 scripts/ladder_replay.py`.

Conservative bias: when a bar contains both the stop and a take-profit, the stop
is assumed to fill first. That understates every ladder equally.

## Finding 1 — win rate is set by the STOP, not the ladder

Fast ladder, 24-bar horizon, stop width swept:

| stop | win% | avgW | avgL | R:R | exp% | PF | break-even win |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1.5% | 45.0% | 11.17 | 1.37 | 8.15 | +4.27 | 6.67 | 10.9% |
| 3.0% | 53.7% | 10.90 | 2.14 | 5.08 | +4.86 | 5.89 | 16.4% |
| 5.0% | 56.2% | 10.85 | 2.71 | 4.00 | +4.90 | 5.12 | 20.0% |
| 10.0% | 58.3% | 10.92 | 3.50 | 3.12 | +4.90 | 4.35 | 24.3% |
| 20.0% | 60.0% | 11.06 | 4.20 | 2.64 | +4.95 | 3.95 | 27.5% |
| 30.0% | 60.9% | 11.17 | 4.10 | 2.73 | +5.19 | 4.24 | 26.8% |

Win rate climbs **45% → 61%** as the stop widens. Expectancy barely moves
(**+4.3% → +5.2%**). Widening the stop buys you a nicer number and almost no
extra money — the win rate and the R:R are the same seesaw, just cranked.

## Finding 2 — the ladder choice does NOT move win rate

3% stop, 24 bars:

| ladder | win% | avgW | avgL | R:R | exp% | PF |
|---|---:|---:|---:|---:|---:|---:|
| fast (live) | 53.7% | 10.90 | 2.14 | 5.08 | **+4.86** | 5.89 |
| slow (config) | 53.5% | 15.03 | 2.15 | 6.99 | **+7.04** | 8.04 |
| single 1.35x | 53.5% | 13.31 | 2.15 | 6.20 | +6.13 | 7.15 |

At 1.5% stop: fast +4.27 / slow +6.20 / single +5.39.
At 30% stop: fast +5.19 / slow +7.16 / single +5.95.

Win rate is **identical to within 0.2 points** across all three ladders at every
stop width. Expectancy is not: the **slow ladder beats the fast one at every
single stop width tested**, by roughly 30-45%.

## Finding 3 — the 2026-11-14 "promotion" was a downgrade

`RECOMMENDATION.md` promoted the fast ladder on the strength of 68.9% win on 120
pools, then admitted in its own 2026-11-15 update that those pools were ~4.5
**years** old and had **"no overlap at all"** with the fresh launches the desk
trades.

On the population the desk actually trades, the fast ladder delivers the *same*
win rate as the slow one and **~30% less expectancy**. The win-rate gain that
justified the switch never existed here; the cost did.

## What this means for 40W / 92L (30.3%)

The model says a 1.5% stop with the fast ladder wins ~45%, not 30%. The gap
between 45% and your 30% is the part that is *yours* — live slippage, entry
timing, and the 7 open positions. That gap is worth chasing; the ladder is not.

## The honest recommendation

1. **Revert to the slow ladder** (`tp1_at 1.5 / tp2_at 2.0`) or the single 1.35x.
   Same win rate, materially better expectancy. This is a one-line config change.
2. **Do not widen the stop to raise the win rate.** It moves the number 15 points
   and the money almost not at all.
3. **Fix entry quality, not exits.** The only lever that changes *which* trades
   you take — and therefore the population's win rate — is the entry filter.

## Caveats, stated plainly

- These are the backtest's entries, not your 135 live trades. The comparison
  between ladders is valid because entries are held constant; the absolute
  numbers are not your account.
- `total%` in the CLI is the sum of per-trade percentages, not account growth.
  Ignore it.
- "timeout" is a large share of exits (1,604 of 4,200 at 3%). Those are trades
  that neither stopped nor reached a rung inside 24 bars and were closed at
  market. That is a real outcome, but it is sensitive to the bar horizon.
