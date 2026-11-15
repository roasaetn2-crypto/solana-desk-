# Where indicators are (and are not) used

An audit of every indicator in the repo: what computes it, what reads it, and
whether it can change a trade. Written after a review that found two switches
that look live but are not, and one measured rule that runs on the wrong bars.

Short version: **the live desk uses zero classic technical indicators.** RSI,
MACD, ATR, Bollinger, EMA, Stochastic, ADX and Fibonacci exist in
`desk/indicators.py` and are never imported by the trading loop. What actually
gates a trade is market microstructure read straight off DexScreener plus
on-chain facts.

## The live path, in order

Every candidate walks these seats. Only the seats marked **gate** can stop a
trade; the rest score or annotate.

| Seat | File | Input | Gates? |
| --- | --- | --- | --- |
| Scanner | `desk/scanner.py`, `desk/pumpportal.py` | new pools, DexScreener pairs | no |
| Chain facts | `desk/chain.py` | mint/freeze authority, supply, top-wallet % | **gate** |
| Dev investigation | `desk/devcheck.py` | creator history, dev holding, snipers | **gate** |
| Meme value | `desk/meme.py` | narrative, community, distribution | score |
| Analyst | `desk/analyst.py`, `desk/brain.py` | the facts above, to an LLM | **gate** (`analyst_min_score`) |
| Signal engine | `desk/signal_engine.py` | DexScreener fields | **gate** |
| Meme tactics | `desk/meme_tactics.py` | 20-bar high, 5m change | **gate** (pullback only) |
| Security | `desk/signals.py` | RugCheck-style flags | **gate** |
| Risk engine | `desk/risk.py` | book, daily P&L, open risk | **gate** |
| Paper broker | `desk/broker.py` | the ticket | fills |

## What `signal_engine.py` actually measures

Six components, each scored −1..+1, blended by weight into a composite, then
mapped to a 0–100 confidence. None of them is a price indicator.

| Component | Weight | What it reads | Source field |
| --- | ---: | --- | --- |
| momentum | 0.24 | 5m and 1h % change, plus "volume still arriving" (`volume_m5 × 12 ÷ volume_h1`) | `change_m5`, `change_h1`, `volume_m5/h1/h6` |
| flow | 0.22 | buys vs sells ratio, and buys per distinct buyer (concentration) | `buys_h1`, `sells_h1`, `buyers_h1` |
| liquidity | 0.16 | pool depth in USD, and volume/mcap turnover | `liquidity_usd`, `mcap_usd`, `volume_h1` |
| holders | 0.14 | top-wallet concentration from chain facts | `top_wallet_pct`, `top_10_pct` |
| narrative | 0.12 | meme scorecard total | `scorecard.total` |
| safety | 0.12 | security flags, dev score, market regime | `security`, `dev`, `regime` |

Two thresholds decide the action: `min_confidence` 60 and
`min_win_probability` 0.52, with `sell_confidence` 68 for the bearish exit.
`require_bullish` means a bearish read can never be bought even at high score.

## What the exit engine actually measures

`desk/exits.py` is a price ladder, not an indicator. In evaluation order:

1. stop price (fixed % below entry, or breakeven after TP1) — fills *at* the stop, not at the gap
2. runner trail — `price ≤ peak × (1 − trail)`
3. TP2, then TP1 — fixed multiples of entry
4. volume decay — `volume_h6 ÷ (volume_h24 ÷ 4) < ratio`
5. timeout and max hold — clock only

`plausible()` rejects any print outside `[entry × min_mark_mult, entry × max_mark_mult]`
before it is allowed to mark the book or trigger a sell. That is the guard that
stops a decimals mismatch from liquidating a position at 284x.

## The eight indicators: tested, rejected, kept offline

`desk/indicators.py` implements RSI(14), MACD(12,26,9), ATR(14), Bollinger(20,2),
EMA(9/21/50), Stochastic(14,3,3), ADX(14) and Fibonacci retracement, all in pure
Python with no numpy/pandas/TA-Lib. The maths is cross-checked against
`Bitvested/ta.py` and reproduces Wilder's published RSI table exactly.

They were then measured on real Solana OHLCV before being allowed anywhere near
the live desk. The full result is in `docs/research/INDICATOR-EVALUATION.md`;
the deciding run was **99 pools, 87,210 five-minute bars, 5,974 signal-BUY
entries**:

| Filter | n | win | lift | PF | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| baseline (no filter) | 5,974 | 14.8% | — | 0.492 | — |
| RSI < 35 | 43 | 20.9% | +6.2pp | 0.737 | fail (PF) |
| RSI < 70 | 5,202 | 14.3% | −0.5pp | 0.492 | fail |
| MACD hist > 0 | 4,760 | 14.6% | −0.2pp | 0.480 | fail |
| EMA 9>21>50 stack | 3,003 | 15.5% | +0.7pp | 0.434 | fail |
| ADX ≥ 20 & +DI > −DI | 3,137 | 15.4% | +0.6pp | 0.502 | fail |
| Bollinger %B ≥ 0.5 | 4,965 | 14.8% | +0.1pp | 0.478 | fail |
| Stoch %K < 25 | 263 | 14.4% | −0.3pp | 0.497 | fail |
| Fib 0.382–0.786 | 2,457 | 14.6% | −0.1pp | 0.554 | fail |

