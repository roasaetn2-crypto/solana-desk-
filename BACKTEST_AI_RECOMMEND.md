# Backtest → Desk AI recommendation (fixed)

## What was broken
- `/api/backtest` produced ~15 sweep rows, but `desk_chat.performance()` sliced `[:8]`, so the AI never saw the chase/alive/gap sweeps.
- Rows carried only a human label ("score 60") and no concrete `settings`, so the AI could not turn a label into a value.
- The grid swept `min_score` (0–100), `max_chase`, `min_alive_6h` — none exist in the live settings registry, so every AI proposal built from them was dropped as an "unknown setting".
- With no AI key, "Recommend settings" returned a dead-end message and never produced a proposal.

## What changed
### `desk/backtest.py`
- `LIVE_SETTING_KEYS` / `LIVE_SETTING_BOUNDS` map backtest knobs to real registry keys:
  - `min_thesis_score` → `analyst.valid_score` (3–9)
  - `max_h1_change_pct` → `scan.max_h1_change_pct` (20–2000 %)
  - `max_price_gap_pct` → `entry.max_price_gap_pct` (2–50 %)
- `default_grid()` is now **actionable and isolated**: every actionable row moves exactly ONE live setting away from current, so a result is attributable. The old-desk score/chase/alive sweeps are kept as `actionable=False` diagnostics and are never recommended.
- `simulate()` emits `actionable` and `bundle` on every row.
- New pure `recommend(report)`:
  - ranks only actionable, traded rows risk-adjusted (`pnl − 0.5·maxDD + 2·win_rate`);
  - compares the best row to `current`;
  - **holds when the best row does not actually beat current** (no zero-improvement noise);
  - emits deltas in registry keys, plus `confidence`, `evidence`, and `notes`;
  - confidence is `low` on thin samples or <3 trades, `high` only with ≥8 trades and positive improvement.
- `report()` attaches `recommendation`; the CLI prints it.

### `desk/desk_chat.py`
- `performance()` sends **all** rows (cap 24) with their `settings`, `unknown`, `passed_gates`, plus the `recommendation` block.
- `SYSTEM` prompt and `RECOMMEND_Q` now treat `performance.backtest.recommendation` as primary evidence and only use valid registry keys; propose nothing on `hold`/low confidence.
- `_BT_KEY_ALIASES` normalises any backtest key the model echoes and drops replay-only keys.
- `_recommend_fallback()` gives a deterministic reply + proposals from the recommendation, so the feature works with **no AI key** — still pending, still requires Confirm.

## Safety invariants preserved
- Backtest is read-only: never changes live settings, never invents prices, marks unverifiable outcomes `unknown`.
- AI is advisory-only: it can only create a **pending** proposal; the operator must Confirm.
- Off-limits: mode/live/execution, keys/secrets, wallet, scam/dev kills, bankroll, `entry.require_confirm_quote`.
- `desk_chat` still imports nothing that can act (isolation test passes).

## Verification
- New `tests/test_backtest_recommend.py`: grid isolation, `recommend()` adjust/hold/confidence, performance completeness, offline fallback, key aliases, and `/api/ai/recommend` end-to-end creating a pending proposal.
- Full suite: **632 passed, 17 skipped**.
- Committed and pushed to `roasaetn2-crypto/solana-desk-` `main` (`7b2d9be`, docs `0f9cbff`).
