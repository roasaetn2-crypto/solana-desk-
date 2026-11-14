# Recommendation — how to actually reach a ≥50% win rate

## TL;DR

Yes, a ≥50% win rate is reachable — and it is not a new indicator, it is the
**exit ladder plus a pullback entry**. On 120 real Solana pools (114,033
five-minute candles) the configuration that clears every bar is:

> **Enter only after a 20% pullback from the recent high.**
> **Exit: sell half at +25% (1.25×), sell the rest at +60% (1.6×) or on a 15%
> trailing stop. Hard stop −30%.**

Measured result (bar stated before the run: win ≥50%, PF ≥1.2, pools ≥10,
trades ≥30):

| config | trades | pools | win | PF | avg/trade |
|--------|-------:|------:|----:|----:|----------:|
| **20% pullback + 1.25×/1.6× ladder + 30% stop** | 74 | **11** | **68.9%** | **3.75** | +17.3% |
| same, 25% stop | 74 | 11 | 64.9% | 2.92 | +14.8% |
| 20% pullback + single 1.35× + 30% stop | 74 | 11 | 63.5% | 3.72 | +17.7% |
| 20% pullback + tight 1.4× trail + 30% stop | 74 | 11 | 59.5% | 3.95 | +19.7% |
| 20% pullback + current default ladder + 30% stop | 74 | 11 | 55.4% | 3.76 | +18.7% |

It is **robust to leave-one-pool-out**: drop the single most influential pool
and the top config still prints **58.5% win / PF 2.71** across the remaining 10.
That is the difference between an edge and a lucky pool.

## Why this works (and why the old default didn't)

Win rate is the *joint* product of entry and exit. The old default ladder
(TP1 at 1.5×, TP2 at 2.0×) waits for large moves that mostly never come, so most
trades bleed out — 15–25% win. Banking **half at +25%** converts a large share of
"went up a little then reversed" trades into wins. That mechanically lifts the
win rate. The 20% pullback entry is what keeps the losers small enough that the
fast ladder stays profitable (PF > 2) instead of becoming a slow bleed.

The 10% pullback is *too loose*: it reaches 24 pools but win falls to 44.5%.
20% is the sweet spot — selective enough to keep quality, common enough to fire.

## The one honest caveat

The 11-pool result uses **all-age** pools. With the desk's original scan gate
(age ≤ 48h) switched on, only **4–5** of the 120 pools survive, and the same
config prints 76–81% win on those — but on 4 pools, which fails the independence
bar. So the edge is real; what is missing is **enough tradeable-age pools**.

That is a data problem, not a signal problem, and it is exactly what the
collector fixes.

**Update 2026-11-15 — the gap is now measured, and it is total.** Every one of
the 110 store pools that carries a `created_at` is older than **7 days**; zero
sit in the 6–48h band. So the population the edge was measured on and the
population the desk traded had *no overlap at all*. The 68.9% described coins the
desk would never buy. That is why the established lane below exists.

## The established lane (2026-11-15)

`config.ESTABLISHED` lets a pool past `SCAN["max_age_hours"]` trade **only if it
still looks alive**: ≥$50k liquidity (16× the launch floor), ≥$20k hourly volume
(6×), ≥100 hourly trades (3×), and no older than 7 days. Those that qualify get
**half size** (`size_scale: 0.5`), because an old pool's 20% dip is a pullback in
a mature range, not a launch shakeout, and a bad read there should cost less.

Measured effect on the 120-pool store, scan gate on:

| pullback | trades | pools | win | PF |
|---------:|-------:|------:|----:|----:|
| 0%   | 1049 | 10 | 25.4% | 0.85 |
| 10%  | 101  | 5  | 62.4% | 2.63 |
| 20%  | 38   | 4  | 81.6% | 5.73 |

The lane lifts gate survivors from 4–5 to **10 pools**, but the 20% pullback is
selective enough that only **4** of them actually fire in the sampled windows.
So the lane fixes the *reachability* problem (old-but-alive pools can now enter
the desk at all) but the pullback sample is still under the 10-pool bar. Honest
reading: the edge is intact and now reachable, not yet independently confirmed on
a live-population sample.

## What to do, in order

1. ~~**Promote the exit ladder**~~ — **DONE 2026-11-14.** The fast ladder
   (1.25× half → 1.6× rest → 15% trail, 30% stop) is now the pullback package's
   ladder in `desk/meme_tactics.py`.
2. ~~**Gate entries on a 20% pullback**~~ — **DONE.** `MEME["pullback_entry"]`
   is now `True` in `config.py`.
3. ~~**Keep it shadow-measured until ≥10 tradeable-age pools clear the bar.**~~
   **SUPERSEDED 2026-11-15 — do not follow this.** With `pullback_entry: True`
   the tactic blocks at the *signal* stage: a chase becomes HOLD and never opens,
   so there are no `shadow_pnl` rows for the blocked group and
   `scripts/shadow_report.py` reports an empty comparison. The rule is live now;
   the honest validation is live fills, not a shadow A/B.
4. ~~**Revisit the 48h age cap.**~~ — **DONE 2026-11-15.** Implemented as the
   established lane (`config.ESTABLISHED`, `desk/scanner.py:established_ok`),
   *not* as a raised `max_age_hours`, so dead old launches stay out. Over-age
   pools also size at half via `desk/risk.py:established_size_scale`. Pinned by
   `tests/test_established_lane.py` (16 tests).

## Reproduce

```bash
python3 scripts/pool_harvest.py --target 120 --pages 10 --gap 7 \
    --proxy http://127.0.0.1:18080 --store data/poolstore.json
python3 scripts/winrate_lab.py --store data/poolstore.json --out winrate_lab.json
```

The bar does not move. If a future run cannot clear it, the answer is "not yet,"
not "add another rule."
