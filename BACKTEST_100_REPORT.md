# 100-Trade Backtest — Win Rate Report

**Repo:** `roasaetn2-crypto/solana-desk-` · **Data:** real GeckoTerminal Solana pool OHLCV (5-min candles) · **Rules:** your own `desk/exits.py` + PAPER fees (1%) & slippage (2%)

## Headline

| Metric | Value |
|---|---|
| **Trades** | 100 |
| **Win rate** | **13.0%** (13 W / 87 L) |
| Median return per trade | **−99.8%** |
| Mean return (outliers capped at +1000%) | +9.1% |
| Profit factor (capped) | **1.11** |
| Best / Worst | +198,908% / −100% |
| Pools scanned | 100 |

## What the 100 trades actually did

| Outcome bucket | Trades | Share |
|---|---|---|
| **Rug / near-total loss (< −90%)** | 81 | 81% |
| Big loss (−90% … −30%) | 5 | 5% |
| Small loss (−30% … 0%) | 1 | 1% |
| Small win (0 … +50%) | 2 | 2% |
| Win (+50% … +200%) | 1 | 1% |
| Big win (+200%+) | 10 | 10% |

**Exit reasons:** timeout 3h under +5% (73), TP1 (11), trail (11), max hold (10), stop (6), TP2 (5).

## The honest read

- **13% win rate, and 81% of trades were rugs.** That is the reality of buying freshly-launched Solana memecoin pools: the median token goes to ~zero.
- The strategy is **not** a steady winner — it's a **lottery-ticket profile**: 87 small-to-total losses funded by ~10 big winners. The capped profit factor of **1.11** means it only just breaks even *if* you actually catch those 10 runners.
- The eye-popping +198,908% / +114,463% trades are **data artifacts** from mispriced illiquid pools (e.g. WETH/SOL, ZEC/SOL). I capped returns at +1000% for the mean/profit-factor so those don't lie to you. Do not bank on them.
- **Stops don't save you on rugs.** Losses hit −100% despite a 20–30% stop because 5-min candles gap straight through the stop on a rug — there's no fill in between. This is real, not a bug.

## Methodology & caveats (read this)

1. **This is an *exit-rule* backtest, not a full entry-gate backtest.** The repo has no recorded `journal.jsonl`, so `desk/backtest.py` had nothing to replay. I pulled 242 real pools from GeckoTerminal, took the first 100 with ≥20 candles, **entered at the first candle close**, and walked forward applying your exact exit profiles (scalp/swing), TP1 1.5x (sell 33%), TP2 2x (sell 50%), trailing runner, 20–30% stop, 3h timeout, 4h max hold.
2. **Entry timing is worst-case.** Entering at the first candle = buying at/near launch, which is the single worst moment for most memecoins. Your real entry gates (score, security, liquidity, dev checks) would filter many of these 81 rugs — so **live win rate should be higher than 13%**, but the gates are not reconstructable from historical OHLCV.
3. **No look-ahead.** Every price is real and only past candles are used at each step. Nothing is invented.
4. **Fees are included:** 1% fee + 2% slippage on both entry and exit (your `PAPER` config).

## Reproduce

```bash
python scripts/backtest_100.py --trades 100 --out backtest_100.json
python scripts/make_backtest_chart.py   # -> backtest_100.png
```

## Bottom line

On 100 real Solana pools with your current exit rules and **no entry filtering**, the win rate is **~13%**, driven by an 81% rug rate. The rules survive only through a handful of large winners (profit factor ≈ 1.11). The path to a better win rate is **tighter entry gating** (liquidity floor, holder concentration, dev-wallet checks) — not looser exits.
