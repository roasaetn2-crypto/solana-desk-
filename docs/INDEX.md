# Documentation index

Thirty-six documents, grouped by what you are trying to do. Start at
[`../START-HERE.md`](../START-HERE.md) if you have never run the desk, or
[`../README.md`](../README.md) for what it is and how it works.

---

## Guides — getting it running

| Doc | Use it when |
|---|---|
| [ANDROID.md](guides/ANDROID.md) | You want the desk on your phone and need the whole walkthrough. |
| [TERMUX-STEP-BY-STEP.md](guides/TERMUX-STEP-BY-STEP.md) | Exact Termux commands, in order, no explanation. |
| [TERMUX-CHEATSHEET.md](guides/TERMUX-CHEATSHEET.md) | You already know the setup and just need the commands again. |
| [TERMUX-ALWAYS-ON.md](guides/TERMUX-ALWAYS-ON.md) | The phone keeps killing the desk when the screen locks. |
| [OPEN-ON-ANDROID-YOURSELF.md](guides/OPEN-ON-ANDROID-YOURSELF.md) | No laptop, no cloud account, no assistant — phone only. |
| [DEPLOY-RENDER.md](guides/DEPLOY-RENDER.md) | You want it hosted instead of on the phone. |
| [RENDER-FREE-NOSLEEP.md](guides/RENDER-FREE-NOSLEEP.md) | Render's free tier keeps spinning the box down after ~15 minutes. |
| [ORACLE-ALWAYS-FREE.md](guides/ORACLE-ALWAYS-FREE.md) | You want a genuinely always-free host instead of Render's free tier. |

## Reference — how a piece works

| Doc | Covers |
|---|---|
| [EXIT-LADDER.md](reference/EXIT-LADDER.md) | The `fast` / `patient` / `slow` / `single` ladders, and why env outranks `config.py`. |
| [WHY-NO-ENTRY.md](reference/WHY-NO-ENTRY.md) | The desk is running but opening nothing — the gate-by-gate checklist. |
| [MEME_TACTICS.md](reference/MEME_TACTICS.md) | What the memecoin playbook adds, and what it does not. |
| [TP-PLACEMENT.md](reference/TP-PLACEMENT.md) | Where TP1/TP2 actually belong, measured. |
| [COLLECTOR.md](reference/COLLECTOR.md) | How pools reach the scanner. |
| [POOL-HARVEST.md](reference/POOL-HARVEST.md) | Growing the candle cache for research. |
| [SOCIAL.md](reference/SOCIAL.md) | Social attention scoring. |
| [FOMO-SEAT.md](reference/FOMO-SEAT.md) | The optional FOMO seat. |
| [GROQ-POSTMORTEM.md](reference/GROQ-POSTMORTEM.md) | The offline "what do the losers share" review. |
| [GROQ-SIZER.md](reference/GROQ-SIZER.md) | The shrink-only size multiplier — and why it is off by default. |
| [GUARD-LIVE.md](reference/GUARD-LIVE.md) | The guard only guarding the simulator. |
| [GUARD-G87E.md](reference/GUARD-G87E.md) | The guard applied to G87E: halves the bleed, still loses. |

## Research — the measurements

These are the numbers the desk's defaults are built on. Each one states what it
tested, what it found, and what it does **not** prove.

| Doc | Question it answers |
|---|---|
| [WINRATE.md](research/WINRATE.md) | What actually moves win rate, and by how much. |
| [RECOMMENDATION.md](research/RECOMMENDATION.md) | The concrete path to ≥50%. |
| [LADDER-REPLAY.md](research/LADDER-REPLAY.md) | Which exit ladder wins on replay. |
| [BACKTEST_100_REPORT.md](research/BACKTEST_100_REPORT.md) | 100 trades, first pass (superseded — kept for the record). |
| [BACKTEST_GATES_REPORT.md](research/BACKTEST_GATES_REPORT.md) | Do the entry gates help, and which settings matter. |
| [BACKTEST_G87E_REPORT.md](research/BACKTEST_G87E_REPORT.md) | The G87E Pine, as written versus fixed versus tuned. |
| [SURVIVORSHIP-G87E.md](research/SURVIVORSHIP-G87E.md) | Why G87E's +7.9% was an artifact. |
| [BACKTEST_LIQUIDITY_REPORT.md](research/BACKTEST_LIQUIDITY_REPORT.md) | Does a liquidity floor help. |
| [BACKTEST_SIGNALS_REPORT.md](research/BACKTEST_SIGNALS_REPORT.md) | Do the entry reads predict direction. |
| [INDICATOR-EVALUATION.md](research/INDICATOR-EVALUATION.md) | Indicators tested before being added — most were not. |
| [BACKTEST_AI_RECOMMEND.md](research/BACKTEST_AI_RECOMMEND.md) | Turning a backtest into a settings change. |
| [TEST_REPORT_entry_exit.md](research/TEST_REPORT_entry_exit.md) | Entry/exit behaviour under test. |

Artifacts (JSON, PNG, Pine) live in [`../research/`](../research/README.md).

## Changelog — what changed and why

| Doc | Records |
|---|---|
| [todo.md](changelog/todo.md) | The running build log: every feature, what it touched, test count. |
| [LOCAL_CHANGES.md](changelog/LOCAL_CHANGES.md) | Changes made locally and never pushed. |
| [RISK-BUDGET-FIX.md](changelog/RISK-BUDGET-FIX.md) | The open-risk versus daily-stop deadlock. |
| [FIX-G87E.md](changelog/FIX-G87E.md) | Step-by-step G87E repair. |

---

## Adding a doc

Put it in the folder that matches the question it answers, then add a row to the
table above. A doc that is not in this index will not be found — the root used to
hold all thirty-six and the index is what replaced that.
