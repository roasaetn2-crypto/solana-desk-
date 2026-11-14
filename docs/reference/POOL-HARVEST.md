# Pool Harvest — growing the candle cache

## Why this exists

Every experiment on this desk keeps hitting the same wall: **too few independent
pools**. The committed caches (`data/indicator_paths.json`, `backtest_cache.json`)
cover ~60–101 pools, and once the gate ladder runs, that collapses to **5 fully
gated trades**. Five trades is not evidence; it is a coin flip with a story
attached. `MIN_POOLS = 10` exists because trades inside a single pool are one
observation, not many (pool `4W7BNgGs` alone produced 42 trades at 81% win,
dragging an otherwise 17% group).

So before adding any more rules, we grow the raw material: **candles, keyed by
pool, append-only and resumable.**

## Pieces

- `desk/poolstore.py` — append-only JSON store keyed by pool address.
  - `put()` merges new candles by timestamp and keeps the newest `max_bars`.
  - Writes are atomic (temp file + `os.replace`), so a crash never corrupts the
    store. A corrupt file is moved aside to `.corrupt` rather than lost.
  - `load_paths()` returns the same shape the backtests already consume, so the
    store drops straight into `indicator_backtest` / `signal_backtest`.
- `scripts/pool_harvest.py` — the collector.
  - Discovers pools from GeckoTerminal `new_pools`, `trending_pools`, `pools`.
  - Fetches 5-minute OHLCV (1000-bar cap ≈ 3.5 days) per pool.
  - Resumable: skips pools already holding `>= MIN_BARS (70)`.
  - Polite: a global pacer keeps requests under ~28/min, and `429` responses are
    honored via `Retry-After` (min 5s, cap 120s) with up to 8 retries.

## Reproduce

```bash
# grow to 500 usable pools (>= 70 bars each)
python3 scripts/pool_harvest.py --target 500 --pages 12 --gap 2.15 \
    --store data/poolstore.json

# behind a shared / rate-limited IP, route through an HTTP proxy:
python3 scripts/pool_harvest.py --target 500 --pages 12 --gap 2.15 \
    --proxy http://127.0.0.1:18080 --store data/poolstore.json

# inspect
python3 scripts/pool_harvest.py --list
```

Re-run any time to extend history; `--refresh` re-fetches pools already stored so
their windows slide forward.

## Notes on rate limits

The free GeckoTerminal tier is ~30 req/min. On a shared sandbox IP you will see
hard `429`s with `Retry-After: 0`; the harvester treats those as "sleep 5s and
retry" and keeps going, but throughput drops. Routing through a proxy with a
clean IP restores full speed. The store makes this safe to stop and restart at
any point.

**Measured on this box:** the OHLCV endpoint is the tight one. Direct requests
throttle after ~1 call and then `429`; through the proxy, OHLCV at **6–7s
spacing** returns clean `200`s. Use `--gap 7 --proxy http://127.0.0.1:18080`.

## Picking the right population (important)

The desk trades pools aged **1–48h**. That creates a data trap:

- `new_pools` is ordered newest-first and only covers the last few hours — every
  pool in it is younger than ~6h, i.e. **fewer than 70 five-minute bars**. A
  14-page sweep found **0** pools in the 6–48h band.
- `trending_pools` / `pools` return established tokens that are **years** old
  (median ~1.7 years in one sweep). The scan gate rejects them on age, so they
  are useless for measuring the tradeable universe — though they *are* fine for
  measuring whether a signal has an edge at all.

So there are three harvesters, each for a job:

| script | population | use |
|--------|-----------|-----|
| `pool_harvest.py` | new + trending + top | general growth, resumable |
| `harvest_old.py` | trending + top, oldest-first | fast bulk history for signal tests |
| `harvest_window.py` | new_pools, filtered to 6–48h | the tradeable universe (needs continuous runs) |

The 6–48h band is genuinely hard to enumerate on the free tier. The practical
answer is to run `pool_harvest.py` continuously so fresh launches **age into**
the window, then measure with `winrate_lab.py`.
