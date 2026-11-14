# G87E backtest - the Pine as written, the fixed logic, and the tuned version

Real GeckoTerminal OHLCV on 10 Solana pools (4 majors, 6 fresh meme coins) and three timeframes (5m / 15m / 1h), up to 1000 bars each - 23 runs. All three variants run through the identical execution model, so the only difference between the curves is the strategy. Costs: 0.05% fee and 0.30% slippage per side, charged on entry and exit.

## Verdict

The original strategy is not a strategy, it is a fee pump. It made **2658 trades** across 23 runs, won **27.92%** of them, and lost a mean of **-28.15%** per run. Only 9/23 runs finished green, and every one of those was a fresh meme coin that went up 200-1700% while the strategy captured a few percent of it.

Fixing the logic alone (same parameters) lifts the mean to **-23.66%** and cuts trades to 1887. Tuning the exits and the position floor lifts it to **-8.12%** mean / **-9.8%** median with 805 trades.

None of the three is a profitable system on this basket. That is the finding, not a bug in the backtest. The rest of this document is why.

## Summary table

| Segment | Variant | Trades | Win% | Mean ret | Median ret | Profitable runs | Avg max DD |
|---|---|---|---|---|---|---|---|
| All 23 runs | Pine as written | 2658 | 27.92% | -28.15% | -37.81% | 9/23 | 31.63% |
| All 23 runs | fixed | 1887 | 28.3% | -23.66% | -25.31% | 8/23 | 25.65% |
| All 23 runs | tuned | 805 | 39.5% | -8.12% | -9.8% | 8/23 | 18.06% |
| Majors (BONK/WIF/POPCAT/TRUMP) | Pine as written | 2263 | 19.75% | -58.88% | -60.35% | 0/12 | 59.04% |
| Majors (BONK/WIF/POPCAT/TRUMP) | fixed | 1609 | 20.57% | -47.16% | -49.9% | 0/12 | 47.35% |
| Majors (BONK/WIF/POPCAT/TRUMP) | tuned | 691 | 34.88% | -22.45% | -19.43% | 0/12 | 24.15% |
| Fresh memes | Pine as written | 395 | 74.68% | 5.37% | 3.39% | 9/11 | 1.73% |
| Fresh memes | fixed | 278 | 73.02% | 1.98% | 2.4% | 8/11 | 1.98% |
| Fresh memes | tuned | 114 | 67.54% | 7.51% | 7.9% | 8/11 | 11.42% |

## The cost autopsy

Run the tuned config with zero fees and zero slippage and you are measuring the raw edge of the entry logic:

| Segment | Trades | Avg gross edge / trade | Median run, zero cost | Median run, real cost |
|---|---|---|---|---|
| majors | 666 | -0.146% | -2.35% | -19.43% |
| fresh | 114 | +4.355% | 10.94% | 7.9% |
| all | 780 | +0.512% | -2.08% | -9.8% |

Read the first row. On majors the entry has an average gross edge of **-0.146% per trade** - statistically nothing. Three lookbacks agreeing does not predict the next twelve bars on a liquid Solana pair. On fresh memes the edge is real (+4.355% per trade) but it comes from the coin going up 200%+, not from the signal.

Now the cost. At 0.05% fee and 0.30% slippage per side you pay ~0.70% of notional per round trip, and notional is up to 90% of equity, so each trade costs ~0.63% of your account. A strategy with a 0.00% edge trading 57 times per run cannot survive that. That is why the majors median goes from -2.35% to -19.43%.

## Per-run detail

