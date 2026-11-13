# Technical indicator evaluation — tested first, not added

The request was to try adding technical indicators (RSI, MACD, ATR, Bollinger
Bands, EMA, Fibonacci, Stochastic, ADX — the "whole picture" set from the
screenshot) to improve entry timing and take-profit exits, **but only add them
to the trading desk if they actually produce a high win rate.** This document is
the test result. Short version: **none of the eight cleared the bar, so none was
wired into the live desk.** The indicator code and the test harness were added
as offline tools so the decision is reproducible, not a one-off opinion.

## What was built

| File | Purpose |
| --- | --- |
| `desk/indicators.py` | Pure-Python RSI, MACD, ATR, Bollinger, EMA, Stochastic, ADX, Fibonacci. No numpy/pandas/TA-Lib, returns `None` when history is too short. |
| `desk/indicator_backtest.py` | Offline harness: baseline signal BUY vs baseline + each indicator filter, plus indicator-based exit timing and a take-profit sweep. |
| `scripts/indicator_eval.py` | CLI that pulls real Solana OHLCV from GeckoTerminal (or synthetic with `--offline`) and prints the verdict table. |
| `tests/test_indicators.py` | Known-value maths tests (Wilder's own RSI table, exact ATR/Bollinger/ADX fixtures). |
| `tests/test_indicator_backtest.py` | Verdict-logic and harness-wiring tests. |

The maths was cross-checked against the MIT-licensed zero-dependency reference
[`Bitvested/ta.py`](https://github.com/Bitvested/ta.py) and the classic TA-Lib
definitions. The RSI implementation reproduces Wilder's published table exactly
(70.46 then 66.25).

## The bar (stated before looking at the numbers)

An indicator is **ADD-WORTHY** only if, on the sample:

1. it keeps at least **30 trades** (so the win rate means something),
2. lifts the win rate by at least **5 percentage points** over the baseline, and
3. reaches a **profit factor ≥ 1.2** that is not worse than the baseline's.

Anything else is **DO-NOT-ADD**. A filter that only shrinks the sample without
lifting the win rate is noise, not edge.

## Data

* **Primary sample:** 50 real Solana pool OHLCV histories from the public
  GeckoTerminal API, 5-minute candles (up to 1,000 bars each), **3,013 entries**
  where the desk's own signal engine said BUY.
* **Take-profit sample:** 25 real pools, 5-minute candles, **1,001 signal-BUY
  entries** (a second, independent draw used for the TP sweep).
* A synthetic random-walk sample was also run as a plumbing check; as expected,
  nothing beat the bar there because there is no edge to find in noise.

These are OHLCV-only paths, so holders, security flags and narrative — which the
live signal also uses — are neutral here. The result answers "do price/volume
indicators add entry or exit edge on top of the signal?" It is directional
evidence, not a guarantee.

## Result — entry filters (primary 50-pool sample)

Baseline (signal BUY only): **n=3,013 · win 16.7% · avg −4.56% · PF 0.551**.

| Indicator filter | n | win | lift | PF | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| RSI < 35 (oversold) | 24 | 25.0% | +8.3pp | 1.129 | DO-NOT-ADD (only 24 trades) |
| RSI < 70 (not overbought) | 2,589 | 16.6% | −0.1pp | 0.557 | DO-NOT-ADD |
| RSI 35–60 (reclaim) | 1,688 | 15.3% | −1.4pp | 0.518 | DO-NOT-ADD |
| MACD hist>0 & above signal | 2,429 | 17.0% | +0.3pp | 0.579 | DO-NOT-ADD |
| ATR 0.4%–20% of price | 2,841 | 17.3% | +0.6pp | 0.563 | DO-NOT-ADD |
| Bollinger %B < 0.25 | 130 | 18.5% | +1.8pp | 0.649 | DO-NOT-ADD |
| Bollinger %B ≥ 0.5 | 2,509 | 16.5% | −0.2pp | 0.557 | DO-NOT-ADD |
| EMA 9>21>50 stack | 1,507 | 17.3% | +0.6pp | 0.524 | DO-NOT-ADD |
| Fibonacci 0.382–0.786 | 1,236 | 16.7% | −0.0pp | 0.647 | DO-NOT-ADD |
| Stochastic %K < 25 | 135 | 15.6% | −1.1pp | 0.513 | DO-NOT-ADD |
| Stochastic %K>%D & %K<80 | 1,070 | 17.0% | +0.3pp | 0.552 | DO-NOT-ADD |
| ADX≥20 & +DI>−DI | 1,616 | 16.7% | +0.0pp | 0.586 | DO-NOT-ADD |
| EMA stack + MACD bull | 1,249 | 18.1% | +1.4pp | 0.565 | DO-NOT-ADD |
| EMA stack + ADX trend | 1,135 | 17.1% | +0.4pp | 0.543 | DO-NOT-ADD |
| RSI<70 + MACD bull | 2,011 | 16.9% | +0.2pp | 0.593 | DO-NOT-ADD |
| Fib pocket + RSI<70 | 1,174 | 16.0% | −0.7pp | 0.597 | DO-NOT-ADD |

The 25-pool sample agreed: every filter stayed under the bar. The single
standout was **RSI < 35** (10 trades, 40% win, PF 1.35 there; 24 trades, 25% win,
PF 1.13 on the larger sample). That is the *only* indicator that showed a real
positive signal — but both samples are below the 30-trade minimum, so it is
**unproven, not add-worthy yet**. It is the one worth re-testing on a much larger
sample before anyone touches the live desk.

## Result — indicator exits (primary sample)

| Extra exit rule | n | win | lift | PF | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| Exit when RSI ≥ 75 | 3,013 | 22.0% | +5.3pp | 0.483 | DO-NOT-ADD (PF 0.483 < 1.2) |
| Exit when MACD hist < 0 | 3,013 | 11.1% | −5.6pp | 0.396 | DO-NOT-ADD |
| Exit when Stochastic %K > 85 | 3,013 | 17.5% | +0.8pp | 0.430 | DO-NOT-ADD |

The RSI-overbought exit is the classic "sell into strength" idea. It *does* raise
the win rate (+5.3pp) but **lowers profit factor** (0.483 vs baseline 0.551)
because it cuts winners before the take-profit ladder can run. That is the wrong
trade for a scalper: more small wins, smaller total P&L.

## Result — take-profit sweep (25-pool sample, 1,001 entries)

| TP plan | n | win | avg return | PF |
| --- | ---: | ---: | ---: | ---: |
| profile (default) | 1,001 | 19.1% | −3.43% | 0.69 |
| global 1.5x/2x | 1,001 | 19.1% | −3.43% | 0.69 |
| single 1.35x | 1,001 | 21.5% | −4.58% | 0.572 |
| quick 1.25x half | 1,001 | 25.1% | −4.67% | 0.545 |
| patient 2x/3x | 1,001 | 17.8% | −2.91% | 0.74 |
| tight trail 1.4x | 1,001 | 20.5% | −4.83% | 0.555 |

No plan reaches a profit factor of 1.0 on these paths. "Patient 2x/3x" is the
least bad (PF 0.74, smallest average loss); "quick 1.25x half" wins most often
but gives up the most per win. This is the important finding: **the exit ladder
itself is negative-expectancy after fees on these real paths.** Adding another
indicator cannot fix a losing exit — the leverage is in entry selectivity and
stop/target calibration, not in stacking more signals.

## Decision

**Do not add any of the eight indicators to the trading desk.** Per the stated
rule — *if it does not give a high win rate, do not add it* — none of RSI, MACD,
ATR, Bollinger, EMA, Fibonacci, Stochastic or ADX cleared the bar on 3,013 real
signal-BUY entries. `desk/signal_engine.py` and `desk/exits.py` are unchanged.

What was added is the **offline test kit** (`desk/indicators.py`,
`desk/indicator_backtest.py`, `scripts/indicator_eval.py`, tests). It changes no
live setting, places no order, and is not imported by the agent loop. It exists
so the next candidate indicator can be judged the same way instead of being
bolted on by feel.

The one lead worth chasing: **RSI < 35 oversold entries** showed a positive
profit factor on both samples but on too few trades. Re-run with a larger pool
count before trusting it:

```bash
python scripts/indicator_eval.py --pools 120 --step 2 --out indicator_eval.json
```

Only if that run shows RSI < 35 with ≥ 30 trades, win lift ≥ 5pp and PF ≥ 1.2
should it be considered for the live entry gate — and even then as a filter on
top of the existing signal, never as a replacement.

## How to reproduce on Termux

```bash
cd ~/solana-desk
python scripts/indicator_eval.py --offline --pools 20      # no network, plumbing check
python scripts/indicator_eval.py --pools 50 --step 2       # real GeckoTerminal OHLCV
python -m pytest tests/test_indicators.py tests/test_indicator_backtest.py -q
```

The real run is paced to stay under GeckoTerminal's free-tier rate limit, so a
50-pool sample takes several minutes. It caches the candles in
`data/indicator_paths.json`; re-run with `--reuse` to skip the fetch.

## Honest limitations

* OHLCV-only: holders, security and narrative are neutral, so this measures the
  price/volume edge, not the full live signal.
* The signal engine's BUY gate is the baseline; the test does not replace it with
  a pure indicator strategy.
* Win rate alone is not enough — that is why profit factor is part of the bar.
  The RSI-overbought exit is the proof: higher win rate, worse P&L.
