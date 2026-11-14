# Social attention: reading the crowd, ticker by ticker

The meme scorecard (`desk/meme.py`) reads the token's **name**. It can tell you
`$DOGE` is an animal coin. It cannot tell you whether anyone is talking about it
this minute. For a memecoin that is the whole game, so `desk/social.py` adds the
missing half: **ticker-keyed attention**.

Everything here is free and needs no API key. That is a hard constraint, not a
preference — the desk is meant to run on a phone with no budget.

---

## 1. What it reads, and what it refuses to read

| source | what we get | key? | verified |
|---|---|---|---|
| Telegram public previews (`t.me/s/<channel>`) | last ~20 messages per channel, with view counts | none | yes, live |
| DexScreener `token-boosts/latest/v1` | every Solana mint someone paid to promote | none | yes, live |
| DexScreener `token-profiles/latest/v1` | the project's own X / Telegram / website links | none | yes, live |

**It does not read X/Twitter.** There is no free tier left, and scraping it is
both fragile and against their terms. Anyone claiming a free X reader is either
using a dead mirror or lying. The watchlist is Telegram plus DexScreener.

### The watchlist is short because most channels block previews

This was measured, not guessed. Of the big Solana/memecoin channels, only these
four serve a public web preview:

```
@binance_announcements   20 msgs   exchange listings: the loudest catalyst
@whale_alert             20 msgs   large transfers
@pumpfun                 20 msgs   new launches
@raydium                 20 msgs   Solana DEX pools
```

`@solanafloor`, `@dexscreener`, `@coingecko`, `@solanaofficial` and a dozen
others all return a "no public preview" page. The config lists only what works.

---

## 2. What it computes

For a ticker `$WIF`, `social.pulse("WIF", mint)` returns:

```json
{
  "available": true,
  "ticker": "WIF",
  "messages": 3,
  "views": 875,
  "channels_live": 4,
  "boosted": false,
  "profiled": false,
  "has_x": false,
  "has_telegram": false,
  "score": 25
}
```

The score is deliberately crude and bounded 0–100:

| component | points |
|---|---|
| messages | 8 each, capped at 40 |
| views | 1 per 1,000, capped at 30 |
| paid boost | 20 |
| has X link | 5 |
| has Telegram link | 5 |

It exists so the signal engine has a number to test, **not** because those
weights are known to be right. The measurement decides.

---

## 3. It is a camera, not a gate

`SOCIAL["shadow"]` is on and `SOCIAL["gate"]` is **off**. That means:

- every signal is tagged with `sig["social_pulse"]`
- the action is **never** changed
- a dead pulse can only downgrade a BUY once the gate is switched on, and that
  only happens if the measurement below clears the bar

Same rule as `desk/meme_tactics.py`: a social number you have not measured is a
story, not an edge.

---

## 4. The measurement

`scripts/social_eval.py` takes every currently-boosted (and profiled) Solana
mint, pulls its real GeckoTerminal candles, and runs the repo's standard signal
+ exit simulation. It then compares against the random-pool baseline from
`indicator_eval_fresh.json`.

The bar, stated before the run:

```
MIN_TRADES   = 30
MIN_POOLS    = 10      independent pools
MIN_WIN_LIFT = +5pp over baseline
MIN_PF       = 1.20, and not worse than baseline
```

`MIN_POOLS` is in the bar because trades inside one pool are not independent —
they share the same coin, the same day and the same move. This was learned the
hard way: the first run returned **"ADD"** on 45 trades from **3 pools**
(win 60.0%, PF 2.850). That is three observations, not forty-five. The pool
count is now part of the bar so the same mistake cannot pass twice.

### Result

| group | trades | pools | win | PF | verdict |
|---|---|---|---|---|---|
| **boost** | 686 | 12 | 17.3% | 0.532 | **DO-NOT-ADD** (win lift +2.0pp < +5pp; PF 0.532 < 1.20) |
| **profile** | 70 | 4 | 50.0% | 3.215 | **INSUFFICIENT** (only 4 independent pools, need 10) |

Baseline: n=3,629, win 15.4%, PF 0.503.

**What this says.** Paying DexScreener for a boost does essentially nothing:
+2.0pp of win rate on 686 real trades, and the profit factor stays at 0.532 —
still a losing strategy. Remember the survivorship bias runs *in favour* of this
group (dead boosted tokens drop off the feed before we look), so a failure here
is a strong failure. A paid promotion is not an edge.

The per-pool breakdown shows why the pool count matters:

```
boost, per pool:
  ANYq8tEL   44 trades  20.4% win  PF  0.861
  ExxzYBTt  101 trades  14.8% win  PF  0.283
  HsprHxBR  183 trades  20.2% win  PF  0.256
  NV2RYH95  109 trades   9.2% win  PF  0.216
  8Xg89xT8  101 trades   5.0% win  PF  0.051
  4W7BNgGs   42 trades  81.0% win  PF 10.430   <-- one coin, one lucky move
  ... 6 more pools, all 0-20% win
```

One pool returns 81% wins and PF 10.43. If you averaged only that pool you would
"prove" boosts work. Across twelve pools the truth is 17.3%. That is the whole
argument for `MIN_POOLS` in one table.

**What this does not say.** The profile group looks strong (50% win, PF 3.22) but
it is four coins. It is not evidence yet — it is a lead. It is labelled
INSUFFICIENT on purpose, and the gate stays off until ten pools say otherwise.

---

## 5. What we can and cannot claim

**Can claim:** whether a DexScreener boost or profile predicts the next few
hours, on real candles, against a stated bar.

**Cannot claim:** whether Telegram *mention velocity* predicts returns. That
needs snapshots collected over time — you cannot backtest a number you never
recorded. This is exactly what shadow mode is for: the desk records the pulse on
every signal today, and once enough positions close, the same blocked/allowed
comparison that `scripts/shadow_report.py` runs for the pullback rule can be run
for social attention.

**Known bias:** DexScreener's boost feed is "latest". A token that got boosted
and then died may have dropped off before we looked, which biases the boost
group *upward*. So a failure here is a strong failure, and a pass is weak
evidence.

---

## 6. Use it by hand

```bash
python3 scripts/social_report.py WIF          # one ticker
python3 scripts/social_report.py --mint <addr>  # with boost/profile lookup
python3 scripts/social_report.py --watchlist  # are the channels alive?
python3 scripts/social_report.py --boosted    # what is being promoted now
```

## 7. Reproduce the measurement

```bash
python3 scripts/social_eval.py --limit 22 --out social_eval.json
python3 -m pytest tests/test_social.py -q
```
