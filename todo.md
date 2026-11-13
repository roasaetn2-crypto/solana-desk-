# Solana Desk — backtest → AI recommendation fix

## Diagnosis
- [x] Repo identified: `roasaetn2-crypto/solana-desk-` (main)
- [x] Bug reproduced: `/api/backtest` produces 15 rows, but `desk_chat.performance()` truncates to `[:8]`, silently dropping all alive/gap sweeps.
- [x] Rows sent to the AI carry no concrete `settings`, so a label like "score 60" cannot be turned into a value.
- [x] Core mismatch: the grid sweeps `min_score` (0-100), `max_chase`, `min_alive_6h` — none exist in the live settings registry, so any AI proposal built from them is dropped as "unknown setting".
- [x] No deterministic recommendation / offline fallback: "Recommend settings" is dead without an AI key.

## Fix — backtesting (`desk/backtest.py`)
- [x] Add live-setting key map (min_thesis_score→analyst.valid_score, max_h1_change_pct→scan.max_h1_change_pct, max_price_gap_pct→entry.max_price_gap_pct).
- [x] Make `default_grid()` actionable + truly isolated sweeps anchored on current settings; keep old-desk rows as non-actionable diagnostics.
- [x] Emit `actionable`/`bundle` on every simulated row.
- [x] Add `recommend(report)`: rank rows risk-adjusted, compare best vs current, emit registry-key deltas with confidence + notes.
- [x] Guard: never recommend a change that did not beat current risk-adjusted (tie → hold).

## Improve — AI desk (`desk/desk_chat.py`)
- [x] `performance()`: send ALL rows (with settings/unknown/passed_gates) + the recommendation block.
- [x] Update SYSTEM prompt + RECOMMEND_Q to use the recommendation as primary evidence and only registry keys.
- [x] Normalize backtest keys the model may echo into valid registry keys (drop non-settable ones).
- [x] Add deterministic offline recommendation reply so the feature works without AI keys.

## Verify
- [x] Add tests for grid isolation, recommend(), performance completeness, offline fallback, endpoint wiring.
- [x] Run full pytest suite (632 passed, 17 skipped).
- [x] Commit + push to GitHub main (7b2d9be).

# Live issue — entries not opening + scalper take-profit

## Diagnose with evidence (no guessing)
- [x] Add `scripts/why_no_entry.py`: read-only tally of journal rejects by stage/reason, paper-book risk gates (daily stop, breaker, book full), effective gate values, and a synthetic scalp TP/stop self-test.
- [x] Exit code 0 = no hard block, 2 = hard block, so Termux scripts can branch.
- [x] Add `termux/why-no-entry.sh` wrapper for the phone.

## Fix scalper take-profit
- [x] Pin `exits.tp_plan = profile` in the scalper preset so a prior global/single plan cannot disable the scalp ladder.
- [x] Pin scalp `tp1_frac` / `tp2_frac` in the scalper + balanced presets so a tampered fraction cannot neuter TP.
- [x] Pin `exits.tp_plan = profile` in the swing preset for symmetry.

## Verify
- [x] Add `tests/test_why_no_entry.py` (empty desk, tally grouping, window, daily halt, full book, override, TP self-test, CLI exit codes).
- [x] Extend settings tests: scalper preset re-arms the scalp TP ladder.
- [x] Full suite: 644 passed, 17 skipped.
- [x] Add section 4c to `termux/verify-update.sh`.
- [x] Docs: `WHY-NO-ENTRY.md`.
- [x] Commit + push to GitHub main (4b16fc3).
