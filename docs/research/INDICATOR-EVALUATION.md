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
* **Validation sample (the deciding run):** 99 real pools, 87,210 bars of
  5-minute candles, **5,974 signal-BUY entries** — roughly double the primary
  sample, used to test whether the RSI < 35 lead was real or small-sample noise.
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

## Result — 120-pool validation (the deciding run)

The 25-pool sample had thrown up one tantalising number: RSI < 35 with PF 1.35.
That is exactly the kind of result that gets an indicator bolted onto a live desk
by feel, so it was re-tested on a much larger draw. **99 pools, 87,210 bars,
5,974 signal-BUY entries** — and the lead evaporated.

| indicator | n | win | lift | PF | verdict |
|---|---|---|---|---|---|
| **baseline (signal BUY only)** | 5,974 | 14.8% | — | 0.492 | — |
| **RSI < 35 (oversold)** | 43 | 20.9% | +6.2pp | **0.737** | DO-NOT-ADD (PF < 1.2) |
| Fib position 0.382–0.786 | 2,457 | 14.6% | −0.1pp | 0.554 | DO-NOT-ADD |
| EMA stack + ADX trend | 2,182 | 15.7% | +0.9pp | 0.453 | DO-NOT-ADD |
| ADX ≥ 20 & +DI > −DI | 3,137 | 15.4% | +0.6pp | 0.502 | DO-NOT-ADD |
| EMA 9>21>50 stack | 3,003 | 15.5% | +0.7pp | 0.434 | DO-NOT-ADD |
| ATR 0.4%–20% of price | 5,674 | 15.2% | +0.5pp | 0.456 | DO-NOT-ADD |
| Bollinger pct_b ≥ 0.5 | 4,965 | 14.8% | +0.1pp | 0.478 | DO-NOT-ADD |
| RSI < 70 (not overbought) | 5,202 | 14.3% | −0.5pp | 0.492 | DO-NOT-ADD |
| MACD hist > 0 | 4,760 | 14.6% | −0.2pp | 0.480 | DO-NOT-ADD |
| Bollinger pct_b < 0.25 | 285 | 13.3% | −1.4pp | 0.489 | DO-NOT-ADD |
| Stoch %K < 25 | 263 | 14.4% | −0.3pp | 0.497 | DO-NOT-ADD |
| Stoch %K > %D & %K < 80 | 2,155 | 14.8% | +0.0pp | 0.492 | DO-NOT-ADD |
| RSI 35–60 (reclaim) | 3,522 | 13.3% | −1.4pp | 0.501 | DO-NOT-ADD |
| EMA stack + MACD bull | 2,441 | 15.5% | +0.7pp | 0.438 | DO-NOT-ADD |
| RSI<70 + MACD bull | 4,005 | 13.9% | −0.8pp | 0.475 | DO-NOT-ADD |
| Fib pocket + RSI<70 | 2,359 | 14.2% | −0.5pp | 0.533 | DO-NOT-ADD |

**RSI < 35 clears the trade count (43 ≥ 30) and the win-lift bar (+6.2pp ≥ 5pp),
but fails the profit-factor bar outright: 0.737 against a required 1.2.** It also
keeps only 0.7% of entries — 43 trades out of 5,974 — so it would starve the desk
to a near-standstill while still losing money. Its average return of −1.06% is
less bad than the baseline's −4.69%, but "loses less per trade on 0.7% of
opportunities" is not an edge worth a live gate.

The pattern across every single indicator is the same: win-rate lift is noise
around zero, and profit factor sits far below 1.0 in every row. No combination
rescued it either — the two best combos (`EMA stack + ADX trend`, `EMA stack +
MACD bull`) reached +0.9pp and +0.7pp, both inside the noise band.

Indicator exits were re-tested on the same 5,974 entries and did no better:
`exit RSI ≥ 75` gave +4.1pp win lift but PF 0.508 (still below baseline's 0.492
only marginally, and below the 1.2 bar); `exit MACD hist < 0` collapsed to PF
0.293 and −5.4pp; `exit Stoch %K > 85` to PF 0.368.

The take-profit sweep on the validation sample confirmed the earlier finding and
made it starker — **no plan reaches PF 1.0**:

| plan | n | win | avg | PF |
|---|---|---|---|---|
| profile (default) | 5,974 | 14.8% | −4.693% | 0.492 |
| global 1.5x/2x | 5,974 | 14.8% | −4.693% | 0.492 |
| single 1.35x | 5,974 | 15.9% | −4.669% | 0.487 |
| quick 1.25x half | 5,974 | 18.0% | −4.712% | 0.470 |
| patient 2x/3x | 5,974 | 13.8% | −4.787% | 0.488 |
| tight trail 1.4x | 5,974 | 15.5% | −4.768% | 0.479 |

Note that `patient 2x/3x`, which was the least-bad plan on the 25-pool sample,
is *not* the least-bad here — the ranking reshuffles between samples. That
reshuffling is itself the lesson: these differences are inside the noise, and
picking a TP plan off a single backtest is curve-fitting. The stable, repeatable
finding across all three samples is that **the exit ladder is negative-expectancy
after fees and slippage on these paths**, and no indicator or TP plan fixes that.

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

The one remaining lead, **RSI < 35**, was then re-tested on the 120-pool
validation draw as agreed, on the explicit condition that it needed ≥ 30 trades,
≥ 5pp win lift **and PF ≥ 1.2** before being wired in. It failed the
profit-factor condition decisively: **43 trades, 20.9% win, +6.2pp lift, PF
0.737**. The promising 25-pool PF of 1.35 was small-sample noise. Nothing was
wired into the entry gate, and nothing should be.

If you want to re-test a future candidate, the command is:

```bash
python scripts/indicator_eval.py --pools 120 --step 2 --out indicator_eval.json
```

The rule stays the same: ≥ 30 trades, ≥ 5pp win lift, PF ≥ 1.2, and never worse
than baseline — and even then it goes in as a filter on top of the existing
signal, never as a replacement.

## How to reproduce on Termux

```bash
cd ~/solana-desk
python scripts/indicator_eval.py --offline --pools 20      # no network, plumbing check
python scripts/indicator_eval.py --pools 50 --step 2       # real GeckoTerminal OHLCV
python scripts/indicator_eval.py --pools 120 --step 2 --reuse  # the deciding run (uses cache)
python -m pytest tests/test_indicators.py tests/test_indicator_backtest.py -q
```

The real run is paced to stay under GeckoTerminal's free-tier rate limit, so a
50-pool sample takes several minutes and the 120-pool run took about 30 minutes
(most of it waiting out 429 backoffs during collection). It caches the candles in
`data/indicator_paths.json`; re-run with `--reuse` to skip the fetch.

## Honest limitations

* OHLCV-only: holders, security and narrative are neutral, so this measures the
  price/volume edge, not the full live signal.
* The signal engine's BUY gate is the baseline; the test does not replace it with
  a pure indicator strategy.
* Win rate alone is not enough — that is why profit factor is part of the bar.
  The RSI-overbought exit is the proof: higher win rate, worse P&L.