| Pool | TF | Bars | Buy & hold | Pine ret% | Pine tr | Fixed ret% | Fixed tr | Tuned ret% | Tuned tr | Tuned DD% |
|---|---|---|---|---|---|---|---|---|---|---|
| BONK/SOL | 5m | 1000 | -6.08% | -69.71% | 183 | -55.67% | 129 | -19.43% | 47 | 19.43% |
| BONK/SOL | 15m | 1000 | -6.9% | -54.32% | 176 | -49.9% | 124 | -26.34% | 53 | 26.81% |
| BONK/SOL | 1h | 1000 | -28.5% | -37.81% | 187 | -25.31% | 126 | -13.46% | 50 | 15.49% |
| WIF/SOL | 5m | 1000 | -9.65% | -69.62% | 191 | -60.41% | 148 | -38.27% | 69 | 38.91% |
| WIF/SOL | 15m | 1000 | -17.76% | -67.15% | 190 | -57.63% | 138 | -25.47% | 60 | 26.27% |
| WIF/SOL | 1h | 1000 | -40.52% | -51.17% | 190 | -33.7% | 129 | -9.8% | 51 | 12.64% |
| POPCAT/SOL | 5m | 1000 | -7.72% | -71.37% | 193 | -59.15% | 139 | -41.07% | 71 | 41.31% |
| POPCAT/SOL | 15m | 1000 | -19.66% | -67.35% | 190 | -55.05% | 137 | -16.62% | 55 | 17.07% |
| POPCAT/SOL | 1h | 1000 | -38.92% | -42.71% | 185 | -38.1% | 135 | -14.03% | 54 | 14.98% |
| TRUMP/USDC | 5m | 1000 | -4.84% | -70.66% | 198 | -54.41% | 138 | -36.87% | 69 | 36.87% |
| TRUMP/USDC | 15m | 1000 | -32.72% | -60.35% | 187 | -48.07% | 136 | -27.74% | 63 | 29.7% |
| TRUMP/USDC | 1h | 1000 | -48.33% | -44.4% | 193 | -28.51% | 130 | -0.33% | 49 | 10.3% |
| FOOD/SOL | 5m | 331 | 1709.47% | 19.2% | 61 | 7.05% | 43 | 37.7% | 18 | 34.77% |
| FOOD/SOL | 15m | 111 | 1003.88% | 3.39% | 16 | -1.26% | 10 | 2.04% | 3 | 6.97% |
| AMC/SOL | 5m | 99 | 1778.44% | 4.27% | 15 | -3.69% | 9 | -8.32% | 3 | 8.32% |
| Sakura/SOL | 5m | 224 | 492.72% | -0.03% | 37 | 2.4% | 29 | 9.02% | 10 | 11.81% |
| Sakura/SOL | 15m | 76 | 499.94% | 0.38% | 8 | 1.33% | 8 | -9.99% | 3 | 13.72% |
| inuhao/SOL | 5m | 487 | 53.36% | 18.81% | 98 | 1.87% | 61 | 7.36% | 27 | 20.68% |
| inuhao/SOL | 15m | 163 | 16.02% | 2.59% | 26 | 5.24% | 17 | 23.4% | 9 | 12.1% |
| SHARKBUX/SOL | 5m | 329 | 217.5% | 5.24% | 68 | 3.37% | 42 | 9.34% | 14 | 6.73% |
| SHARKBUX/SOL | 15m | 110 | 211.51% | -3.89% | 16 | -2.94% | 10 | -6.67% | 6 | 10.45% |
| ANTH/SOL | 5m | 196 | 240.51% | 6.05% | 41 | 5.38% | 40 | 10.78% | 15 | 0.01% |
| ANTH/SOL | 15m | 66 | 236.38% | 3.05% | 9 | 3.05% | 9 | 7.9% | 6 | 0.01% |

## What was actually broken, in order of how much it cost you

**1. `or` instead of `and` - the fatal one.**

```pine
if (close > close[3] or close > close[12] or close > close[30])
```
Three conditions joined by `or` is true on nearly every bar: if price is above *any* one of three lookbacks, it goes long. The short branch below is `or` too, but the long branch is tested first and `strategy.entry` fires, so the short branch is unreachable. The strategy is a permanent long bias machine. On a coin that only goes down it buys every bar of the way to zero. Fixed to `and` (all three must agree) and made the branches mutually exclusive with `else if`.

**2. No trend filter.** Three lookbacks agreeing still fires on a bounce inside a downtrend. Added a 50-EMA regime filter.

**3. `math.round(qty * 1000) / 1000` silently disables meme coins.** For a token at $0.000002 the position rounds to 0.000 and no order is ever placed. You would see a flat line and assume the market was quiet. The step now scales to the price.

**4. `atr` can be zero, so `qty = risk / 0 = inf`.** Pine returns `na` and quietly skips the order; a Python port returns `inf` and buys the planet. ATR is floored at 0.05% of price.

