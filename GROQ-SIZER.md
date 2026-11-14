# Groq size multiplier — shrink-only

`desk/groq_sizer.py`. A second opinion on **ticket size only**. It cannot grow a
ticket, block a trade, move a stop, or admit a setup the rules already refused.

## The contract

```
mult = clamp(groq_response, 0.25, 1.0)
```

That single line is the entire safety story. Everything else in the module exists
to make the call cheap, fast, and measurable.

| Property | Behaviour |
|---|---|
| Can grow a ticket | **No.** Clamped to 1.0 |
| Can block a trade | **No.** Minimum is 0.25x, never 0 |
| Can move a stop | **No.** Never sees or touches one |
| Can admit a refused setup | **No.** Runs after every rule has agreed |
| On timeout / 429 / bad JSON | Returns **1.0** and the trade proceeds at rule size |
| On missing API key | Disabled, returns 1.0 |
| Max latency added | 4s ceiling (`BRAIN_SIZER_TIMEOUT_S`) |

## Why shrink-only

Expected value is `Σ wᵢ·eᵢ`. If every `eᵢ` is negative, no choice of weights makes
the sum positive. **Sizing amplifies selection; it never creates it.** So the model
gets the one lever that cannot hurt you when it is wrong: reduce exposure.

This was not a theoretical concern. A threshold sweep on 2,446 setups produced
expectancy of +0.065, +0.023, −0.034, **+0.099**, −0.074, −1.289, −5.137 as the
filter tightened. Non-monotonic — a real effect strengthens under a tighter filter;
this one spikes and collapses. The best-looking two-condition filter (+0.740% on
n=129) was **one pool out of five**. That is what data snooping looks like, and it
is exactly what an unbounded model would produce confidently in fluent English.

## Wiring

In `solana_agent._caution()`, after every rule cut has been applied:

```python
gmult, groq_info = groq_sizer.size_mult({...}, mint=..., symbol=...)
if gmult < 1.0:
    cuts.append(("groq", gmult))
mult, names = guard.size_multiplier(cuts)     # existing floor of 0.25 still applies
```

`guard.size_multiplier` multiplies all cuts and floors at `MIN_SIZE_MULT = 0.25`,
so Groq stacks with the security, regime, breaker and Jupiter cuts rather than
replacing them. `risk.py` is untouched — its docstring says *"No model touches this
file. Ever."* and that remains true.

The multiplier is stored on the position (`broker.buy(..., groq_mult=...)`) so the
close path can journal realised P&L against it.

## The measurement that decides whether this was worth building

Every call journals its full input, raw output, and clamped multiplier. Every close
journals `groq_sizer_pnl` with the multiplier and the realised P&L.

```
python3 scripts/groq_sizer_report.py
```

```
  mult     n   avg P&L $  win rate
   1.0   412      0.0812       41%
  0.75    88     -0.0140       33%
   0.5    54     -0.0903       24%
  0.25    31     -0.1204       19%

verdict: monotonic: lower multiplier -> lower P&L. Groq has ranking skill.
```

Read it strictly:

- **Expectancy falls as the multiplier falls** → the model ranks. Keep it.
- **Flat or inverted** → no signal. `BRAIN_SIZER_ON=0` and stop paying for it.
- **Fewer than 30 closes in a band** → noise. The report says so out loud.

This is the only version of "Groq controls risk" that ever becomes knowable. A
model that sets an absolute risk percentage produces an opinion nobody can test.

## Quota

| Knob | Default | Effect |
|---|---|---|
| `BRAIN_SIZER_MODEL` | `openai/gpt-oss-20b` | Small model — triage is pattern-matching |
| `BRAIN_SIZER_BUDGET` | 40 | Hard daily call cap, persisted to disk |
| `BRAIN_SIZER_CACHE_TTL_S` | 600 | One call per mint per 10 min |
| `BRAIN_SIZER_TIMEOUT_S` | 4 | Fail-open ceiling |
| `BRAIN_SIZER_ON` | 1 | `0` disables the layer |

40 calls/day is **10% of the 400/day desk budget**, and the cache means a mint
scanned repeatedly costs one call. The separate `BRAIN_SIZER_BUDGET` counter is
persisted in `data/groq_sizer_state.json` so a restart cannot reset the burn.

## What this does not fix

The underlying entry has no measured edge: median −22.5% on unbiased pools, zero
green out of twelve, and a reward-to-risk of 0.52:1 needing a 66% win rate from a
signal that delivers 27.9%. A shrink-only multiplier makes those losses smaller.
It does not make them profits.

The highest-value change in this repo is still `solana_agent.py` line 551 —
`if self.mode != "live":` — which means the circuit breaker never runs in live
mode, and every live entry is separately rejected by the `paper mode` checklist
item. Live trading is broken in two independent ways.
