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
| `timeout_minutes` | **5** | if it isn't green by 5 min, free the slot |
| `timeout_min_gain` | **1%** | "green" = +1% (covers half the round-trip cost) |
| `entry_mode` | **moving_candle** | only enter while the 5m candle is still moving up |
| `moving_candle_bars` | **1** | signal bar = the most recent closed 5m candle |
| `moving_candle_min_body_pct` | **0.3%** | reject dead/doji candles |
| `min_age_minutes` / `max_age_minutes` | **1 / 60** | a launch is tradeable in its first hour |
| `min_mcap_usd` / `max_mcap_usd` | **10k / 100k** | the launch band where 5m moves are large |
| `min_liquidity_usd` / `max_liquidity_usd` | **5k / 20k** | enough depth to exit, shallow enough to still move |
| `min_volume_m5_usd` | **500** | the candle must have real volume behind it |
| `stop_pct` | **5%** | 5–20k liquidity means 2–4% slippage; 3% is noise |
| `tp1_at` / `tp1_frac` | **+4% / 50%** | bank half at +4% |
| `tp2_at` / `tp2_frac` | **+8% / 50%** | rest at +8% |
| `trail_pct` | **3%** | trail the runner after TP1 |

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

## 4b. Running the scalper in Termux (no Render dependency)

Render's free plan sleeps after ~15 min of no traffic and can expire; Termux on
your phone does not. The desk is designed to run locally at
`http://127.0.0.1:8000`, and the scalp lane is now switchable from Termux
without editing code.

### One-time (already cloned?)
```bash
cd ~/solana-desk-        # or wherever the checkout is; the helpers find it
git pull --ff-only
```

### Turn the scalper ON (persistent, survives restarts/reboot)
```bash
bash ~/solana-desk-/termux/scalp.sh on
```
This writes `$DESK_DIR/.desk_env` with `SCALP_ENABLED=1`, restarts the
supervisor, and health-checks the desk. `termux/start-desk.sh` sources
`.desk_env` on every start, so a respawn or Termux:Boot keeps it on.

Tune in the same command — any value can be overridden:
```bash
bash ~/solana-desk-/termux/scalp.sh on TP1=1.025 STOP=0.03 HOLD=8 TIMEOUT=5 \
     MINAGE=5 MAXAGE=360 MINMCAP=30000 MAXMCAP=500000 MINLIQ=8000
```
Keys: `TP1 TP2 STOP TP1FRAC HOLD TIMEOUT ENTRY MINAGE MAXAGE MINMCAP MAXMCAP
MINLIQ MINVOL TRAIL`.

### Check it / turn it OFF
```bash
bash ~/solana-desk-/termux/scalp.sh status   # shows .desk_env + live health
bash ~/solana-desk-/termux/scalp.sh off      # back to the normal desk
```

`status` reads `/api/health`, which now reports a `scalp` block, so you can
confirm the *running* process actually loaded the lane — not just that a file
exists.

### Normal update loop
```bash
bash ~/solana-desk-/termux/update-all.sh        # pull + deps + ledger + restart + health
```
Then open `http://127.0.0.1:8000/?v=2` and hard-refresh.

### Why this matters for "Render expired"
- The scalper's edge is **in-and-out in 1–10 minutes**; it cannot tolerate a box
  that sleeps mid-position. Termux keeps the process and the wake-lock alive.
- `SCALP` gates only apply to coins **younger than `scalp_max_age_hours` (6h)**,
  and with the default `trading.style = both` those coins automatically get the
  `scalp` horizon → the 3% / +3% / +6% / 10-min ladder. No style change needed.
- If you still want Render as a backup, keep it as a *second* paper desk — but
  the authoritative scalper run is the Termux one at `127.0.0.1:8000`.

---

## 4c. The `hyper_scalp` preset card (what it sets)

| Setting | Value |
|---|---|
| `trading.style` | `scalp` |
| `scan.max_age_hours` / `exits.scalp_max_age_hours` | `1` / `1` |
| `scalp.max_hold_minutes` / `scalp.timeout_minutes` | `10` / `5` |
| `scalp.min_age_minutes` / `scalp.max_age_minutes` | `1` / `60` |
| `scalp.min_mcap_usd` / `scalp.max_mcap_usd` | `10,000` / `100,000` |
| `scalp.min_liquidity_usd` / `scalp.max_liquidity_usd` | `5,000` / `20,000` |
| `scalp.min_volume_m5_usd` | `500` |
| `scalp.entry_style` | `moving_candle` (1 bar, body ≥ 0.3%) |
| `scalp.stop_pct` | `5%` |
| `scalp.tp1_at` / `tp1_frac` | `1.04` / `50%` |
| `scalp.tp2_at` / `tp2_frac` | `1.08` / `50%` |
| `scalp.trail_pct` | `3%` |
| `agent.cycle_seconds` | `20` |

