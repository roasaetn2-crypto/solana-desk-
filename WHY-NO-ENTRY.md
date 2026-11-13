# Why is the desk not opening entries? (and is take-profit armed?)

Two live questions, one read-only tool:

1. **Which gate is actually rejecting tokens?**
2. **Is the take-profit ladder live in scalper mode?**

Nothing here trades, changes a setting, or calls the network. It only reads
`journal.jsonl` and `paper_ledger.json` and runs a synthetic position through the
same exit code the broker uses.

## Run it on the phone (Termux)

```bash
cd ~/solana-desk
bash termux/why-no-entry.sh
```

Other windows / machine-readable:

```bash
bash termux/why-no-entry.sh --hours 6      # last 6 hours only
bash termux/why-no-entry.sh --json         # JSON for jq / scripts
python scripts/why_no_entry.py             # same thing without the wrapper
```

Exit code: **0** = no hard block, **2** = a hard gate is blocking.

## What the report tells you

```
-- reject tally (last 24h) --
    412  scanner: chase
     88  risk: thesis score below min
     31  confirm: price moved past gap cap
```

That tally is the answer to "why no entry". Each line maps to one real gate:

| Tally line | Setting that decides it |
|---|---|
| `scanner: chase` | `scan.max_h1_change_pct` (20–2000) |
| `scanner: age` | `scan.max_age_hours`, `scan.min_age_minutes` |
| `scanner: liquidity` / `volume` / `trades` | `scan.min_liquidity_usd`, `scan.min_volume_h1_usd`, `scan.min_trades_h1` |
| `scanner: style` | `trading.style` (scalp/swing/both) |
| `risk: thesis score below min` | `analyst.valid_score` (3–9) |
| `risk: open-risk budget would be exceeded` | `risk.max_daily_loss_pct` vs the open book |
| `risk: ticket below fee floor` | `paper.min_ticket_usd`, `risk.max_pool_share` |
| `confirm: price moved past gap cap` | `entry.max_price_gap_pct` (2–50) |
| `confirm: liquidity failed re-check` | `entry.min_confirm_liq_ratio` |
| `signal: not a BUY read` | `signal.min_confidence` |
| `signal: win probability below floor` | `signal.min_win_probability` |

The report also prints the **paper book / risk gates** block. The three hard
blocks that stop *all* entries regardless of the market are:

- **Daily loss stop** — `blocking_entries=True`. Fix for today:
  `curl -X POST http://127.0.0.1:8000/api/risk/continue`, or set
  `risk.auto_continue_after_daily_stop = true` if you want it automatic.
- **Circuit breaker cooldown** — 3 losses in a row pauses new entries ~30 min.
- **Book full** — `open positions = max_open_positions`; nothing enters until one exits.

If the report says *"No journal found"*, the desk has not run a cycle in that
directory. Start it (`bash termux/start-desk.sh`) and check `DESK_DATA_DIR`.

## Take-profit in scalper mode

The report has a dedicated section:

```
-- take-profit --
  style=scalp  plan=profile  armed=True
    TP1: sell 33% at 1.3x
    TP2: sell 50% at 1.8x
  scalp stop 15%, trail 15%, timeout 1.5h, max hold 4h
  self-test: TP1='TP1 at 1.3x'  TP2='TP2 at 1.8x'  stop='stop -15%'  ok=True
```

`armed=True` and `self-test ok=True` mean the ladder really fires: the script
builds a fake scalp position, marks it up through TP1 and TP2, then drops a second
one through the stop, and checks `desk.exits.evaluate` returns the right sell.

**Scalper mode now pins its take-profit.** The `scalper` preset includes
`exits.tp_plan = profile` plus the scalp `tp1_frac` / `tp2_frac`, so a previous
`global` or `single` plan (or a hand-edited fraction) can never leave scalper mode
without its 1.3x / 1.8x ladder. Confirming the preset re-arms it.

To switch into scalper mode (pending proposal → Confirm in the app, or):

```bash
curl -s -X POST http://127.0.0.1:8000/api/proposals \
  -H 'content-type: application/json' -d '{"preset":"scalper"}'
# then confirm the id it returns:
# curl -s -X POST http://127.0.0.1:8000/api/proposals/<id>/confirm
```

After confirming, re-run `bash termux/why-no-entry.sh` and check the take-profit
block says `style=scalp ... armed=True`.

## How to know the update is really on the phone

```bash
cd ~/solana-desk
bash termux/verify-update.sh --fix
```

Look for section **4c**:

```
=== 4c. why-no-entry diagnostic + scalper take-profit ===
  OK    scripts/why_no_entry.py present
  OK    presets pin exits.tp_plan (TP ladder re-armed)
  OK    tests/test_why_no_entry.py present
  OK    why_no_entry.py runs (exit 0 = no hard block, 2 = blocked)
```

If any line says `MISS`, the pull did not land — run `--fix` again and read the
git output.

## If entries are still rare after this

That is the desk being selective, not broken. The honest next step is to let the
Desk AI read the backtest and propose the exact gate to move:

- In the app: **Recommend settings** (creates a PENDING proposal, you Confirm).
- Or `curl -s -X POST http://127.0.0.1:8000/api/ai/recommend`.

The recommendation is built from the same reject history this tool summarises, so
the two should agree. If they disagree, trust the journal tally first.
