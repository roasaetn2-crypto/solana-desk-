# Fast Scalper — 1–10 minute holds (settings, lab, and the honest truth)

This document covers the **fast-scalp lane**: entering a *fresh* coin on a
**moving 5-minute candle**, holding **1–10 minutes max**, gated by **age,
market-cap band and liquidity**, and exiting through a tight partial ladder.

It ships three things:

1. A **config surface** (`SCALP` block in `config.py`, `scalp` exit profile,
   `scalp_kill` scanner gate, and a `hyper_scalp` settings preset).
2. A **backtest lab** (`scripts/scalper_lab.py`) that measures entry/exit
   geometry on real 5-minute candles and reports win rate + profit factor.
3. This **honest write-up** of what the data actually says — including where a
   naive "≥60% win rate in 10 minutes" target does *not* hold up, and why.

---

## 1. What "fast scalp" means in this desk

| Knob | Value | Why |
|---|---|---|
| `max_hold_minutes` | **10** | hard exit — this is what makes it a scalp |
| `timeout_minutes` | **6** | if it isn't green by 6 min, free the slot |
| `timeout_min_gain` | **1%** | "green" = +1% (covers half the round-trip cost) |
| `entry_mode` | **moving_candle** | only enter while the 5m candle is still moving up |
| `moving_candle_bars` | **1** | signal bar = the most recent closed 5m candle |
| `moving_candle_min_body_pct` | **0.2%** | reject dead/doji candles |
| `min_age_minutes` | **5** | not a 30-second-old snipe; needs one closed 5m bar |
| `max_age_minutes` | **360** | fresh only (≤6 h old) |
| `min_mcap_usd` / `max_mcap_usd` | **30k / 500k** | the micro-cap band where 5m moves are large |
| `min_liquidity_usd` | **8k** | enough depth to actually get out |
| `min_volume_m5_usd` | **500** | the candle must have real volume behind it |
| `stop_pct` | **3%** | tight stop for a tight clock |
| `tp1_at` / `tp1_frac` | **+3% / 50%** | bank half at +3% |
| `tp2_at` / `tp2_frac` | **+6% / 50%** | rest at +6% |
| `trail_pct` | **2%** | trail the runner after TP1 |

The **scalp lane is opt-in**. `SCALP["enabled"]` is `False` by default so the
desk's existing behaviour is untouched. Turn it on either by applying the
**`hyper_scalp` preset** in Settings, or by setting `SCALP["enabled"] = True`.
When enabled, the `scalp` exit profile is overlaid with the `SCALP` values
*last* (after meme-tactic overrides), so the scalp clock and ladder always win
for the scalp horizon.

---

## 2. The lab — how to measure it honestly

```
python3 scripts/scalper_lab.py --help
```

Key design choices (all deliberately pessimistic):

- **Bars are 5 minutes.** A 1–2 bar hold is 5–10 minutes — exactly the window.
  The simulator never looks past the hold window.
