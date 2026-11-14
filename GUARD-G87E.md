# Does the desk's discipline layer rescue G87E? — No. It halves the bleed and still loses.

**Short version:** replaying the desk's *real* `desk/guard.py` circuit breaker and a
volume-expansion entry gate on the same 12 unbiased pools cuts the median loss from
**−23.25% to −8.75%**, cuts average drawdown from **24.9% to 9.6%**, and improves the
average trade from **−0.893% to −0.346%**. Every single pool improved. And the result
is still **negative on 10 of 12 pools**.

The discipline layer is a loss-limiter, not an edge. It cannot turn a signal with no
information into a signal with information. It can only make the bleeding slower.

---

## What was actually tested (and what could not be)

This distinction matters more than the numbers.

| Desk module | Backtestable? | Why |
|---|---|---|
| `desk/guard.py` | **Yes — fully** | Pure functions, no I/O. `circuit_breaker()`, `loss_streak()`, `size_multiplier()` all replay against history. |
| `desk/signals.py` | **No** | GoPlus, GMGN, PumpPortal, DexScreener, Jupiter — all live network calls. No historical snapshot of those flags exists. Backtesting them would mean inventing data. |

So variant B tests **real desk code**. Variant C tests a **proxy**: the one signals
component derivable from OHLCV, volume expansion versus its own 20-bar mean. It is
labelled a proxy everywhere and should never be reported as "the signals layer works."

## Method

Same 12 pools as `SURVIVORSHIP-G87E.md`, same tuned config, same execution model, same
costs (0.05% fee + 0.30% slippage per side), 5m bars. The only change is the gate.

The backtester gained one optional hook, `gate(i, row, ctx) -> multiplier`, where 0
blocks the entry, 0.5 halves it, 1.0 passes. It can only tighten (values above 1.0 are
clamped), and a gate that raises an exception defaults to 1.0 so a bug can never
silently block trading. Verified non-invasive: with `gate=None` the 10 cached 5m pools
still produce **exactly 343 trades**, identical to the pre-change record.

| Variant | Gate |
|---|---|
| **A control** | none |
| **B breaker** | `guard.circuit_breaker()` — 3 losses in a row → pause, 2 → half size, day P&L past −2% → half size |
| **C volgate** | volume ≥ 1.5 × its 20-bar mean (proxy for the volume chip) |
| **D breaker+vol** | both |

## Result

| Variant | Trades | Median | Mean | Green | Avg trade | Exposure | Avg DD | Blocked |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A control | 644 | **−23.25%** | −23.12% | 0/12 | −0.893% | 71.4% | 24.9% | 0 |
| B breaker | 598 | −12.71% | −13.68% | 0/12 | −0.761% | 67.8% | 15.4% | 354 |
| C volgate | 411 | −13.54% | −10.40% | 2/12 | −0.369% | 42.1% | 14.6% | 1,971 |
| D breaker+vol | 390 | **−8.75%** | −5.86% | 2/12 | −0.346% | 40.0% | **9.6%** | 2,128 |

Improvement over control: B **+10.54 pp** median, C **+9.71 pp**, D **+14.50 pp**.

### Every pool improved, none flipped meaningfully

| Pool | A control | D breaker+vol | Trades | Max DD |
|---|---:|---:|---:|---|
| Punch / SOL | −13.3% | **+26.9%** | 63 → 39 | 19.1% → 3.5% |
| GTA6 / SOL | −10.4% | **+1.4%** | 55 → 36 | 16.8% → 7.0% |
| USDT / SOL | −23.6% | −6.7% | 37 → 20 | 23.6% → 6.7% |
| SPCX / SOL | −14.1% | −6.7% | 41 → 28 | 14.6% → 7.0% |
| GATO / SOL | −8.6% | −7.4% | 53 → 36 | 12.1% → 7.7% |
| USDT / SOL | −22.9% | −8.2% | 38 → 22 | 22.9% → 8.2% |
| BIG / TTWO | −31.8% | −9.3% | 59 → 29 | 32.1% → 9.3% |
| TTWO / SOL | −22.6% | −10.2% | 47 → 29 | 23.7% → 11.8% |
| HYPE / SOL | −23.6% | −10.8% | 50 → 34 | 23.9% → 10.8% |
| NIO / SOL | −32.4% | −11.7% | 64 → 47 | 35.3% → 14.9% |
| USDC / SOL | −44.5% | −13.6% | 82 → 38 | 44.5% → 13.6% |
| AVGOx / SOL | −29.7% | −13.9% | 55 → 32 | 29.7% → 14.4% |

Twelve for twelve improved. Two crossed into profit. Ten did not.

## The number that settles it

Average trade: **−0.893% → −0.346%**.

That is a genuine improvement in per-trade expectancy — nearly a 3× reduction in the
per-trade bleed — and it is still **negative**. The gates are not finding better
entries; they are finding *fewer* entries, and the ones they keep are still, on
average, losers. Exposure falling from 71% to 40% confirms it: the improvement comes
mostly from standing aside.

A filter that improves a losing system by making it trade less is a risk control, not
an edge. Both are useful. Only one of them makes money.

Also note the liquidity floor is a dead end: median at liq ≥ $0 is −23.25%, at
≥ $50k it is −18.37%, at ≥ $100k it is −22.89%. Non-monotonic, 0 green at every
threshold. Liquidity does not separate winners from losers here.

## Verdict

1. **Do not ship G87E as an entry, with or without the gates.** −8.75% median with
   2/12 green is not a strategy; it is a slower way to lose.
2. **The guard layer is validated and worth keeping.** Real desk code, +14.5 pp median,
   drawdown cut by 60%. It should be wired into the live desk permanently — for
   whatever entry eventually ships.
3. **The signals layer remains untested.** Its actual chips (GoPlus traps, GMGN
   rug/bundler/insider, PumpPortal launch facts, Jupiter impact) cannot be backtested
   without historical snapshots. The only way to test them is forward, on paper. That
   is a data-collection project, not a backtest.
4. **Stop tuning this entry.** Two independent lines of evidence now agree: the
   zero-cost autopsy (−0.146%/trade on majors) and this unbiased sample. The
   three-lookback breakout carries no information about the next 24 bars.

## Honest limits

- **n = 12 pools.** Small. The sign is consistent with the zero-cost autopsy, which is
  why I trust it, but the magnitude is noisy.
- **Data drift.** Candles were refetched for this run, so the control lands at −23.25%
  / 644 trades versus −23.16% / 647 in `SURVIVORSHIP-G87E.md`. That is a fresh 1000-bar
  window, not a code change — proven by the 343-trade identity check above.
- **The volume gate is a proxy.** It is not `desk/signals.py`. Do not cite variant C
  as evidence about the real signal chips.
- **Three stablecoin pairs** (USDT/SOL ×2, USDC/SOL) still slipped the name filter.
  They are included for comparability with the survivorship run. Removing them makes
  the control slightly worse, not better.

## Reproduce

```bash
cd solana-desk
python3 scripts/guard_g87e.py     # ~2 min, 12 OHLCV calls, then cached offline
cat backtest_g87e_guard.json
```

Files: `scripts/guard_g87e.py`, `backtest_g87e_guard.json`, candle cache in
`data/g87e_surv/`. Gate hook lives in `desk/g87e.py::backtest(gate=...)`.