> Note: the setting key is `scalp.entry_style` (not `..._mode`) because the
> settings guard treats any `*_mode` key as owner-only. The `SCALP` dict key is
> still `entry_mode`.

**Risk note.** 10k–100k mcap is the band the desk journal historically bled on
for *hours* holds (16 trades, 6% WR, −$17.55); 100k–500k was the edge. A 1–10
minute scalp has no other band with enough movement to beat its round trip, so
this is a deliberate, paper-only, small-size bet — and the 20k liquidity cap
mechanically limits position size. Let the journal prove it before real money.

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
| `config.py` | `SCALP_<KEY>` env overrides (flip the lane on without editing code) |
| `termux/scalp.sh` | new: `on` / `off` / `status`, writes `.desk_env`, restarts + health-checks |
| `termux/start-desk.sh` | sources `.desk_env` on every start (persists across respawn/reboot) |
| `app.py` | `/api/health` now reports a `scalp` block |

All 983 unit tests pass with `SCALP` off by default.

---

# 6. The three bands: scalp / swing / both

Before this change the desk had **three gate systems that contradicted each
other**, and one of them made the fast lane impossible to use.

## 6.1 The bug that mattered

`free_kill()` ran the scalp gate first, then applied `SCAN`'s mcap band
(100k–2M) to *everything*. So a fresh launch at 45k mcap — exactly what the
scalp lane is for — was killed as `mcap` even with `SCALP.enabled = True`. The
10k–100k band was unreachable. The lane was on, the settings were right, and
nothing could ever trade.

Two more, smaller:

- **The 5m-volume trap.** Fresh coins were judged on `min_volume_h1_usd = 3000`
  (an hour of volume) when they were three minutes old. They died at `volume`
  before they had lived long enough to accumulate it.
- **The 60min–6h dead zone.** `horizon_for()` used `scalp_max_age_hours = 6` to
  pick the exit ladder, while the scalp gate refused anything over 60 minutes.
  A 90-minute coin got *scalp exits* (20% stop) while being ineligible for the
  scalp lane. The exits and the entry filter disagreed about what a scalp was.

## 6.2 The model now

Two bands, each with its own four gates, chosen **by age**:

| | **scalp** | **swing** |
|---|---|---|
| age | 1–60 min | 1 min–48 h |
| mcap | $10k–$100k | $100k–$2M |
| liquidity | $5k–$20k | $3k+, no ceiling |
| volume | $500 in 5m | $3,000 in 1h |
| trades | — (too young to judge) | 30 in 1h |
| hold | 5 min timeout, 10 min max | 3 h timeout, 4 h max |
| stop / TP1 / TP2 | 5% / 1.04 / 1.08 | 30% / 1.50 / 2.00 |

**`trading.style`** decides which lanes are allowed to open:

- `scalp` → only the scalp band trades; the swing lane is closed
- `swing` → only the swing band trades; the scalp lane is closed
- `both` → both are live at once, routed by age

A fresh coin that is **too big or too deep** for the scalp band (say 8 minutes
old but 150k mcap) falls through to the swing gates rather than being thrown
away. That is what makes `both` genuinely both, instead of "scalp with a
fallback that never fires".

## 6.3 Where to change it

Settings page → **Scalp** and **Swing** groups. Every number above is a row.
Presets:

| preset | style | what it does |
|---|---|---|
| Hyper Scalp (1-10 min) | scalp | 1–60 min / 10k–100k / 5k–20k, swing lane off |
| Scalper mode | scalp | legacy hour-scale scalp ladder (1.3x/1.8x) |
| Swing mode | swing | 1h–3d / 100k–2M / 15k+ liq / 10k+ 1h vol |
| Both lanes | both | scalp band + swing band, routed by age |

Env overrides work for both lanes without editing code:
`SCALP_MIN_MCAP_USD=20000`, `SWING_MIN_LIQUIDITY_USD=25000`, etc.

`/api/health` now returns both bands as loaded by the running process:

```json
"scalp": {"enabled": true, "mcap_band": [10000, 100000], ...},
"swing": {"enabled": true, "mcap_band": [100000, 2000000], ...}
```

## 6.4 What is still unproven

The swing band's numbers are seeded from `SCAN`, which came from the journal's
own review (100k–500k was the edge at 60% WR; sub-100k bled at 6%). The scalp
band's numbers are the geometry you specified, and the honest 5m backtest
caveat in §3 still stands: free data cannot validate a 60% win rate on 1–10
minute holds. Both lanes are paper-only until the journal says otherwise.