The bar was fixed before the numbers were read: ≥30 trades, ≥5pp win lift, and
PF ≥ 1.2. RSI < 35 is the only one that lifted win rate and cleared the trade
count — and it failed profit factor outright (0.737), while keeping 0.7% of
entries. Its earlier PF 1.35 on a 25-pool sample was small-sample noise, which
is exactly why the larger validation run existed.

Indicator *exits* did no better: `exit RSI ≥ 75` raised win rate +4.1pp but cut
PF to 0.508, because it sells winners before the ladder can run. That is the
wrong trade for a scalper — more small wins, less total P&L.

Verified at runtime: importing `app` does not pull `desk.indicators` or
`desk.indicator_backtest` into `sys.modules`. They are offline tools only.

## Two switches that look live but are not

Both are in `config.py` `MEME`, both default `False`, and both are **inert even
when set to `True`** — because the value they read is never produced.

### `MEME["atr_stop"]` and `MEME["vol_ceiling"]`

`meme_tactics.apply()` reads `facts["atr_pct"]`:

```python
if blocked is None and on["vol_ceiling"]:
    ok, why = vol_ok(f.get("atr_pct"))      # None -> returns (True, "unknown")
...
if on["atr_stop"]:
    stop = atr_stop_pct(f.get("atr_pct"))   # None -> returns None, stop unchanged
```

Nothing in the live loop ever sets `atr_pct`. `chain.facts()` does not compute
it, and `desk/indicators.py` is not imported. So:

* `vol_ok(None)` returns `(True, "unknown")` — the ceiling never blocks.
* `atr_stop_pct(None)` returns `None` — the stop is never widened.

Confirmed by running `meme_tactics.apply()` with both flags on and an empty
facts dict: action stayed `BUY`, `stop_pct` stayed `0.30`, `tactic_block` was
`None`. Feeding `{"atr_pct": 0.35}` blocks correctly and `{"atr_pct": 0.05}`
sets the stop to 0.08, so the code path is fine — it is simply never fed.

This matters because the measurement said ATR rules do not help anyway
(`ATR stop` was *worse*: 13.1–14.5% win vs 15.4% baseline; volatility ceiling
was flat). So the honest fix is to delete the two switches rather than wire
them, unless you want them for the shadow report.

### `high_20` is computed on 15-second bars, not 5-minute

The pullback rule was measured as "20% below the 20-bar high" on **5-minute**
candles (`scripts/pool_harvest.py` fetches `ohlcv/minute`; the eval harness is
documented as 5-minute). That is a 100-minute lookback.

The live loop asks for candles with no timeframe:

```python
cs = candle_chart.series(t["mint"], session=self.session)
```

`series()` defaults to `tabs_for()` with no horizon and no age, which returns
`["15s", "30s"]`, so `tf` becomes `"15s"`. Twenty 15-second bars is **5
minutes**, not 100. And if the mint has fewer than `MIN_TICK_BARS` (8) local
buckets, `series()` falls back to 1-minute network candles — so the window is
5 minutes or 20 minutes depending on how long the desk has been watching the
coin, and it is never the 100 minutes that was measured.

Verified at runtime: `tabs_for()` → `['15s','30s']`, and a series built from 60
recorded ticks reports `tf: 15s`, `source: ticks`.

The practical effect is that the live gate demands a 20% drop inside 5 minutes
instead of inside 100 minutes. On a fresh memecoin that is a much rarer event
than the measured rule, so the desk sits out far more often than the backtest
implies — and the 68.9% win rate on 11 pools was earned on a rule the live desk
is not running.

The fix is one argument: `candle_chart.series(t["mint"], "1m", session=...)`
with 100 bars, or fetch 5-minute candles directly. Whatever is chosen, the
backtest and the live gate must use the same bar size or the measurement means
nothing.

## What the dashboard shows

No indicator is displayed anywhere in `web/`. There is no RSI readout, no MACD
histogram, no Bollinger band. The chart tabs are 15s/30s for fresh coins and
15m/1H/4H for older ones, drawn from `desk/chart.py` — a price chart, not an
indicator panel. The decision row shows the signal action, confidence, win
probability, meme score, dev score and ticket; the position card shows entry,
last, cost, quantity, held time and the exit ladder.

## If you want to test a new indicator

The harness is already there and takes one command:

```bash
python3 scripts/indicator_eval.py --pools 100
```

It pulls real GeckoTerminal OHLCV, replays every signal-BUY entry, and prints
the same verdict table with the same bar. Do not wire an indicator into
`signal_engine.py` or `exits.py` until it clears ≥30 trades, ≥5pp win lift and
PF ≥ 1.2 on a sample large enough that leave-one-pool-out does not collapse it.
