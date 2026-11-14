# The guard was only guarding the simulator

Line 551 of `desk/solana_agent.py` read:

```python
if self.mode != "live":
    self.regime = ...
    brk = guard.circuit_breaker(...)
```

That one condition meant the discipline layer was a paper-only feature. On the
only book where it matters — real money — none of it ran.

## What was actually broken

Three defects, and the second is worse than the line that got reported.

**1. The drawdown breaker never ran in live mode.** `self.breaker` stayed at its
constructor value `{"state": "ok", "mult": 1.0}` forever. The circuit breaker
could not trip on a real losing streak or a real day-loss breach.

**2. `_caution()` was never called on the live path at all.** It was invoked only
inside the `else:` (paper) branch of the seat-4 block. So a real fill skipped:

- the security halve-flags (GoPlus `halve` findings),
- the RISK_OFF regime halve,
- the breaker size cut,
- the Jupiter price-impact cut,
- the pre-trade checklist.

The `paper_only=self.mode != "live"` argument inside `_caution()` was dead code:
in live mode the function was never reached to evaluate it.

**3. The checklist item was semantically backwards.** `guard.pretrade_checklist`
had `("paper mode", bool(paper_only))`. Even if `_caution()` had been reached in
live mode, that line would have failed every real fill, because `paper_only`
would be `False`.

## The fix

- The regime read and the `guard.circuit_breaker()` call moved out of the
  `mode != live` gate. They now run in both modes. The regime read is wrapped in
  `try/except` so a regime outage degrades to `UNKNOWN` (no halve) instead of
  bricking entries.
- `_caution()` is now called in the live branch too. Live builds an `Account`
  from `self.live` and passes `max_ticket=caps["max_trade"]`, so the per-trade
  cap is the hard live USD cap, not a percentage of a paper equity base.
- `_caution()` gained a keyword-only `max_ticket` override. Paper leaves it
  `None` and keeps the percentage-of-equity cap.
- The checklist item is now `("mode: paper, or live is armed", paper_only or
  live_armed)`. `live_armed` is true only when `mode == "live"`, the active
  broker really is `self.live`, and `LIVE_DISABLED` is false. If the desk claims
  live but the broker fell back to paper, the checklist fails — which is correct.
- `LiveBroker.buy()` now accepts `groq_mult` (the agent passes it on every buy;
  without it every real fill would have raised `TypeError` at the last step) and
  stores it on the position. `LiveBroker.sell()` journals the realised P&L
  against that multiplier, so `groq_sizer.buckets()` measures ranking skill on
  real fills too.
- The "price confirmed on a second read" checklist item is appended in both
  modes: paper re-quotes via `scanner.quote`, live re-quotes through Jupiter
  inside `LiveBroker.buy()`.

## Verification

| Check | Result |
|---|---|
| Full suite | **717 passed, 17 skipped** |
| Control backtest (`guard_g87e.py`, variant A) | **644 trades, 12 pools, −23.25% median, 27.9% win** — identical to the documented control |
| Breaker trips in live mode | new test seeds 3 losses, forces `mode="live"`, asserts `breaker["state"] == "cooldown"` |
| `LiveBroker.buy` accepts `groq_mult` | new signature test |
| Checklist permits a live fill when armed | new assertion; refuses when `mode=live` but broker is not live |

The paper path is behaviourally unchanged: the breaker already ran there,
`_caution()` already ran there, and the checklist item already passed there
(`paper_only=True`). The 644-trade control confirms it.

## What this still does not fix

The live path is still unreachable in this repo: `desk/live.py` sets
`LIVE_DISABLED = True` at line 41. This change makes the guard correct for the
day live is armed; it does not arm it. Arming live remains a separate, deliberate
decision.
