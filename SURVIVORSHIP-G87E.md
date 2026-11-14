# Survivorship check on G87E — the +7.9% was a lie

**Short version:** the "fresh meme" segment that made G87E look alive (+7.51% mean,
+7.9% median, 8/11 green) was picked from the top of GeckoTerminal's volume
ranking — i.e. from coins that had *already* run 200–1700%. Re-run on a sample
with **no performance filter at all**, the same tuned config returns
**−22.5% median, 0 of 9 pools profitable**. The edge was the coin selection, not
the strategy. There is no edge.

---

## What was tested

Same code, same execution model, same costs as the main backtest:

| Parameter | Value |
|---|---|
| variant | fixed (or→and, EMA trend filter) |
| stop | 2.5 × ATR |
| trail arm / offset | 2.0 / 1.0 × ATR |
| time stop | 24 bars |
| cooldown | 3 bars |
| sizing | 1% risk, 25% notional floor, 90% cap |
| fees | 0.05% per side |
| slippage | 0.30% per side |
| timeframe | 5m, 1000 bars (~3.5 days) |

Only the **pool selection** changed.

## How the unbiased sample was built

The original sample was the top of the volume ranking. That is the definition of
survivorship bias: you are buying the coins that already won, then measuring how
the strategy did on them.

This sample walks **mid-rank pages (2–10)** of the Solana pool list — ranks
roughly 40–200 by 24h volume. A pool qualifies if it is *tradable* and *old
enough to backtest*, and for no other reason:

- liquidity 15k–300k USD
- 24h volume ≥ 25k USD
- pool age ≥ 2 days (so 5m series has ≥ 60 bars)
- not a stable / blue-chip pair

There is **no filter on past performance**. A pool that dumped 90% is as
eligible as one that pumped 10x. That is the population you actually face when
you scan for something to buy.

## Result

### Memes only (stablecoin pairs removed)

| Metric | Unbiased sample | Original "fresh meme" sample |
|---|---|---|
| pools | 9 | 11 |
| trades | 489 | — |
| **median return** | **−22.50%** | **+7.90%** |
| mean return | −20.60% | +7.51% |
| profitable runs | **0 / 9** | 8 / 11 |
| worst run | −33.60% | — |
| best run | −5.92% | — |
| avg win rate | 37.5% | — |
| avg max drawdown | 22.9% | 11.4% |

### All 12 pools (including 3 stablecoin pairs that slipped the name filter)

median −23.16%, mean −23.03%, 0/12 green, 647 trades.

### Per pool

| Pool | Liq | Buy & hold | Strategy | Win% | Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| GATO / SOL | 291,292 | +11.6% | −5.9% | 58.2% | 12.2% | 55 |
| GTA6 / SOL | 62,284 | −17.5% | −8.4% | 58.5% | 16.8% | 53 |
| SPCX / SOL | 56,847 | −3.6% | −14.1% | 30.9% | 14.4% | 42 |
| Punch / SOL | 124,106 | +13.5% | −14.7% | 43.1% | 19.1% | 65 |
| TTWO / SOL | 76,817 | +3.5% | −22.5% | 21.7% | 23.2% | 46 |
| HYPE / SOL | 39,384 | −2.6% | −23.8% | 36.7% | 23.9% | 49 |
| AVGOx / SOL | 32,048 | +14.9% | −29.7% | 32.1% | 29.8% | 56 |
| BIG / TTWO | 109,680 | **+1017.7%** | −32.7% | 16.7% | 33.0% | 60 |
| NIO / SOL | 167,206 | +89.9% | −33.6% | 39.7% | 33.8% | 63 |

The BIG/TTWO row is the whole story in one line. The coin went up **ten-fold**
over the window and the strategy still lost 32.7%, because it was chopped to
death by its own exits — 60 trades, 16.7% win rate, on a coin that only went up.
That is not a timing problem you can tune away; that is an entry that has no
information.

## Why this is the honest number

1. **No selection on outcome.** Pools were chosen by rank and age, not by how
   much they had already moved.
2. **Same execution model.** Identical costs, stops, sizing and intrabar
   assumptions as every other variant, so the only variable is the sample.
3. **The direction is consistent with the cost autopsy.** In the main backtest,
   with fees and slippage set to zero, the tuned entry averaged **−0.146% per
   trade on majors** — no gross edge at all. The +4.355% gross on fresh memes
   was the coins' momentum, not the signal. This run confirms it: remove the
   winner-picking and the number goes negative.

## What this means for the desk

- **Do not ship G87E as an entry signal.** The fixed exits are fine; the entry
  is noise. Shipping it live would convert a −22% median into real losses at
  0.30% slippage per side.
- **The exits are salvageable.** GATO and GTA6 both finished with ~58% win rates
  and single-digit-to-mid-teen drawdowns. The stop/trail/time-stop machinery is
  not the problem.
- **The missing piece is a reason to trade.** The desk already has one:
  `desk/signals.py` and `desk/guard.py` (volume expansion vs baseline, liquidity
  floor, holder growth, dev-wallet check). Those are *reasons*; G87E's
  three-lookback breakout is a *shape*. Wire the guards in as the entry gate and
  re-run this exact unbiased sample. If the median does not move, the desk has
  its answer and should stop trying to scalp memes on 5m charts.

## Reproduce

```bash
cd solana-desk
python3 scripts/survivorship_g87e.py     # ~4 min, rate-limited by GeckoTerminal
cat backtest_g87e_survivorship.json
```

Raw run data: `backtest_g87e_survivorship.json` (per-pool returns, win rates,
drawdowns, trade counts, buy-and-hold for the same window).

> Note: GeckoTerminal's free tier only serves pool-list pages 1–10; pages 11+
> return HTTP 401. That caps a single pass at ~12 qualifying pools from the
> mid-rank band. The sample is small, but 0/9 green with a −22.5% median is not
> a small-sample accident — it is the same sign as the zero-cost autopsy.

---

## Follow-up: the desk's own guards, on this same sample

Tested `desk/guard.py` (real code, replayable) and a volume-expansion gate (proxy for
`desk/signals.py`, which is live-network only and cannot be backtested) on these exact
12 pools:

| Variant | Median | Mean | Green | Avg trade | Avg DD |
|---|---:|---:|---:|---:|---:|
| control | −23.25% | −23.12% | 0/12 | −0.893% | 24.9% |
| + breaker | −12.71% | −13.68% | 0/12 | −0.761% | 15.4% |
| + volgate | −13.54% | −10.40% | 2/12 | −0.369% | 14.6% |
| + both | **−8.75%** | −5.86% | 2/12 | −0.346% | **9.6%** |

Every pool improved; ten of twelve are still negative. The gates cut the bleed by
roughly two-thirds and do not create an edge. Full write-up: [`GUARD-G87E.md`](GUARD-G87E.md).
