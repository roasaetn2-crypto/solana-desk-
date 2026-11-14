# How to fix G87E — step by step

You asked how to fix it. This is the whole procedure, in order, with the exact
code. Nothing here is theory: every change was measured on 10 real Solana pools
across 3 timeframes before it went in this document.

Files in the repo:

| File | What it is |
|---|---|
| `g87e_original.pine` | your strategy, byte for byte, as uploaded |
| `g87e_fixed.pine` | the corrected version — paste this into TradingView |
| `desk/g87e.py` | Python port + backtester (baseline / fixed / tuned) |
| `BACKTEST_G87E_REPORT.md` | the numbers |

---

## Step 1 — Open the fixed file and paste it

In TradingView: Pine Editor → select all → delete → paste the contents of
`g87e_fixed.pine` → **Save** → **Add to chart**.

Do not try to edit the original in place. You will miss one of the ten changes
and the result will be worse than either version. Replace the whole script.

---

## Step 2 — The one-character fix that matters most

This is the bug. Everything else is a tuning issue; this one is a logic error.

```pine
// BEFORE — true on nearly every bar
if (close > close[3] or close > close[12] or close > close[30]) and strategy.position_size == 0
    strategy.entry("L", strategy.long, qty=tam)
if (close < close[3] or close < close[12] or close < close[30]) and strategy.position_size == 0
    strategy.entry("S", strategy.short, qty=tam)

// AFTER — all three must agree, and only one branch can fire
up3  = close > close[3]
up12 = close > close[12]
up30 = close > close[30]

longSig  = up3 and up12 and up30
shortSig = not up3 and not up12 and not up30

if flat and not cooling and qtyR > 0
    if longSig
        strategy.entry("L", strategy.long, qty=qtyR)
    else if shortSig and not longOnly
        strategy.entry("S", strategy.short, qty=qtyR)
```

Three separate things are fixed in those six lines:

1. `or` → `and`. Price must be above **all three** lookbacks, not any one.
2. `if` → `else if`. In the original the long branch is tested first and
   `strategy.entry` fires, so the short branch is dead code. Your strategy was
   long-only whether you knew it or not.
3. A cooldown gate, so it does not re-enter on the very bar after an exit.

---

## Step 3 — Stop the position size from rounding to zero

```pine
// BEFORE — for a token at $0.000002 this is 0.000, so no order is ever placed
tam = math.round(math.min(crudo, tope) * 1000) / 1000

// AFTER — the step scales to the price
qStep = math.max(math.min(math.pow(10, math.floor(math.log10(close)) - 6), 0.001), 0.000000000001)
qtyR  = math.floor(qty / qStep) * qStep
```

If you have ever looked at a meme coin chart with this strategy on it and seen a
flat line, this is why. The strategy was not idle because the market was quiet —
it was idle because every position it computed rounded to zero.

---

## Step 4 — Stop the ATR from dividing by zero

```pine
// BEFORE — atr = 0 on a flat bar, so crudo = risk / 0 = inf
atr = ta.atr(atrLen)

// AFTER — floor it at 0.05% of price
atrRaw = ta.atr(atrLen)
atr    = math.max(atrRaw, close * 0.0005)
```

---

## Step 5 — Split the trail into "when it arms" and "how far behind"

This is the single biggest performance change, and it is the one nobody thinks
to check.

```pine
// BEFORE — one number for both. Arms after 0.2xATR, trails 0.2xATR behind.
trailMult = input.float(0.2, "Trail ATR Multiple", ...)
strategy.exit("LX", from_entry="L",
     loss=(atr*stopMult)/syminfo.mintick,
     trail_points=(atr*trailMult)/syminfo.mintick,
     trail_offset=(atr*trailMult)/syminfo.mintick)

// AFTER — two inputs, and both are bigger
armMult = input.float(2.0, "Trail arms after ATR x", ...)
offMult = input.float(1.0, "Trail offset ATR x", ...)
strategy.exit("LX", from_entry="L",
     loss=(atr*stopMult)/syminfo.mintick,
     trail_points=(atr*armMult)/syminfo.mintick,
     trail_offset=(atr*offMult)/syminfo.mintick)
```

A 0.2×ATR trail on a 5m meme chart sits **inside the noise**. The trail arms
after a fifth of an ATR of profit and then follows a fifth of an ATR behind, so
normal chop stops you out at roughly breakeven. That is why the original wins
27.9% of its trades and still loses money: the wins are smaller than the spread.

Arming at 2.0×ATR means you only start protecting profit once the trade has
actually moved. Offsetting at 1.0×ATR gives it room to breathe.

---

## Step 6 — Give the trade time to work

```pine
// BEFORE
maxBars = input.int(5, "Max Bars In Trade", minval=2, maxval=20)

// AFTER
maxBars = input.int(24, "Max bars in trade", minval=2, maxval=200)
```

Five bars on a 5m chart is 25 minutes. A meme coin that is going to run 200%
does not finish in 25 minutes. The sweep tested 12 and 24; 24 won on median
return across all 23 runs.

---

## Step 7 — Reset the bar counter on exit

