# Signal Backtest — does the entry read predict direction, and which take-profit wins?

**Data:** 18 real Solana pool OHLCV paths from the public GeckoTerminal API (5-minute
candles), 4,595 simulated entries (an entry every 3 candles once 30 bars of history
exist). Reproduce with:

```bash
python scripts/backtest_signals.py --trades 40 --out backtest_signals.json --chart backtest_signals.svg
```

> **What this test can and cannot see.** The backtest reconstructs only the features
> price + volume carry: **momentum** and **flow**. Holders, narrative and safety need
> on-chain / social facts that OHLCV history does not keep, so they are neutral here.
> The result therefore measures the *momentum+flow* edge alone — not the full live
> signal. Every price is real; nothing is invented.

## Headline result

| slice | win rate | avg return / trade | profit factor | direction accuracy |
|-------|---------:|-------------------:|--------------:|-------------------:|
| **All entries** | 17.7% | −2.65% | 0.741 | 45.9% |
| **Signal BUYs only** (gate on) | 18.6% | −0.28% | 0.975 | 42.6% |
| Confidence 50–59% | 17.6% | −3.12% | 0.689 | 47.5% |
| Confidence 60–69% | 18.5% | −0.29% | 0.975 | 42.6% |
| Direction bullish | 17.5% | −2.72% | 0.747 | 43.6% |
| Direction bearish | 17.1% | −0.87% | 0.917 | 55.2% |

## Three honest findings

**1. Momentum + flow alone do NOT beat a coin flip on real 5-minute memecoin data.**
Direction accuracy is 45.9% overall and 42.6% on the entries the gate keeps — i.e.
slightly *worse* than random. On 5-minute bars, a coin that just ran tends to mean-revert,
so a pure price/volume "bullish" read is not a reliable predictor of the next hour.
The bearish read is better (55.2%): telling you *what to avoid* is easier than telling
you *what will pump*. **This is why the live signal also weighs holders, narrative and
safety** — the edge the user is looking for is not in momentum alone, and this backtest
proves it rather than pretending otherwise.

**2. The signal gate still cuts the bleed sharply.**
Even though momentum+flow is not predictive on its own, filtering to *signal BUYs only*
cuts the average loss from **−2.65% to −0.28%** per trade and lifts the profit factor from
**0.741 to 0.975** by refusing ~83% of entries (the worst ones). The gate is a *loss
reducer*, not a magic profit switch — exactly what an entry-quality filter should be.

**3. Higher win rate ≠ higher P&L. The take-profit choice is the real P&L lever.**

| take-profit plan | win rate | avg return / trade | profit factor |
|------------------|---------:|-------------------:|--------------:|
| quick 1.25x half | **24.1%** | −2.69% | 0.742 |
| single 1.35x | 20.5% | −2.03% | 0.815 |
| tight trail 1.4x | 19.9% | −2.29% | 0.794 |
| profile (default) | 18.6% | −0.28% | 0.975 |
| global 1.5x/2x | 18.6% | −0.28% | 0.975 |
| **patient 2x/3x** | 17.5% | **+0.86%** | **1.075** |

The plan with the **highest win rate** (quick 1.25x, 24%) **loses the most money**: it
caps winners at +25% while losers still run to the full stop. The plan with the
**lowest win rate** (patient 2x/3x, 17.5%) is the **only profitable one** (+0.86% per
trade, PF 1.075) because it lets the rare winner run to 2×/3× while every loser is cut
at the stop. On memecoins the payoff distribution is fat-tailed: a few big winners pay
for many small losers, so **capping the winners is the fastest way to lose.**

## What this means for increasing P&L / take-profit

1. **Don't chase win rate for its own sake.** Optimise expectancy (avg return) and
   profit factor. A 24% "win rate" plan that bleeds −2.7%/trade is worse than a 17.5%
   plan that makes +0.86%/trade.
2. **Let winners run.** Use the **patient** ladder (`tp1_at ≈ 2.0`, `tp2_at ≈ 3.0`,
   wider trail) or the default profile — not a quick 1.25× scalp. Set
   `exits.tp_plan = "profile"` and widen the rungs, or use `"global"` with a patient ladder.
3. **Keep the entry gate on.** It won't make a bad edge profitable, but it removes the
   worst trades and roughly halves the average loss.
4. **The real edge is in the facts, not the chart.** Because momentum alone is not
   predictive, the live signal's holders / narrative / safety weights matter most.
   Feed it good on-chain + social data (Helius, RugCheck, FOMO) and let the gate use the
   full composite — that is where a genuine directional edge can come from.

## Caveats

- Overlapping entries (every 3 candles) make the 4,595 samples highly correlated; treat
  the *relative* comparison between plans as the signal, not the absolute win rate.
- 18 pools is a small, recent sample; re-run periodically and on more pools.
- OHLCV-only: holders / narrative / safety are neutral here (see the box at the top).
