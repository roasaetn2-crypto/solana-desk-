# Collector — how the desk gets pools it can actually trade

## The problem, measured

The desk trades a **6–48h** age band. Younger than 6h there are fewer than 70
five-minute bars, so nothing can be judged; older than 48h the scan gate refuses
the pool (unless the established lane accepts it, which needs much deeper
liquidity).

The store used to be filled by one-shot harvests, and it never contained a pool
in that band. The reason, measured on 2026-11-15:

```
new_pools pages 1..8 -> 160 pools, every one under 0.1 hours old
  0.0-   1.0 h: 160
  1.0-   2.0 h: 0
  ... every other bucket: 0
```

`new_pools` is a **rolling ~6-minute feed**. Paging back does not walk into the
past — it walks minutes apart. So `harvest_window.py`'s premise ("page back to
reach the 6–48h band") was simply wrong, and it kept finding 0 pools. The
existing 120-pool store came from `trending_pools`/`pools`, which is why every
pool in it is **~1,659 days** old.

## The fix: separate discovery from fetching

`scripts/collector.py` runs two phases:

1. **discover** — pull `new_pools`, remember every candidate in
   `data/watchlist.json` with its `created_at`. Cheap: 1–3 requests, no OHLCV.
2. **harvest** — for each watched pool whose age has now entered 6–48h, fetch its
   OHLCV **once** and write it to the store.

A pool seen young in phase 1 becomes harvestable a few hours later. No
long-running process is needed between runs, which is what makes this work on a
phone: each invocation is short and idempotent.

## Running it on Termux

```bash
cd ~/solana-desk-

# one pass, to see it work
bash termux/collector-loop.sh --once

# keep it running (foreground; Ctrl-C to stop)
bash termux/collector-loop.sh

# or in the background, logging to data/collector.log
bash termux/collector-loop.sh --daemon
```

Each pass prints one line:

```
[2026-11-15 18:29:05] discover +38 | harvest +0 (fail 0) | pruned 0 | watched 69 pending 69 | store 120 pools 120 usable | 7.6s
```

`watched`/`pending` climbing with `harvest +0` is **correct early on** — those
pools are minutes old. They become harvestable in about six hours. The first
`harvest +N` is the signal that the band is filling.

Check state without touching the network:

```bash
python3 scripts/collector.py --status
```

## Tuning

| setting | default | why |
|---|---|---|
| `COLLECTOR_INTERVAL` | 1200s (20 min) | often enough not to miss the window; short enough to be gentle on the free tier |
| `COLLECTOR_PAGES` | 2 | 40 pools per pass; more pages mostly re-read the same minutes |
| `COLLECTOR_FETCH` | 12 | caps a pass at ~30s so a phone run stays short |
| `COLLECTOR_PROXY` | none | set to `http://127.0.0.1:18080` if the direct IP is 429'd |

```bash
COLLECTOR_PROXY=http://127.0.0.1:18080 COLLECTOR_FETCH=6 bash termux/collector-loop.sh
```

## Two Android-specific traps

**The screen sleeping stops the loop.** Android suspends background work, so the
loop silently stops and the 6–48h window is missed. `collector-loop.sh` calls
`termux-wake-lock` when it exists; install it with `pkg install termux-api` and
grant Termux the battery-optimisation exemption.

**A killed process must not corrupt state.** The watchlist is written atomically
(temp file + `os.replace`), so a phone dying mid-run leaves the previous good
file rather than a truncated one. A corrupt file is treated as empty rather than
crashing the run.

## What "working" looks like after a day

```bash
python3 scripts/collector.py --status
python3 scripts/pool_harvest.py --list
```

The store should gain pools whose ages sit in the band. Once it holds ~10 such
pools, `scripts/winrate_lab.py` can measure the pullback edge on the population
the desk actually trades — which is the one thing the current 120-pool store
cannot do.