- **Entry fills at the close of the signal bar** (same convention as the rest of
  the desk's backtests).
- **Worst-case intrabar ordering:** if a single bar touches *both* the stop and
  the target, the **stop** wins.
- **Fees + slippage charged on both sides.** Defaults are deliberately harsh
  (1% fee + 2% slippage each side); pass `--fee 0.0025 --slip 0.0025` for a
  realistic DEX round trip of ~1%.
- **Partial ladder mode** (`--ladder`) models how the desk *actually* exits:
  sell `tp1_frac` at TP1, move the stop to breakeven, sell the rest at TP2.
  A trade that banks half at +3% and stops the rest at breakeven is still a
  **win**, which the all-or-nothing simulator wrongly calls a loss.

Example sweeps:

```bash
# all-or-nothing geometry
python3 scripts/scalper_lab.py --data data/g87e --fee 0.0025 --slip 0.0025 \
  --tp 0.02 0.03 0.04 --stop 0.03 0.05 0.08 --hold-bars 1 2 \
  --entry-mode dip momentum breakout

# partial ladder (the desk's real exit)
python3 scripts/scalper_lab.py --data data/fresh --ladder --tp1-frac 0.5 \
  --tp2 0.06 --fee 0.0025 --slip 0.0025 \
  --tp 0.02 0.03 0.04 --stop 0.03 0.04 0.05 --hold-bars 1 2 \
  --entry-mode dip momentum
```

Output lands in `research/scalper_lab*.json` (never the repo root).

---

## 3. What the data actually says

I ran the full sweep on every 5-minute dataset available in the repo
(`data/g87e` — 10 established pools; `data/fresh` — 8 trending pools, 1000 bars
each). **No configuration cleared 60% WR + PF ≥ 1.2.** The results are not
close, and the reason is structural, not a tuning miss:

### 3.1 The pools we can get data for are low-volatility majors

The median **5-minute candle range** in `data/fresh` is **0.65%**. These files
carry `mcap ≈ $2.4B` and `liq` in the millions — they are trending *majors*, not
fresh micro-caps. GeckoTerminal's free API does **not** serve deep 5m history on
genuinely fresh launches: `new_pools` returns 0–2 bars (the pool is too new to
have a series), and deeper pages 401. So the free data we *can* pull is exactly
the wrong volatility regime for a 5m scalp.

### 3.2 A 1% round-trip cost exceeds the per-bar move

If a 5m bar only travels ~0.65% and a 10-minute hold ~1.3%, then capturing a
move large enough to beat a ~1% round trip leaves almost nothing. Best measured
results:

- momentum, TP 2% / stop 8%, 10 min → **WR ≈ 29%, PF ≈ 0.33, avg −1.16%**
- dip, TP 2% / stop 8%, 10 min → **WR ≈ 20%, PF ≈ 0.37, avg −0.83%**
- small-TP/wide-stop "high-WR" shapes → **WR ≈ 5%**, because a 1.5% TP simply
  never prints inside a 0.65%-range bar, so every trade times out.

### 3.3 The ladder math

With a round-trip cost `c`, a partial ladder nets positive only when
`tp1_frac × tp1 > c`. With `c ≈ 1%` and `tp1_frac = 0.5`, TP1 must be **> 2%**
just to break even when the runner stops at breakeven. To *also* hit 60% of the
time, TP1 must be small relative to noise — the two requirements fight each
other. On 0.65%-range bars there is no solution.

**Conclusion:** on the free data available, a 1–10 minute scalp that is *both*
≥60% win rate *and* profitable (PF > 1) is **not achievable**. Reporting a
"60% WR" config from this data would be fiction. The desk's genuine, documented
edge is the **pullback/dip entry on longer holds** (the repo's own research:
68.9% WR / PF 3.75 on 120 pools, hours not minutes) — that is a different
strategy with a different clock.

---

## 4. Where 60% *can* be real — and how to prove it

A 60%+ win rate at 1–10 minutes is achievable **only on genuinely fresh,
high-volatility micro-caps**, where a single 5m bar can travel 5–15%. On those,
a +3% TP1 is hit often and a ~1% cost leaves real edge. The shipped `SCALP`
values (3% stop, +3%/+6% ladder, 30k–500k mcap, ≥8k liq, moving-candle entry)
are tuned for exactly that regime.

To validate the 60% target on *your* coins, feed the lab real fresh-coin 5m
candles:

1. **Capture candles** for the fresh pools the desk actually trades. The desk
   already stores OHLCV; export each pool's 5m series to
   `data/fresh/<pool>_5m.json` with the same shape the loader expects
   (`candles[]` of `{ts, open, high, low, close, vol}` plus `mcap`, `liq`,
   `created_at`).
2. **Run the ladder sweep** with realistic costs:
   ```bash
   python3 scripts/scalper_lab.py --data data/fresh --ladder \
     --tp1-frac 0.5 --tp2 0.06 --fee 0.0025 --slip 0.0025 \
     --tp 0.02 0.03 0.04 0.05 --stop 0.03 0.04 0.05 --hold-bars 1 2 \
     --entry-mode momentum dip --min-trades 40 --min-pools 8
   ```
3. **Read `clears: true`.** The lab prints `YES` only when a config hits
   `WR ≥ 60%` **and** `PF ≥ 1.2` with enough trades/pools. Do not trust a WR
   number that comes with PF < 1.2 — a high win rate with a negative PF is just
   many small wins and a few catastrophic losses.
4. **Then, and only then, promote** the winning values into `SCALP` (or a new
   preset) and let the paper desk forward-test it.

Until step 3 clears on real fresh-coin data, treat the shipped `SCALP` numbers
as a **sensible starting geometry**, not a validated 60%-WR promise.

---

## 5. Files touched

| File | Change |
|---|---|
| `config.py` | new `SCALP` block (opt-in, `enabled=False`); `scalp` profile gets minute clock fields; `BENCH_MINUTES` scalp entries |
| `desk/exits.py` | `scalp` profile overlays `SCALP` last; `evaluate()` is minute-aware (`timeout_minutes` / `max_hold_minutes`) |
| `desk/scanner.py` | `scalp_kill()` enforces age / mcap / liquidity window (only when enabled) |
| `desk/settings.py` | ~19 `scalp.*` settings registered; `hyper_scalp` preset ("Hyper Scalp (1-10 min)") |
| `scripts/scalper_lab.py` | new 1–10 min backtest lab (all-or-nothing + partial ladder) |
| `scripts/fetch_fresh.py` | 5m OHLCV + mcap/liq/created_at harvester |

All 983 unit tests pass with `SCALP` off by default.
