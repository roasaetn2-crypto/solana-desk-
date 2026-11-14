# Groq post-mortem

An offline read of your own closed trades. It answers one question: **what do the
losing trades share that the winners do not?** It is not a signal, not a sizer and
not an entry opinion. It never touches an order.

## Why this and not the sizer

The shrink-only sizer was the wrong job for a language model. Win rate is
`wins / trades`, and position size does not appear in that ratio. Cutting a ticket
from 1.00x to 0.50x scales both the win and the loss by the same factor, so
neither win rate nor profit factor can move. The only lever that changes win rate
is **which trades you take**, and that is a question about the population, not
about one ticket in front of you. A post-mortem is the cheapest way to ask it.

The other reason is quota. Exit review and the post-mortem both draw from the same
`GROQ_API_KEY` allowance (1,000 requests/day on the free tier). The sizer spent
40 calls a day to produce a multiplier that could not change the outcome. That
allowance is better spent on a handful of exit reviews and one batch analysis
after the sample is large enough.

## The honesty gate

`desk/groq_postmortem.py` refuses to call a model until there are **30 closed
trades** (`BRAIN_POSTMORTEM_MIN_TRADES`). Below that bar a model will happily
invent a pattern from noise, and a confident-sounding fabrication is worse than
no answer. The gate is checked before any provider object is built, so a small
sample costs zero tokens.

When it does run, the sample is balanced: up to 40 winners and 40 losers, not the
first 80 trades in journal order. A win streak cannot dominate the batch and make
every trait look like a winner trait.

## What it reads

It joins three journal kinds by mint:

- `decision` — the context at entry (liquidity, mcap, one-hour volume, buy/sell
  counts, age, score, thesis, shape, regime, confidence).
- `entry` — the fill.
- `exit` — the close, including `pnl_usd` when the position is fully flat.

Exits without a `pnl_usd` are skipped: a partial fill is not a result. Null fields
are dropped rather than zeroed, because a missing `age_minutes` is missing data,
not a young pool. The prompt is told this explicitly and is allowed to answer
"no separating feature found" — that is a valid and useful result.

The model is also forbidden from recommending size, leverage or stop changes. The
only actionable output is an **entry-quality hypothesis** plus a **next test**.

## Running it

```bash
# is there enough data yet?
python3 scripts/groq_postmortem.py --status

# what would be sent (no model call)
python3 scripts/groq_postmortem.py --cases

# produce a report (only runs at 30+ closed trades)
python3 scripts/groq_postmortem.py

# re-run even if the trade count has not moved
python3 scripts/groq_postmortem.py --force
```

The result is cached in `groq_postmortem.json` and reused until the number of
closed trades changes, so a cron job or a dashboard refresh cannot burn quota.

## The panel

`web/index.html` carries a **Groq post-mortem** card in the left column. It shows:

- a badge: `OFF`, `WAITING`, `READY` or `REPORT`;
- a progress bar of closed trades against the 30-trade bar;
- the win/loss split and win rate from the last report;
- the verdict and any null-data warning;
- loser traits and winner traits, each with its evidence and a confidence colour;
- the next test.

The panel **never triggers a model call**. It reads `groq_postmortem.json` and
counts the journal, nothing else. Running the analysis is a deliberate CLI step,
so a refresh loop on a phone cannot spend the day's allowance by accident.

## Reading the output honestly

A post-mortem produces a hypothesis. The correct next move is to turn it into one
config change and measure it:

```bash
python3 scripts/winrate_lab.py
```

If the win rate does not move, the split was noise. Do not stack three changes at
once; you will not know which one did anything.

## Environment

| variable | default | meaning |
| --- | --- | --- |
| `BRAIN_POSTMORTEM_ON` | on | set `0` to disable entirely |
| `BRAIN_POSTMORTEM_MIN_TRADES` | `30` | the honesty gate |
| `BRAIN_POSTMORTEM_BATCH` | `40` | winners and losers sent per side |
| `BRAIN_POSTMORTEM_CALLS` | `3` | hard cap on model calls per run |
| `BRAIN_POSTMORTEM_STATE` | `groq_postmortem.json` | where the report is cached |
| `GROQ_API_KEY` | — | required; the same key exit review uses |