**5. No slippage.** 0.05% commission and a fill at the exact close is a TradingView fantasy. 0.30% per side is added here, and it is the single biggest reason the numbers look worse than your chart.

**6. Instant re-entry.** `strategy.position_size == 0` is true on the bar right after an exit, so it re-enters immediately and pays the spread again. Added a cooldown.

**7. `abierto` is never reset on a stop-out.** The bar counter survives the exit, so the `maxBars` check can fire on a brand-new trade. Reset on flat.

**8. `refEquity` only compounds in live mode.** `(liveMode and barstate.isrealtime) ? strategy.equity : 50.0` means the backtest always sizes off the $50 start, so the equity curve you see is not the one you would have traded. Sizing is now off live equity in both.

**9. `trailMult = 0.2` is a hair-trigger.** The trail arms after 0.2xATR of profit and trails 0.2xATR behind. On a 5m meme chart that is inside the noise, so every winner is scratched at roughly breakeven. In the original run 27% of trades win and the average win is smaller than the spread. Arm and offset are now separate inputs (2.0x / 1.0x tested best).

**10. Risk sizing makes dust positions.** `riskUsd / (atr * stopMult)` on a token whose ATR is 20% of price with a 2.5xATR stop deploys about 2% of the account. The `tope` 90%-of-equity cap never binds, so the strategy risks $0.50 to make $0.02. Added a minimum notional floor. Flooring at 25% of equity took the fresh-meme median from +2.4% to +7.9%; it cannot save majors because the edge there is negative.

## What I would actually do with this

Do not run this on majors. The entry has no edge there and the cost is brutal. If you want to keep the idea, the only place it has any life is fresh meme coins on the 5m, and even there the edge is the coin's momentum, not the signal - which means the correct trade is a momentum breakout with a wide stop and a trailing exit that lets a 200% move pay for twenty losers, not a 5-bar time stop.

The three numbers to change before anything else: `trail_arm_mult` up from 0.2 to 2.0, `max_bars` up from 5 to 24, and add a minimum notional floor. Those three are the difference between the red curve and the green one in the chart.

## Files

- `g87e_fixed.pine` - the corrected Pine v6, paste into TradingView
- `desk/g87e.py` - the Python port, the fixes, the backtester
- `scripts/fetch_g87e_data.py` - GeckoTerminal OHLCV fetch + cache (`data/g87e/`)
- `scripts/backtest_g87e.py` - runs the matrix, writes this report
- `scripts/sweep_g87e.py` - 144-combination parameter sweep, scored on median
- `backtest_g87e.json` - every trade, every run
- `backtest_g87e_sweep.json` - the full sweep
- `backtest_g87e.png` - equity curves

## Honest limits

5m/15m/1h bars, up to 1000 per pool. This cannot model the liquidity you actually eat, MEV, failed transactions, or the fact that a fresh pump.fun coin's 5m candles are often one wallet. Treat the absolute returns as a sanity check on the logic, not a forecast. The comparison between variants is the trustworthy part: same data, same costs, same execution, one variable changed at a time.

---

## Update: the fresh-meme result did not survive an unbiased sample

Everything above is measured on pools drawn from the top of GeckoTerminal's
volume ranking - coins that had already run 200-1700%. That is survivorship
bias. Re-running the tuned config on a mid-rank sample with **no performance
filter** (pages 2-10, liquidity 15k-300k, volume >= 25k, age >= 2 days) gives:

| | Unbiased (9 meme pools) | Original (11 fresh memes) |
|---|---:|---:|
| median return | **-22.50%** | +7.90% |
| mean return | -20.60% | +7.51% |
| profitable runs | **0 / 9** | 8 / 11 |

One pool (BIG/TTWO) went up **1017%** and the strategy still lost 32.7% on it.
The +7.9% was the coin selection, not the signal. Full write-up:
[`SURVIVORSHIP-G87E.md`](SURVIVORSHIP-G87E.md), data in
`backtest_g87e_survivorship.json`.

**Conclusion: do not ship the G87E entry.** The exits are fine; the entry has no
information. The next honest test is the same unbiased sample with the desk's
existing guards (`desk/signals.py`, `desk/guard.py`) as the entry gate.
