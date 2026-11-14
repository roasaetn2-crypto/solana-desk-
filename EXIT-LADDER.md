# Switching the exit ladder

The ladder is now a **named choice**, not hardcoded geometry. This exists because
`scripts/ladder_replay.py` measured the four ladders on real candles with entries
held constant, and the result was not close:

| ladder | tp1 | tp2 | trail | win% (30% stop) | expectancy |
|---|---|---|---|---|---|
| `patient` | 2.0x | 3.0x | 20% | 60.2% | **+15.24%** |
| `slow` | 1.5x | 2.0x | 20% | 60.4% | +7.58% |
| `single` | 1.35x | — | 18% | 55.5% | +5.95% |
| `fast` (default) | 1.25x | 1.6x | 15% | 60.9% | +5.19% |

Win rate is **within 1 point across all four**. Expectancy is **~3x apart**.

Read `LADDER-REPLAY.md` before acting on this: the *ranking* is stable across four
stop widths, but the *absolute levels* are not calibrated to your journal. The
engine puts `patient` at 60.2% while `meme_tactics.py` records 30.3% for the same
geometry on a different population. Trust the order, not the number.

## How to switch

Edit `config.py`:

```python
"exit_ladder": "patient",     # fast | patient | slow | single
```

or, without touching the file:

```bash
BRAIN_EXIT_LADDER=patient python3 app.py
```

Resolution order is **env → config → `fast`**. An unknown name falls back to
`fast` rather than raising, so a typo can never leave the desk without exits.

## What does not change

- The default is still `fast`. This commit is inert until you choose otherwise.
- `pullback_entry: False` still disables the whole package, ladder included.
- Nothing about entry selection changes. That is deliberate: the measurement says
  the ladder is *not* where your win rate lives.

## The honest sequence

1. Pick a ladder. Run it for 30+ closed trades.
2. `python3 scripts/trade_audit.py --journal data/journal.jsonl` — compare real
   expectancy against the previous ladder.
3. Only then change anything else. One variable at a time, or you learn nothing.

If your 30.3% does not move after a ladder switch, that confirms what the replay
implies: the problem is upstream, in which trades you take.
