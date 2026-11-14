# Memecoin tactics: what the "high-profit meme bot" guide actually adds

The operator asked us to take a public guide
([*Building a high-profit cryptocurrency trading bot with meme coin support*](https://medium.com/@gwrx2005/building-a-high-profit-cryptocurrency-trading-bot-with-meme-coin-support-dc716e158b88))
and use it to raise the desk's win rate, on the reasonable observation that
memecoins are hard to predict because their price is driven by meme value and
social attention rather than cash flow.

This is what we measured, on real Solana candles, before changing a single
setting. The short version: **the guide's indicator toolkit is already
disproven here, three of its four memecoin rules do nothing, and the fourth —
"never buy the top" — is the only one that moves the win rate.** It clears the
bar only when paired with a wider stop and a slower take-profit, and it costs
96% of the desk's trade frequency. So it ships **off by default**, behind one
flag.

---

## 1. The indicator half was already tested and lost

The guide leans on RSI, MACD, moving-average crosses and Bollinger bands. This
repo had already run exactly that experiment on real GeckoTerminal OHLCV, with
the verdict bar stated *before* the run (`MIN_TRADES=30`, `MIN_WIN_LIFT=0.05`,
`MIN_PF=1.2`):

| run | file | baseline trades | baseline win | baseline PF | ADD entry | ADD exit |
|---|---|---|---|---|---|---|
| 80 pools | `indicator_eval.json` | 3,013 | 16.7% | 0.551 | **none** | **none** |
| 120 pools | `indicator_eval_120.json` | 5,974 | 14.8% | 0.492 | **none** | **none** |
| fresh 60 pools | `indicator_eval_fresh.json` | 3,629 | 15.4% | 0.503 | **none** | **none** |

Best of a losing field: RSI<35 (n=24, PF 1.13 — too few trades), EMA stack +
MACD (n=1,249, +1.4pp), ATR band (n=2,841, +0.6pp). None reached +5pp and
PF 1.2. Re-adding them would be dishonest, so we did not.

**Conclusion:** the guide's technical layer is noise on memecoins. Its *social*
layer — how attention behaves — is the only part worth testing.

---

## 2. The four memecoin rules, measured on 3,629 real trades

Harness: `scripts/meme_tactics_eval.py`, 60 real GeckoTerminal pools, 5-minute
candles, entry on every bar where the desk signal says BUY, forward-replayed
with the desk's own exit rules, 1% fee + 2% slippage both sides.

Baseline: **n=3,629, win 15.4%, PF 0.503.**

| tactic | variant | n | win | lift | PF | verdict |
|---|---|---|---|---|---|---|
| **A. ATR-scaled stop** | k=1.0 | 3,629 | 13.1% | −2.2pp | 0.444 | no |
| | k=1.5 | 3,629 | 13.6% | −1.8pp | 0.451 | no |
| | k=2.0 | 3,629 | 14.2% | −1.2pp | 0.468 | no |
| | k=2.5 | 3,629 | 14.5% | −0.9pp | 0.471 | no |
| **C. Flash-pump breaker** | 15% | 3,511 | 14.9% | −0.5pp | 0.478 | no |
| | 25% | 3,582 | 15.2% | −0.2pp | 0.487 | no |
| | 50% | 3,614 | 15.2% | −0.1pp | 0.489 | no |
| **D. Volatility ceiling** | ATR 10% | 3,429 | 14.8% | −0.6pp | 0.495 | no |
| | ATR 20% | 3,598 | 15.3% | −0.1pp | 0.501 | no |
| | ATR 40% | 3,627 | 15.4% | −0.0pp | 0.501 | no |
| **B. Pullback from 20-bar high** | 5% below | 1,003 | 23.0% | **+7.6pp** | 0.661 | no (PF) |
| | 10% below | 424 | 26.9% | **+11.5pp** | 0.745 | no (PF) |
| | 20% below | 145 | 33.1% | **+17.7pp** | 0.949 | no (PF) |
| | 30% below | 57 | 33.3% | +17.9pp | 0.791 | no (PF) |

Three rules are dead. **Pullback is real**: the deeper the dip you demand, the
higher the win rate climbs — 15.4% → 33.1% at a 20% pullback. But on its own it
still loses money, because the winners are small and the losers are full-size.

---

## 3. Pairing pullback with a wider stop and a slower ladder

The guide's second claim is "let winners run, don't get shaken out". So we swept
pullback depth × stop width × take-profit plan on the same entries
(`scripts/meme_tactics_deep.py`, 72 combinations). Only one cleared the bar:

| package | n | win | lift | PF | verdict |
|---|---|---|---|---|---|
| pullback 20% + stop 30% + TP 2x/3x | 145 | **30.3%** | **+15.0pp** | **1.587** | **ADD** |
| pullback 20% + stop 30% + quick 1.25x | 145 | 50.3% | +35.0pp | 1.187 | no (PF) |
| pullback 20% + stop 30% + default ladder | 145 | 37.9% | +22.6pp | 1.170 | no (PF) |
| pullback 10% + stop 30% + TP 2x/3x | 424 | 25.0% | +9.6pp | 1.001 | no (PF) |

The winner is `pullback 20% below the 20-bar high`, `stop 30%`, `TP1 2.0x for
33%`, `TP2 3.0x for 50%`, `trail 25%`.

**Overfit check.** Best-of-72 invites luck, so we split the 60 pools into two
halves and re-ran the winner on each:

| slice | n | win | PF | baseline PF |
|---|---|---|---|---|
| half A (pools 1–30) | 76 | 26.3% | **1.853** | 0.601 |
| half B (pools 31–60) | 69 | 34.8% | **1.270** | 0.375 |
| all | 145 | 30.3% | 1.587 | 0.503 |

It holds on both halves. That is not proof, but it is not a single lucky slice
either.

---

## 4. The honest cost, and why it ships off

The filter keeps **145 of 3,629** signal BUYs — **4%**. On a desk that already
complains about too few entries, switching this on would make it trade roughly
once every twenty-five candidates. That is a real trade-off, not a free win, so
the operator decides:

```python
# config.py
MEME = {
    ...
    "pullback_entry": False,     # <-- flip to True to enable the package
    "pullback_min_drop": 0.20,
}
```

When `pullback_entry` is True:

* `desk/meme_tactics.py` downgrades any BUY whose price is not at least 20%
  below the 20-bar high to HOLD, with the reason attached;
* `desk/exits.py` swaps in the measured ladder (stop 30%, TP 2x/3x, trail 25%)
  so the broker, the plan and the backtests all read the same numbers;
* `desk/solana_agent.py` fetches the 20-bar high for finalists from the chart
  feed (only when the flag is on, so the default desk makes no extra calls).

The other three rules (`flash_pump_guard`, `atr_stop`, `vol_ceiling`) are also
implemented and tested, and also default to `False`, because they measured
*negative* or zero. They are kept switchable so the operator can re-run the
experiment on a new sample without rewriting code.

---

## 5. What we could not test, and will not claim

The desk's real edge is not the raw signal — it is the **entry gates**. The
existing ladder (`scripts/backtest_gates.py`) shows that plainly:

| rung | trades | win | PF |
|---|---|---|---|
| no gates | 101 | 16.8% | 0.941 |
| + SCAN floors | 14 | 35.7% | 1.563 |
| + trades & sell ratio | 12 | 41.7% | 2.050 |
| + volume still arriving | 5 | 40.0% | 2.176 |

We wanted to test pullback **on top of** those gates, because that is what the
live desk actually trades. We could not do it honestly: 101 pools produce only
5–14 gated trades, and the repo's bar requires at least 30. Reaching 30 gated
trades needs roughly 600 pools, which the free GeckoTerminal tier rate-limits
well past this session's budget.

So the pullback package is measured on the **ungated** signal and labelled as
such. It is a promising filter, not a proven one on the fully-gated desk. That
distinction is written here on purpose — the whole point of this repo is that a
number you cannot reproduce is not a number you get to quote.

---

## 6. Reproduce it

```bash
python3 scripts/indicator_eval.py --pools 60 --out indicator_eval_fresh.json
python3 scripts/meme_tactics_eval.py --pools 60 --out meme_tactics.json
python3 scripts/meme_tactics_deep.py --out meme_tactics_deep.json
python3 -m pytest tests/test_meme_tactics.py -q
```

Artifacts: `indicator_eval_fresh.json`, `meme_tactics.json`,
`meme_tactics_deep.json`, `data/indicator_paths.json` (the 60-pool candle
cache these runs share).
