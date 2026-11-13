# Open risk vs the daily stop — the deadlock, and the fix

You saw this on the desk:

```
AMC: risk engine said no (open risk $2.14 (open $1.89 + new $0.25)
would exceed the $1.54 daily stop)
```

That message was not a one-off rejection. It was a **permanent deadlock**: open
risk ($1.89) was already above the whole-book budget ($1.54), so *every* new
entry was refused, forever, while the desk still looked alive. Nothing was wrong
with AMC or with the scanner.

## What the gate is for

The daily stop is 3% of the book. A single trade risks 1.5%. If you open six of
them, you have 9% of the book at risk on a desk that promises a 3% day. So before
any entry, the desk adds up what the whole open book would lose if every stop
filled at once (`open_risk_usd`) and refuses tickets that would push that past the
daily stop.

That idea is right. Three separate faults made it brick the desk.

## Fault 1 — it refused on a few cents instead of shrinking the ticket

The old check was all-or-nothing:

```python
if open_risk + new_risk > budget:
    ok = False
```

If $0.02 of room was left, a $4 ticket was rejected outright rather than trimmed
to the $0.02. Now `fit_risk_budget()` **shrinks the ticket to whatever room is
left**, and only refuses when there is genuinely nothing sensible to do:

```
ticket trimmed $4.17 -> $2.33 to keep open risk inside the $1.54 budget
```

## Fault 2 — the budget shrank as you lost (the procyclical trap)

The budget was `3% x live equity`. Live equity falls when open positions go under
water, so the budget shrank, so the desk locked itself out of new entries *exactly
when it was already losing*. A budget that moves with the marks is not a budget.

It is now anchored to `day_start` — the equity the day opened with:

```
equity $40.00, day_start $51.33 -> budget $1.54   (old basis: $1.20)
```

## Fault 3 — the config contradicted itself

`max_risk_per_trade` is 1.5% and `max_open_positions` is 6. That is 9% of the book
at risk against a 3% daily stop. **Only two positions can ever fit**, so the third
was refused no matter what. The numbers were never reconciled when the retune
moved risk per trade from 5% to 1.5%.

There is now a knob for it:

```python
"risk_budget_split_across_slots": False,
```

Set it to `True` and the per-trade risk becomes `budget / max_open_positions`
($0.26 on your book instead of $0.77), so six positions fit inside the 3% day by
construction. It defaults to `False` so your current ticket sizes do not silently
change. It also refuses to split so far that the caution layer's worst cut drops a
ticket under the fee floor — that would just be the sizing deadlock in a new hat.

## What you will see now

* **If there is room** — the ticket is trimmed and the entry goes through.
* **If the book is already over budget** — a banner on the dashboard says so, in
  dollars, with the way out:

  > Open risk $1.89 is over the $1.54 risk budget (over by $0.35). New entries
  > wait until positions close or stops tighten.

  The same numbers are in `risk_status()` as `open_risk_usd`,
  `risk_budget_usd` and `over_risk_budget`, so the API and any script can see it.

## Your book right now

Open risk is $1.89 against a $1.54 budget, so the desk is correctly refusing new
entries. It is **not** broken — it is over its own risk limit. It clears itself
two ways:

1. **Automatically.** Every position has a timeout and a max-hold in
   `desk/exits.py`, so open risk falls as they close, and entries resume.
2. **Immediately, if you decide the limit is wrong for this book.** Either raise
   `max_daily_loss_pct` (e.g. `0.03` -> `0.05` makes the budget $2.57), or set
   `risk_budget_split_across_slots` to `True` so future tickets are sized to fit
   six slots.

What you should *not* do is delete the check. Without it, six 1.5% trades can put
9% of the book at risk on a desk that advertises a 3% day.

## Verify on the phone

```bash
cd ~/solana-desk
bash termux/verify-update.sh          # section 4e
python -m pytest tests/test_risk_budget.py -q
```

`tests/test_risk_budget.py` has 16 tests pinning all three faults: the trim
instead of refuse, the day-start basis, the two legitimate refusals, and the
dashboard fields.
