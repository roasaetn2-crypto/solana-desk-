# research/

Generated artifacts from the `scripts/` analysis tools. Everything here is
**regenerable** — it is committed only so the numbers in `docs/research/` can
be checked without re-running a multi-hour backtest.

Nothing in `desk/` or `app.py` reads from this directory. Deleting it cannot
affect trading; it only breaks the links in the reports.

## What is here

| File | Produced by | Consumed by |
|---|---|---|
| `backtest_100.json` | `scripts/backtest_100.py` | `docs/research/BACKTEST_100_REPORT.md` |
| `backtest_100.png` | `scripts/make_backtest_chart.py` | `docs/research/BACKTEST_100_REPORT.md` |
| `backtest_gates.json` | `scripts/backtest_gates.py` | `docs/research/BACKTEST_GATES_REPORT.md` |
| `backtest_gates.png` | `scripts/make_gates_chart.py` | `docs/research/BACKTEST_GATES_REPORT.md` |
| `backtest_g87e*.json` | `scripts/backtest_g87e.py` | `docs/research/BACKTEST_G87E_REPORT.md` |
| `backtest_signals_*.json/.svg` | `scripts/backtest_signals.py` | `docs/research/BACKTEST_SIGNALS_REPORT.md` |
| `indicator_eval*.json` | `scripts/indicator_eval.py` | `docs/research/INDICATOR-EVALUATION.md` |
| `liq_tiers.json`, `liq_comparison.png` | `scripts/analyze_liq.py`, `scripts/make_liq_chart.py` | `docs/research/BACKTEST_LIQUIDITY_REPORT.md` |
| `meme_tactics*.json` | `scripts/meme_tactics_eval.py` | `docs/reference/MEME_TACTICS.md` |
| `winrate_lab.json`, `winrate_summary.png` | `scripts/winrate_lab.py`, `scripts/make_winrate_chart.py` | `docs/research/WINRATE.md` |
| `pools_cache.json` | `scripts/pool_harvest.py` | `scripts/analyze_liq.py` |
| `tp_placement.json` | `scripts/tp_placement.py` | `docs/reference/TP-PLACEMENT.md` |
| `social_eval.json` | `scripts/social_eval.py` | `docs/reference/SOCIAL.md` |
| `g87e_*.pine` | hand-written | `docs/research/BACKTEST_G87E_REPORT.md` |

## Why it is not in the repo root

It used to be. Every script wrote to a bare filename, so the output landed in
whatever directory you ran it from, and the root accumulated thirty of these
next to `app.py`. `scripts/_paths.py` now resolves every artifact against the
repo root, so a script run from anywhere writes here and reads from here.
`tests/test_artifact_paths.py` fails if a bare filename comes back.

## Regenerating

```bash
python3 scripts/backtest_100.py          # -> research/backtest_100.json
python3 scripts/make_backtest_chart.py   # -> research/backtest_100.png
```

Each script still takes `--out /tmp/x.json` if you want the artifact somewhere
else.
