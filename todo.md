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
- [ ] Commit + push to GitHub main.