```pine
// BEFORE — `abierto` survives the stop-out, so maxBars can fire on a new trade
if strategy.position_size != 0 and not na(abierto) and bar_index - abierto >= maxBars
    strategy.close_all()
    abierto := na

// AFTER — reset on every flip to flat, and start the cooldown there
if strategy.position_size == 0 and strategy.position_size[1] != 0
    openedBar   := na
    lastExitBar := bar_index
```

---

## Step 8 — Size off live equity in the backtest too

```pine
// BEFORE — the backtest always sizes off the $50 start
refEquity = (liveMode and barstate.isrealtime) ? strategy.equity : 50.0

// AFTER — one behaviour, backtest and live
refEquity = liveMode ? strategy.equity : strategy.initial_capital
```

With the original, the equity curve you looked at on TradingView is not the
curve you would have traded. That is a very expensive way to be surprised.

---

## Step 9 — Put a floor under the position size

```pine
// BEFORE — risk sizing on a 20%-ATR token with a 2.5xATR stop deploys ~2% of the account
crudo = (refEquity * riesgo) / (atr * stopMult)
tope  = refEquity * 0.90 / close
tam   = math.round(math.min(crudo, tope) * 1000) / 1000

// AFTER — the 90% cap never binds, so add a floor as well as a ceiling
minQty = refEquity * minNotionalPct / 100.0 / close     // default 25%
maxQty = refEquity * maxNotionalPct / 100.0 / close     // default 90%
qty    = math.min(math.max(rawQty, minQty), maxQty)
```

Risk sizing is correct in principle, but on a token whose ATR is 20% of price
the formula returns a position so small that the fees eat it. Flooring at 25%
of equity took the fresh-meme median from +2.4% to **+7.9%**.

---

## Step 10 — Charge yourself the spread

```pine
// BEFORE — commission only, and Pine's `slippage` is in ticks
commission_value=0.05

// AFTER — 0.05% fee + 0.30% slippage, expressed as one percentage
commission_value=0.35
```

Pine's `slippage` input is measured in **ticks**, and a tick on a token priced
0.000002 is meaningless. Expressing the total round-trip cost as a percentage is
the only way to get an honest number. Set it to your real cost — if you are
trading through Jupiter on Solana with 0.3% slippage tolerance, 0.35% per side
is the right figure.

---

## The three numbers to change if you change nothing else

| Input | Original | Fixed | Why |
|---|---|---|---|
| `trail_arm_mult` | 0.2 | **2.0** | 0.2×ATR arms inside the noise, so winners become scratches |
| `max_bars` | 5 | **24** | 25 minutes is not enough time for a meme to run |
| `min_notional_pct` | — | **25%** | risk sizing on a high-ATR token makes dust positions |

Those three are the difference between the red curve and the green curve in
`backtest_g87e.png`.

---

## What you should expect after the fix

Honest numbers from the backtest, 23 runs, 0.05% fee + 0.30% slippage per side:

| | Trades | Win% | Mean ret/run |
|---|---|---|---|
| Your Pine as written | 2,658 | 27.9% | -28.2% |
| Fixed logic, same params | 1,887 | 28.3% | -23.7% |
| Fixed + tuned | 805 | 39.5% | -8.1% |

And split by what you are trading:

| Segment | Pine | Tuned |
|---|---|---|
| Majors (BONK/WIF/POPCAT/TRUMP) | -58.9%, 0/12 green | -22.5%, 0/12 green |
| Fresh memes | +5.4%, 9/11 green | **+7.5% mean, +7.9% median, 8/11 green** |

**The fix stops the bleeding. It does not make this a profitable strategy on
majors, because the entry has no edge there.** Run with zero fees and zero
slippage, the tuned entry averages **-0.146% per trade** on majors. There is
nothing to fix in the code that will turn a negative edge positive. On fresh
memes the edge is real (+4.355% gross per trade), which is the only place this
idea has life.

---

## How to verify the fix yourself

```bash
cd ~/solana-desk
python3 scripts/fetch_g87e_data.py      # cache real candles (once)
python3 scripts/backtest_g87e.py        # baseline vs fixed vs tuned
```

That rewrites `BACKTEST_G87E_REPORT.md`, `backtest_g87e.json` and
`backtest_g87e.png` from scratch. Change any input in `BASE` or `TUNED` at the
top of `scripts/backtest_g87e.py` and re-run — you get your own numbers in about
a minute, no TradingView subscription needed.

To sweep parameters:

```bash
python3 scripts/sweep_g87e.py           # 144 combinations, scored on median
```

---

## The thing I would tell you as a trader

The fix is mechanical and it is done. But the interesting result is not the
-28% → -8% improvement. It is that **the entry signal has no edge on liquid
pairs at all.** Three lookbacks agreeing is a trend-following proxy that fires
constantly, and constantly firing is how you donate the spread.

If you want this to make money, the work is not in the exits. It is in the
entry: a filter that fires rarely and only when something unusual is happening
— volume expansion relative to the token's own baseline, a liquidity floor, a
holder-growth check, a dev-wallet check. The desk already has all of those in
`desk/signals.py` and `desk/guard.py`. G87E is a chart pattern; the desk's
signals are a reason to trade. Combine them and the same exit logic becomes
worth something.
