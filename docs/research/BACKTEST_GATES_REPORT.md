# Backtest: does the meme scorecard raise the win rate, and which settings matter?

**Date:** 2026-11-13
**Script:** `scripts/backtest_gates.py` (two-phase: `fetch` then `run`)
**Data:** 101 real Solana pools, real 5-minute OHLCV from GeckoTerminal
**Cache:** `backtest_cache.json` (so every number below is reproducible offline)
**Full output:** `backtest_gates.json`

---

## The headline

Two things came out of this, and the first one is uncomfortable.

**1. The old "13% win rate" was a bug, not a result.** `scripts/backtest_100.py`
compared the raw token *price in dollars* against a **$1.03 cost basis** instead
of computing a return. A coin that went from $0.000498 to $0.000509 — a **+2.1%**
trade — was written down as **−100.0%**. Every memecoin priced under a dollar was
therefore recorded as a total loss, and only tokens trading above ~$1 could ever
"win". That is where 13% came from.

Re-scoring the *same 100 trades* with the same entry and exit prices, but as
returns:

| | logged by the old script | true, from the same prices |
|---|---|---|
| wins | 13 / 100 | **53 / 100** |
| win rate | 13.0% | **53.0%** |
| mean return | +9.1% | **+10.0%** |
| median return | −99.8% | **+0.34%** |

The −99.8% median was the token price itself, not a loss. So the desk's exit
rules were never as bad as the report claimed, and the "81% rug rate" was really
"81% of tokens cost less than a dollar".

**2. Once the arithmetic is fixed, entry gating is worth about 25 points of win
rate, and the 4-hour hold cap costs about 17 points.** The meme scorecard, on
this sample, is a tie-breaker — not a filter.

---

## Method, and what it can and cannot see

Every pool is entered **2 hours after the pool was created**, which is the
earliest point where a 2-hour feature window exists. That anchor matters: it
means the age gate is evaluated against the pool's real age, not against the
start of the OHLCV window.

Features are built from `candles[:i]` only — no future price, no future volume.
The forward path is then walked candle by candle through the desk's own
`desk/exits.evaluate()`, with 1% fee and 2% slippage charged on both sides.
Returns are capped at +1000% so one 400x candle cannot define a mean.

Three things cannot be reconstructed from historical data, and I have not
pretended otherwise:

- **Liquidity, market cap, buys/sells/buyers** come from the pool's *current*
  attribute snapshot, not from the entry minute. GeckoTerminal does not serve
  historical liquidity. They are used as a static pool-quality filter.
- **Holders, socials and top-wallet concentration** are unknown, so they are
  passed as `None`. `meme.scorecard()` already treats those neutrally (half
  credit for holders, no credit for socials, no distribution penalty).
- **The deployer investigation** cannot be replayed. The scorecard is therefore
  run twice: once with `dev_score = 0` ("dev unknown") and once with
  `dev_score = 55, legit = True` ("dev passes"). The truth is between them.

Because a coin only reaches the meme seat in live trading if `devcheck` did not
already veto it, the **gated** set uses the "dev passes" scorecard. Scoring it as
"dev unknown" kills 100% of coins and tells you nothing.

---

## The gate ladder

Each rung applies every gate before it. `n` is the number of trades that
survived, out of 101 pools.

| rung | n | win rate | mean | median | profit factor |
|---|---|---|---|---|---|
| 0. no gates (old baseline) | 101 | 16.8% | −0.5% | −6.0% | 0.94 |
| 1. + SCAN floors | 14 | **35.7%** | +9.7% | −25.3% | 1.56 |
| 2. + trades & sell ratio | 12 | **41.7%** | +16.0% | −15.6% | 2.05 |
| 3. + organic buyers | 12 | 41.7% | +16.0% | −15.6% | 2.05 |
| 4. + meme not-bad (≥50) | 8 | 37.5% | +12.8% | −15.6% | 1.86 |
| 5. + community ≥ 35 | 8 | 37.5% | +12.8% | −15.6% | 1.86 |
| 6. + volume still arriving | 5 | 40.0% | +20.3% | −24.7% | 2.18 |

What actually happened, rung by rung:

- **SCAN floors do most of the work.** 16.8% → 35.7% win rate, and expectancy
  flips from −0.5% to +9.7%. The filters that bite are `too_old` (43 pools —
  older than 48h at entry), `mcap_high` (26 — over $25M), `vol` (14 — under
  $3k/h), `chasing` (2), `mcap_low` (2). Only 14 of 101 pools are in the desk's
  actual hunting ground.
- **The trade-count and sell-ratio gate adds another 6 points** (35.7% → 41.7%)
  and pushes profit factor from 1.56 to 2.05 by removing two pools with almost
  no two-way flow.
- **The organic-buyers gate changed nothing** on this sample. No pool that
  reached that rung had more than 4 buys per wallet.
- **The meme scorecard cost 4 points of win rate** (41.7% → 37.5%) and cut the
  sample from 12 to 8. On 8 trades that difference is noise, but it is not
  evidence *for* the scorecard either.
- **The volume-still-arriving gate** lifted mean return to +20.3% on 5 trades.
  Again: directionally sensible, statistically meaningless at n=5.

**The honest reading:** the SCAN floors and the flow gates are the real edge.
The meme scorecard neither helps nor hurts measurably here, because the pools
that survive the first three rungs are already decent.

---

## Does a higher meme score win more?

Score bands on the rung-3 set (SCAN + flow + organic), dev-passes scorecard:

| score band | n | win rate | mean | median | PF |
|---|---|---|---|---|---|
| 30–39 | 9 | 33.3% | +13.0% | −24.7% | 1.78 |
| 40–49 | 3 | 66.7% | +25.0% | +32.6% | 3.27 |
| 50–59 | 8 | 37.5% | +12.8% | −15.6% | 1.86 |
| 60+ | 0 | — | — | — | — |

Directionally the 40–49 band beat the 30–39 band, but with n=3 versus n=9 that
is not a finding. **No pool scored above 60**, because holders, socials and
top-wallet data are missing — the scorecard is capped at roughly 75 in this
reconstruction and usually lands in the 40s.

So: I cannot show you that a high meme score predicts a win. I can only show
that the low bands are worse. Treat the score as a tie-breaker between two
otherwise-equal candidates, not as a filter.

---

## Settings sweep (all 101 pools)

Exit rules act on whatever you bought, so the sweep is run on the full 101-trade
set — that is the statistically meaningful universe.

| parameter | best value | win rate | mean | note |
|---|---|---|---|---|
| `stop_pct` | 0.60 | 18.8% | +0.2% | 0.20 gives 16.8% / −0.5% |
| `tp1_at` | 1.5 (current) | 16.8% | −0.5% | 1.2 and 1.3 are worse |
| `tp2_at` | 5.0 | 16.8% | −0.4% | barely moves |
| `trail_pct` | 0.15 | 16.8% | −0.4% | barely moves |
| `timeout_hours` | 6.0 | 19.8% | −0.4% | 1.0 gives 12.9% |
| `max_hold_hours` | 4.0 (current) | 16.8% | −0.5% | 48h gives 11.9% *with* a 20% stop |
| `tp1_frac` | 0.75 | 16.8% | −0.4% | barely moves |

**No single exit setting rescues an ungated entry.** The best mean return
available from any one-parameter change is **+0.2%**, against a −0.5% baseline.
That is the whole point: settings are second-order, gating is first-order.

---

## Combinations — where the real lever is

Single-parameter sweeps hide interactions. These are the combinations that
matter, on all 101 pools:

| configuration | n | win rate | mean | median | PF |
|---|---|---|---|---|---|
| current (stop 20%, hold 4h) | 101 | 16.8% | −0.5% | −6.0% | 0.94 |
| no stop, hold 4h | 101 | 18.8% | −0.0% | −6.0% | 1.00 |
| no stop, hold **12h** | 101 | **33.7%** | +0.6% | −6.1% | 1.05 |
| no stop, hold 48h | 101 | 34.6% | +0.7% | −6.9% | 1.04 |
| stop 40%, hold 48h | 101 | 34.6% | **+2.2%** | −6.9% | 1.15 |
| stop 40%, hold to data end | 101 | 36.6% | **+3.1%** | −8.7% | 1.18 |
| stop 25%, tp1 1.3x, hold 12h | 101 | 31.7% | −0.9% | −6.6% | 0.91 |

Read the first three rows together. Holding the same trades for **12 hours
instead of 4 nearly doubles the win rate** (18.8% → 33.7%). Memecoins routinely
draw down 20–30% and then recover; a 4-hour clock closes them before the
recovery, and a 20% stop closes them even earlier.

Widening the stop to 40% *and* holding 48h gives the best mean return of
anything tested (+2.2%), and letting winners run to the end of the data gives
+3.1%.

On the 5 fully-gated pools the same pattern is extreme (40% → 100% win rate),
but n=5 is noise and I would not act on it.

---

## What I would change, and what it costs

**Change 1 — widen the scalp stop from 20% to 25–30% and the hold from 4h to
12h.** On this data that is worth roughly +15 points of win rate and +2.7 points
of mean return per trade. The cost is real: with `max_open_positions = 6`, a
12-hour hold means roughly two entries a day instead of six, so you will see far
fewer trades. That is a throughput-for-quality trade, and it is your call.

**Change 2 — keep the SCAN floors exactly as they are.** They are the single
biggest source of edge in this whole exercise. Do not loosen `max_age_hours`,
`max_mcap_usd` or `min_volume_h1_usd` to "get more trades".

**Change 3 — stop treating the meme score as a veto.** `meme_kill()` currently
rejects anything under 50, and with unknown holders/socials that rejects almost
everything. Either feed it real holder and social data, or demote it to a
ranking signal. As it stands it is the most expensive gate per unit of edge.

**Change 4 — do not trust the old report.** `BACKTEST_100_REPORT.md` should be
read as "the exit rules were measured wrong". The corrected picture is in this
file.

---

## Reproduce it

```bash
python scripts/backtest_gates.py fetch --pools 120   # ~25 min, rate-limited
python scripts/backtest_gates.py run                 # instant, offline
```

The `fetch` phase is slow on purpose: GeckoTerminal's free tier is roughly 30
requests a minute and the sandbox IP is shared, so the fetcher retries with a
1.2s backoff and saves the cache every 5 pools. `run` never touches the network,
so you can re-run every experiment as many times as you like.

## Limitations, stated plainly

- 101 pools is a small sample. The gate ladder's 41.7% has a 95% confidence
  interval of roughly ±28 points.
- Liquidity and market cap are a current snapshot, not point-in-time, so the
  SCAN rung is a *pool-quality* filter rather than a faithful replay of what the
  scanner saw.
- Holders, socials and top-wallet concentration are missing, which caps the
  meme score around 75 and makes the scorecard look worse than it will in live
  trading once those feeds are on.
- Entry is fixed at 2h after creation. The desk can enter earlier; this backtest
  cannot, because it needs a feature window.
- `runner_max_minutes = 180` caps every runner 3 hours after TP1 regardless of
  `max_hold_hours`, so the long-hold rows above are driven by positions that
  never reached TP1.
