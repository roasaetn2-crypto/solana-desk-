# AI Trading Desk — Solana Agent

A faithful, runnable rebuild of the [SpikeCalls/ai-trading-desk](https://github.com/SpikeCalls/ai-trading-desk) architecture — the narrow-seat crypto desk — extended with a dedicated **autonomous Solana Agent**, a **meme-coin value desk**, and a live web dashboard.

The original five seats are kept exactly as designed. Three evidence seats are added for the Solana meme market: a **DexScreener market view**, a **dev/creator investigation** (RugCheck), and a **meme value scorecard** that answers *what gives this coin value*. A **seat-to-seat dialogue** layer lets the seats argue with each other before any ticket is written.

> **The AI reads. The code decides. You sign.**
> Paper only. No private keys, no signing, no seed phrases, no live orders anywhere in this repo.

---

## Why it is built this way

The original desk is a set of narrow, replaceable seats. Each seat has one job and a hard boundary. The language model is allowed to *read* and *argue*, but it is never allowed to *size* or *place* a trade. Sizing is pure Python, and the final signature is a human clicking in their own wallet.

This project keeps that contract intact and adds a Solana-native agent that runs the whole pipeline on a loop.

---

## The seats

Seven seats. Each has one job, one file, and a hard boundary. Evidence seats (2b, 2c) can veto before the analyst ever speaks.

| # | Seat | File | What it does | What it may **not** do |
|---|------|------|--------------|------------------------|
| 1 | **Scanner** | `desk/scanner.py` | Pulls new Solana pools from GeckoTerminal, applies free cuts (age, liquidity, volume, trades, sell ratio, mcap), extracts socials, quotes price | Touch a wallet |
| 2 | **Chain facts** | `desk/chain.py` | Solana JSON-RPC: mint/freeze authority, supply, real top-wallet concentration — excluding program-owned accounts (bonding curve / pool) so the curve is never counted as a whale | Interpret narrative |
| **2b** | **Dev investigation** | `desk/devcheck.py` | RugCheck report + RPC fallback: creator bag, creator's prior rugs, sniper/bundle share, LP lock, mint authority. Produces a 0–100 `dev_score` and a hard-no list | Forgive a rug history |
| **2c** | **Meme value** | `desk/meme.py` | Answers *what gives this coin value*: community, dev legitimacy, narrative, distribution, utility — weighted to 0–100, plus the hold horizon | Score a coin it has no data for |
| 3 | **Analyst** | `desk/analyst.py` | Produces a typed `TokenThesis` (verdict, score, flags, shape, invalidation, sources). Any configured brain (Ollama, Groq, Gemini, OpenRouter, Claude), otherwise a deterministic heuristic | Choose size, place orders, invent facts |
| **3b** | **Signal engine** | `desk/signal_engine.py` | Turns every seat's read into **one deterministic BUY / SELL / HOLD / AVOID call**, with a bullish/bearish direction, a confidence, a calibrated win probability, and an entry / stop / take-profit plan. Optional entry gate and bearish-flip exit | Size, place orders, or override a security block |
| 4 | **Risk engine** | `desk/risk.py` | Pure code. Three-cap sizing: `min(risk cap, position-size cap, 2% of pool liquidity)`, fee floor, quiet hours, daily loss stop, max positions | Be overridden by the model |
| 5 | **Paper broker** | `desk/broker.py` | Simulated fills and rule-based exits: stop −35%, take half at 2× once, volume decay, max hold 24h, no quote = close | Sign anything |

**DexScreener** (`desk/dexscreener.py`) is a second, independent market view used by seats 2b and 2c: price, liquidity, FDV/mcap, 5m/1h/6h/24h volume, buy/sell counts per window, pair age, active boosts, socials. If GeckoTerminal and DexScreener disagree, the desk sees both.

**Dialogue** (`desk/dialogue.py`) is the speech layer: scanner → chain → dev → analyst → risk → analyst → risk → broker, in that order, with risk always having the last word on size.

**Ops** (`desk/ops.py`) wraps the desk: append-only `journal.jsonl`, a SQLite `desk.db` bench, alerts, and a `KILL` file kill switch.
A read-only backtest (`desk/backtest.py`, also a Backtest card on the dashboard) replays that journal against other gate settings and never changes the live desk.

### The typed thesis

The analyst must return a `TokenThesis`:

```python
verdict:     "skip" | "watch" | "valid_setup"
score:       int, clamped to 0..10 in code
red_flags:   list[str]
green_flags: list[str]
shape:       "crowd" | "one_buyer" | "fading" | "too_early"
invalidation: str
sources:     list[str]
```

The score is clamped **in code**, not in the prompt. A model that returns `score: 99` still gets `10`, and a `valid_setup` with a score below `MIN_SCORE` still gets vetoed by the risk engine.

### Untrusted input

Every third-party string (token name, symbol, socials) is wrapped before it reaches the model:

```xml
<pasted_content id="a3f9c1d2e4b5...">
  ...untrusted text...
</pasted_content>
```

with a random hex id per call, so prompt-injection text inside a token name is treated as data, not instructions.

### Three-cap sizing

```
ticket = min(
    equity * risk_per_trade / stop_distance,   # risk cap
    max_position_usd,                          # size cap
    liquidity_usd * 0.02,                      # pool cap
)
ticket = max(ticket, fee_floor)                 # never dust
```

The smallest cap always wins. The model cannot raise it.

---

## What gives a coin value

Seat 2c scores five things and nothing else. The weights live in `config.MEME["weights"]`:

| Part | Weight | What it measures |
|------|--------|------------------|
| **Community** | 0.30 | Holder count, buy/sell balance, social presence (X, Telegram, website), whether real people are on both sides |
| **Dev legitimacy** | 0.25 | Creator bag, creator's prior rugs, sniper share, LP lock, mint authority — from `devcheck` |
| **Narrative** | 0.20 | Which story the coin is selling: **animal**, **celebrity**, **community**, or **utility** |
| **Distribution** | 0.15 | Top-wallet concentration, real top-10 share with curve/pool excluded |
| **Utility** | 0.10 | Is there anything to do with it beyond holding |

The weighted total is 0–100 and maps to a verdict band:

- **70+** — `community_coin`: a real community with a defensible story.
- **50–69** — `watch`: interesting, not yet a trade.
- **<50** — `not_a_community_coin`: the desk cuts it before the analyst is called.

Narrative classification is deterministic keyword matching (`meme.classify_narrative`), priority **celebrity > animal > utility > community**, so `DOGE`-style animal names and `TRUMP`-style celebrity names are tagged without a model call.

### Dev account: is it legit?

`desk/devcheck.py` asks four hard questions and any one of them is a veto:

1. **Does the creator still hold a bag?** More than `max_dev_hold_pct` (5%) → `dev_hold`.
2. **Has this creator rugged before?** Any prior rug → `creator_rug_history`.
3. **Were snipers/bundles loaded at launch?** More than `max_sniper_pct` (20%) → `sniper_bundle`.
4. **Is mint authority still live?** → `mint_authority`.

If RugCheck is unreachable the seat degrades to RPC-only facts and says so; it never invents a clean report. A passing coin gets a `dev_score` (0–100), a `legit` flag, and a list of reasons for and against.

### Hold horizon: match the chart to the hold

The desk states the horizon *before* it sizes a ticket, because a fresh launch is not traded on a weekly chart:

| Token age | Hold | Chart | Rule |
|-----------|------|-------|------|
| < 60 min (fresh launch) | 10–60 minutes | 15s or 30s | Only draw lines once price has built memory |
| < 6 h (first hours) | a few hours | 1m or 5m | Trade the structure, not the first candle |
| < 2 days (couple of days) | 1–3 days | 15m, 1H or 4H | Higher timeframe decides, lower one times the entry |
| older (long hold) | weeks+ | weekly | Only the weekly trend matters at this horizon |

`config.HOLD_HORIZONS` is the single source of truth; the analyst sees it in its context and the dashboard shows it live.

---

## The seats talk to each other

`desk/dialogue.py` produces a transcript for every decision, in a fixed order:

```
Scanner -> Chain -> Dev -> Analyst -> Risk -> Analyst -> Risk -> Broker
```

Each message carries a stance — `info`, `support`, `warn`, `veto`, or `accept` — so the transcript reads as an argument, not a log. Risk always speaks last on size; the broker only fills if risk accepted.

Two backends:

- **`template`** (default, always available): deterministic, built from the same facts the seats used. Honest and reproducible.
- **model-backed** (when any brain is configured — see [The brain](#the-brain-use-a-free-ai)): the seats are given personas and the same facts, and are asked to argue in `seat>seat:` lines. If the model output cannot be parsed into at least four lines, the desk silently falls back to the template.

The dashboard renders the live transcript in the **Seat dialogue** panel, with the backend labelled.

---

## The BUY / SELL signal (one call, every coin)

`desk/signal_engine.py` is a **pure, deterministic** function — no clock, no model, no I/O. Given the same facts the seats already collected, it produces one recommendation per coin:

```python
signal_engine.evaluate(token, facts, links, thesis=..., scorecard=...,
                       security=..., regime=..., dev=..., holding=False)
```

It returns:

| field | meaning |
|-------|---------|
| `action` | `BUY` · `SELL` · `HOLD` · `AVOID` |
| `direction` | `bullish` · `bearish` · `neutral` |
| `confidence` | 0–100, conviction **in the direction** (a strong bearish read is high-confidence, not low) |
| `win_probability` | 0–1, calibrated from the composite edge (logistic, bounded by `SIGNAL["win_prob_floor"/"win_prob_cap"]`) |
| `plan` | `entry`, `stop`, `tp1`, `tp2`, `risk_reward` |
| `signal` | one-line emoji string for the dashboard, e.g. `🟢 BUY $WIF · bullish 78% · win 61% · entry … · TP1 1.5x …` |
| `prompt` | the same call in a sentence: *"BUY $WIF: bullish with 78% confidence … take profit 1.5x …"* |

The composite is a weighted blend of six components, each scored `-1..+1`:

```
momentum 0.24 · flow 0.22 · liquidity 0.16 · holders 0.14 · narrative 0.12 · safety 0.12
```

**Safety always wins.** Any hard security flag or dev hard-no forces `AVOID` regardless of how bullish the rest looks. The signal never sizes a trade and never places an order — the risk engine and broker still own that.

### How it plugs into the desk

- **Advisory:** every decision record carries the full `signal`, it is journaled, and it is emitted as a `signal` event so the dashboard shows the BUY/SELL call next to the thesis.
- **Entry gate:** after the risk engine approves, `signal_engine.allows_entry(sig)` can refuse an entry unless the read is bullish, above `signal.min_confidence`, and above `signal.min_win_probability`. Turn it off with `signal.enabled = false`.
- **Bearish-flip exit:** in `_manage_positions`, if no rule-based exit has fired and a held position's read flips bearish above `signal.sell_confidence`, the desk cuts it (`signal.sell_on_bearish`).

### Take-profit plans

The exit ladder is operator-selectable via `exits.tp_plan` (config key `EXITS["take_profit_mode"]`):

- **`profile`** (default): the per-horizon scalp/swing ladder, exactly as written.
- **`global`**: one ladder for every trade — `EXITS["global"]` (`tp1_at`, `tp1_frac`, `tp2_at`, `tp2_frac`, `trail_pct`).
- **`single`**: one take-profit + a trail for every trade — `EXITS["single"]` (`tp_at`, `tp_frac`, `trail_pct`), TP2 disabled.

All of these are editable from the operator settings panel (Confirm required) under the **Exits** and **Signal** groups.

### Backtesting the signal

`desk/signal_backtest.py` + `scripts/backtest_signals.py` answer two questions honestly:

```bash
# real Solana pool OHLCV from GeckoTerminal (same source as backtest_100)
python scripts/backtest_signals.py --trades 40 --out backtest_signals.json --chart backtest_signals.svg

# no network? labelled synthetic plumbing run (not a performance claim)
python scripts/backtest_signals.py --offline --trades 60
```

It reconstructs the point-in-time features the signal can see from **price + volume alone** (momentum, flow), asks the engine for a call at each entry, then measures:

1. **Direction accuracy** — did the bullish/bearish call match the forward move?
2. **Win rate by confidence bucket** — do higher-confidence reads actually win more?
3. **Gate lift** — win rate / P&L of *every* entry vs *signal BUYs only*.
4. **Take-profit sweep** — replays the same BUYs under six TP plans (profile, global, single, quick, patient, tight-trail) and reports win rate, avg return and profit factor, so you can see which plan extracts the most P&L.

Holders, narrative and safety are neutral in the backtest (OHLCV history has no such facts), so the result speaks only to the momentum+flow edge — not the full live signal. Every price is real; nothing is invented.

---

## The Solana Agent

`desk/solana_agent.py` is the addition. It is an autonomous worker that runs the full seven-seat pipeline on a background thread:

- **start / stop / step / reset / kill** lifecycle, exposed over REST.
- Runs a cycle every `AGENT["cycle_seconds"]` (default 45s).
- Per-cycle budgets: `chain_budget` (14 RPC checks) and `analyst_budget` (3 model calls) so a noisy scan cannot burn your quota.
- Manages open paper positions every cycle: marks to the latest quote and applies the broker's rule-based exits.
- Keeps bounded ring buffers of `events` and `decisions`, journals every action, and alerts on entries/exits.
- **Transparent demo mode:** if a live scan yields fewer than 3 pools, it tops up with synthetic pools tagged `source="demo"` (hash-seeded facts, random-walk quotes) so the desk is always demonstrable. Live data is tagged `source="geckoterminal"`; you can always tell them apart.

### Data sources

- **GeckoTerminal API v2** — `https://api.geckoterminal.com/api/v2/networks/solana/new_pools`
- **DexScreener** — `https://api.dexscreener.com/latest/dex/tokens/{mint}` (market view, volumes, buys/sells, boosts, socials)
- **RugCheck** — `https://api.rugcheck.xyz/v1/tokens/{mint}/report` (dev/creator legitimacy)
- **Solana JSON-RPC** — `getAccountInfo`, `getTokenLargestAccounts`, `getMultipleAccounts`
- **Helius** (optional) — DAS `getAsset`, `getTokenAccounts`, `getPriorityFeeEstimate`

Program-owned token accounts (bonding curve, pool vaults) are detected via the SYSTEM_PROGRAM owner check and excluded from holder concentration.

---

## Do you need Helius?

**No.** The desk is built to run with zero paid infrastructure, and it does: public
RPC plus three free HTTP APIs. Helius is an upgrade you can add with one
environment variable, and it buys four specific things.

| What | Without Helius | With Helius |
|---|---|---|
| **RPC** | `api.mainnet-beta.solana.com`, aggressively rate-limited; a busy cycle can get throttled | a real node with a real quota |
| **Holder count** | top-20 accounts only, used as a proxy | true count of every account with a balance |
| **Token metadata** | whatever the listing published | the token's own name, symbol, image, socials |
| **Priority fee** | none | live `medium` estimate per transaction |

```bash
export HELIUS_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

That is the whole setup. `desk/chain.py` resolves the endpoint per call, so
`HELIUS_API_KEY` → `SOLANA_RPC_URL` → public node, in that order. The dashboard
shows which one is live (`RPC: helius · holder counts on` or
`RPC: public · free public node`).

### What Helius actually changes in the decision

The honest answer is **one number and one input**:

1. **The community score.** `MEME["min_holders"] = 150` is a claim about the whole
   distribution. The public RPC can only see the top 20 accounts, so without
   Helius the desk falls back to `wallets_counted` — which is 20 at most — and
   almost every token looks like it has no community. With Helius, 900 real
   holders score 30/30 on community instead of 3/30. This is the difference
   between the meme seat being useful and being a rubber stamp.
2. **The narrative tag.** Animal / celebrity / utility / community classification
   reads the token's name and website. GeckoTerminal often gives neither. Helius
   metadata fills them in, so `WIF` is tagged `animal` on its real name rather
   than on a symbol string.

Everything else — mint authority, freeze authority, top-wallet concentration,
creator bag, rug history, sniper bundle, liquidity, volume, buys/sells — works
identically without a key.

### What Helius does *not* do

It does not make the desk profitable, it does not give you an edge, and it does
not place orders. It is plumbing. A rate-limited public node and a paid node see
the same chain; one of them just answers more often. If your bot is losing money,
a better RPC endpoint will not fix the strategy.

**Webhooks** are the one Helius feature this desk does not use yet. Polling
GeckoTerminal every 45 seconds means you see a pool up to 45 seconds late. A
Helius webhook on new pool creation would cut that to near-zero, but it requires
a public HTTPS endpoint to receive the callback — which is a real deployment
decision, not a config flag. The polling path stays the default.

### The credit math (and why there is a cache)

Helius bills in credits: 1 for an RPC call, **10 for a DAS call**, 1 for a
priority-fee estimate. The free tier is 1M credits/month at **2 DAS req/sec**.

Uncached, that is fatal. At a 45-second cycle with a 14-token chain budget:

```
14 tokens x (getAsset 10 + getTokenAccounts 10) = 280 credits per cycle
280 x 1,920 cycles/day = 537,600 credits/day
1,000,000 / 537,600 = the free tier dies in under two days
```

It also costs time: 28 DAS calls at 2 req/sec is 14 seconds of a 45-second
cycle spent waiting.

So `desk/helius.py` caches, with a TTL matched to how fast each fact actually
changes:

| Call | TTL | Why |
|---|---|---|
| `metadata` | 6 hours | a token's name and socials do not change |
| `holder_count` | 5 minutes | the distribution moves, but not every 45 seconds |
| `priority_fee` | 30 seconds | this one genuinely moves constantly |

A failed lookup is **never** cached, so a transient outage is retried on the next
cycle instead of being remembered as "this token has no holders". With the cache
the same workload lands around 100–300k credits/month, comfortably inside the
free tier.

If you outgrow that, the Developer plan is $49/month for 10M credits and 50 RPS.
You do not need it to run this desk.

---

## Paper or live: the switch

The desk boots in **paper** every single time. That is not a default you can
change in a config file and forget about: `SolanaAgent.__init__` sets
`self.mode = EXECUTION["default_mode"]`, and that value is `"paper"`. Restart
the process and you are back in paper, even if you were live a second ago. A
crash can never leave you silently trading.

There are two brokers behind one interface. `PaperBroker` fills against the
quoted price with a 1% fee and 2% slippage. `LiveBroker` (in `desk/live.py`)
inherits the same ledger shape, the same exit rules and the same snapshot, and
overrides only `buy` and `sell` so the fill is a real Jupiter swap. The agent
loop does not know or care which one it is holding.

### Arming live

Three things must be true, in this order:

1. `SOLANA_PRIVATE_KEY` is in the environment. Base58 or a JSON byte array. It
   is read on demand, never written to disk, never logged, and scrubbed out of
   every error string by `live.redact()` before it can reach the journal or the
   API.
2. `solders` and `base58` are installed (`pip install -r requirements-live.txt`).
   Without them paper still runs; live simply refuses to arm and says why.
3. You type the exact phrase from `EXECUTION["live_confirm_phrase"]` into the
   dashboard and press **Arm live**. The phrase is a speed bump, not a secret.

```bash
export SOLANA_PRIVATE_KEY="your base58 secret key"
./run.sh
# dashboard -> Execution mode -> Arm live -> type the phrase
```

The wallet address is shown so you can check it is the one you meant. The key
itself is never shown, anywhere, by anything.

### The caps (two locks, not one)

Seat 4 sizes live tickets from the live caps, not from the paper percentages.
Paper sizes ~$3-5 tickets on a $20 book; live sizes from the money you actually said you
are willing to risk. `check_live_trade()` in `desk/risk.py` takes the smallest
of the per-trade cap, what is left of the total notional cap, and 2% of the
pool. Then `LiveBroker._guard_buy()` re-checks every one of them before it
builds a transaction, plus the kill switch and the daily loss stop. If the two
disagree, the trade does not happen.

| Cap | Default | What it means |
|---|---|---|
| `live_max_trade_usd` | $25 | no single live ticket above this |
| `live_max_notional_usd` | $100 | total live money at risk at once |
| `live_daily_loss_pct` | 3% of the notional cap ($3) | down this much today, no new buys |
| `live_max_slippage_bps` | 300 (3%) | wider than this and the route is a trap |
| `max_open_positions` | 3 | shared with paper |

Live mode also refuses synthetic `DEMO…` mints outright. The demo feed exists
so the rules get exercised with no market; it must never be able to reach a real
swap, and `_scan()` will not even produce demo pools while the mode is live.

### What live actually does

Cash is USDC, so the ledger stays in dollars and never drifts with the SOL
price. A buy is: quote USDC → token on Jupiter, build the swap, sign the
`VersionedTransaction` locally with `solders`, send it to the RPC, and poll
`getSignatureStatuses` until it is confirmed or the timeout fires. A token with
no direct USDC route still fills, because Jupiter builds the multi-hop through
SOL itself. A sell is the same path in reverse, and it is never blocked by the
daily loss stop or the caps — you can always try to get out.

### What can still go wrong

This is the part that matters. A live fill is not a paper fill:

- **Slippage and price impact are real.** The quote you see is not the price you
  get if the pool moves while your transaction lands.
- **MEV and sandwiching are real.** A public memecoin pool is an adversarial
  environment. A 3% slippage cap limits the damage; it does not remove it.
- **Honeypots and rugs are real.** The desk checks mint/freeze authority, dev
  holdings, creator history and holder distribution, and it still gets fooled.
- **Fees are real.** Jupiter routing, priority fees, and pump.fun's curve fee all
  come out of the ticket. A $10 ticket can lose a meaningful slice before the
  price moves at all.
- **Transactions fail.** Preflight rejection, dropped transactions, expired
  blockhashes. The desk surfaces the error and does not retry blindly.
- **The brain can be wrong.** The analyst is a model reading public data. It does
  not know the future and it cannot see the wallet that is about to dump on you.

If any of that is not acceptable, stay in paper. The desk is a research tool
that can be switched into a small-money execution tool, not a money printer.

### A bad quote must never reprice the book

On 2026-11-14 the dashboard showed an equity of **$549,195,436.20** on a $160
paper bankroll — a 3,432,471x multiple and a +343,247,105% day. That was not a
trade result. One open position had been marked from a quote that was not a
price for that token: a decimals mismatch, a different pool for the same mint,
or a duplicated pair. The desk trusted the print, wrote it into `last`, and the
whole book was repriced from it.

Three independent guards now make that shape of bug impossible:

1. **`desk/exits.py: plausible()`** — a quote is refused if it is outside
   `[entry × min_mark_mult, entry × max_mark_mult]` (0.001x to 1000x the entry
   price). No number of agreeing reads makes a 1e9x print real, so this band is
   absolute. Inside the band, a single tick may still not jump more than
   `quote_max_jump_x` (25x) or fall further than `quote_max_drop_x`; a rejected
   print is remembered and accepted only once a second read lands within 25% of
   it, so a genuine gap-up costs one extra tick instead of being blocked.
   A refused print never touches `last`, `peak`, the equity or a sell fill, and
   after `missing_quote_closes` refusals the position is closed at the last
   **good** price.
2. **`desk/broker.py: _value()`** — even if a corrupt mark is already on disk, a
   position outside the same band is valued at what it cost. `clamped()` lists
   what was overridden, so the clamp is visible instead of silent.
3. **`desk/broker.py: _repair()`** — runs on every boot. It resets impossible
   marks to entry, drops a closed trade whose P&L exceeds anything the book
   could have produced, and restores `cash` from the double-entry identity

   ```
   cash = start + Σ closed P&L + Σ open proceeds − Σ open cost
   ```

   so a book that is already broken comes back sane on the next start, keeping
   its positions and its honest history. The repair is journaled as
   `ledger_repair`.

To inspect a ledger by hand:

```bash
python scripts/check_ledger.py            # name the impossible mark
python scripts/check_ledger.py --fix      # repair in place, keep history
```

## Running it

```bash
cd solana-desk
python -m pip install -r requirements.txt
python -m pip install -r requirements-live.txt   # only if you want live
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

`run.sh` / `run.bat` do the same thing plus a venv and the key banner, so
prefer those unless you know why you want uvicorn directly.

**On a phone?** There's a full walkthrough with three routes, the Termux rust
gotcha, and a troubleshooting table in **[ANDROID.md](ANDROID.md)**.

## The brain: use a free AI

The analyst seat and the seat-to-seat dialogue both run through one provider
layer (`desk/brain.py`). It speaks plain HTTP to five providers, so you do not
need a paid key and you do not need a model SDK. Cheapest first:

| Provider | Cost | Key needed | How to start |
|---|---|---|---|
| **Ollama** | free, local | none | install [ollama](https://ollama.com), then `ollama pull llama3.2` |
| **Groq** | free tier | free key | [console.groq.com](https://console.groq.com) → API keys |
| **Google Gemini** | free tier | free key | [aistudio.google.com](https://aistudio.google.com/apikey) |
| **Cerebras** | free tier | free key | [cloud.cerebras.ai](https://cloud.cerebras.ai) → API keys |
| **Cloudflare Workers AI** | 10,000 Neurons/day | account id + token | [dash.cloudflare.com](https://dash.cloudflare.com) → Workers AI |
| **OpenRouter** | `:free` models | free key | [openrouter.ai/keys](https://openrouter.ai/keys) |
| **Claude** | paid | paid key | `ANTHROPIC_API_KEY` |
| **heuristic** | free, no model | none | always available, pure rules |

Pick one explicitly:

```bash
export DESK_BRAIN=groq          # or ollama | gemini | openrouter | anthropic | heuristic
export GROQ_API_KEY=gsk_...     # only the key for the provider you chose
```

Or set nothing and let it auto-detect, in this order: a local Ollama if it is
running, then the first cloud provider whose key is present (Groq, Gemini,
OpenRouter, Claude), then the heuristic. That means **the desk always works**:
a model outage, a rate limit or a bad response falls back to the deterministic
analyst instead of stalling the pipeline.

#### Running out of free quota? Add more keys, not more providers

Free tiers meter **per key**, so the cheapest way to get more calls is a second
account on a provider you already use. The advisor rotates keys automatically:

```bash
export GROQ_API_KEY=gsk_first...
export GROQ_API_KEY_2=gsk_second...     # up to _5
export GEMINI_API_KEY=...
export GEMINI_API_KEY_2=...             # same pattern for every provider
```

Each key gets its own daily budget and its own cooldown, so a `429` on one key
rotates to the next instead of resting the whole provider. Three Groq keys at the
default `BRAIN_BUDGET_GROQ=400` is **1,200 calls/day**, and the provider only
goes quiet when every key is exhausted. Duplicate keys are ignored, so a
copy-paste mistake cannot inflate the budget. `/api/ai/status` and `/api/health`
report `keys` and `used_today` per provider so you can see the rotation working.

#### Spend the free tier on sells, not on every buy

By default the AI now only advises **exits**. A buy candidate is already fully
gated by rules (scanner, chain, dev, meme, analyst, risk), so asking a model
about it spends quota on a decision the rules have already made. A sell is
different: money is at risk and a second opinion is worth a call.

```bash
export BRAIN_ADVISE_ON=exit    # default: only when a sell/exit rule fires
export BRAIN_ADVISE_ON=entry   # old behaviour: only on buy candidates
export BRAIN_ADVISE_ON=both    # every candidate and every exit (most expensive)
export BRAIN_ADVISE_ON=off     # never ask the AI, rules only
```

An exit review is deliberately cheap: **one provider, one call, no debate**. The
rule has already fired and the order is already sent, so the answer can never
delay or block the sell — it is logged as `AI on exit $SYM: sell_now 78% — ...`
and journaled as `brain_exit` so you can compare the AI against the rule over
time. Exits are rare, so this typically costs a handful of calls a day instead of
hundreds.

You can change this live from the dashboard: **AI advice on** under the AI group.

Two other knobs cut burn without touching quality:

```bash
export BRAIN_MIN_INTERVAL_S=240   # at most 15 reviews/hour (default 0 = unpaced)
export BRAIN_CACHE_TTL_S=1500     # reuse an opinion for 25 min unless price moves
export BRAIN_DEBATES_PER_CYCLE=0  # a debate is 3 extra calls; opinions still work
```

The advisor ships with **five** independent free buckets — Gemini, Groq,
Cerebras, Cloudflare and OpenRouter — tried in that order. They meter
separately, so when four of them are cooling the fifth still answers. Cerebras is
OpenAI-compatible and very fast; set `CEREBRAS_API_KEY` (free at
[cloud.cerebras.ai](https://cloud.cerebras.ai)) and it is picked up with no other
change. Its models fall back `llama-3.3-70b` → `llama3.1-8b` → `gpt-oss-120b`,
and `BRAIN_CEREBRAS_MODEL` overrides the first one.

#### The biggest free quota is Cloudflare, and it is the one to add

Cloudflare Workers AI gives **10,000 Neurons a day, every day**, and it bills in
Neurons (GPU compute) rather than requests. A short exit review — roughly 600
tokens in, 150 out — costs about **8 Neurons**, so the free allowance covers
well over a thousand calls a day. That is more than this desk can spend on exits
even if it tried, which makes Cloudflare the single best free bucket available.

It is the only provider here that needs **two** values, because the endpoint is
scoped to your account:

```bash
bash termux/set-ai-keys.sh - - - - <CLOUDFLARE_ACCOUNT_ID> <CLOUDFLARE_API_TOKEN>
```

The account id is on the Workers & Pages overview page; the token comes from
*My Profile → API Tokens → Create Token → "Workers AI"*. Tokens rotate as
`CLOUDFLARE_API_TOKEN_2`…`_5` and share one account id. A token without an
account id cannot build a URL, so the provider reports itself as unconfigured
instead of failing quietly — and `set-ai-keys.sh` refuses the half-configuration.

Models fall back `@cf/openai/gpt-oss-120b` →
`@cf/meta/llama-3.3-70b-instruct-fp8-fast` → `@cf/qwen/qwen3-30b-a3b-fp8` →
`@cf/meta/llama-3.1-8b-instruct-fp8-fast`; `BRAIN_CLOUDFLARE_MODEL` overrides the
first. `BRAIN_BUDGET_CLOUDFLARE` (default 300) caps calls per day, well inside the
Neuron allowance.

Two providers that look free but are **not** worth wiring up in 2026: **GitHub
Models** was retired on 30 July 2026 and its inference API no longer exists, and
**Mistral's** free mode no longer includes API access at all — keys require
billing activation. Both are still recommended by older blog posts.

The dashboard's **AI quota** panel draws one bar per provider with calls used
today, key count and any cooldown, plus a single line for total free calls left.
`keys_cooling` tells you when only *part* of a provider is spent — e.g. `1 of 2
keys cooling` means the second key is still serving, so nothing is lost yet.

Desk AI chat has its own per-minute window, separate from the trading loop, so a
long conversation can no longer starve the review path (or vice versa).

The dashboard shows exactly what is thinking, e.g. `Brain: Groq · llama-3.3-70b-versatile · free`,
or `Brain: heuristic (no model configured)` in amber when no provider is set.

### Which free option should you use?

- **Ollama** is the only one that is truly unlimited and private: nothing leaves
  your machine, no rate limits, no signup. It needs a machine that can run a 3B–8B
  model. Best default if you have the RAM.
- **Groq** is the fastest free option and the best choice on a small VPS or phone-hosted
  setup, because the inference runs on their hardware. The free tier has a daily
  token cap that a 45-second cycle will not hit.
- **Gemini** free tier is good at strict JSON, which is what the typed thesis needs.
- **OpenRouter** is the escape hatch: one key, many models, filter by `:free`.

### The honest caveat

Small free models are weaker than Claude at judgement. They are fine at reading
a structured brief and filling a typed shape, which is all this desk asks them to
do — they never size a trade and never place an order. But expect more `skip`
verdicts and blunter red flags from a 3B model than from a frontier one. If a
free model returns something unparseable, the desk silently uses the heuristic
for that token and keeps going; it does not retry in a loop and it does not
guess.

### Tests

```bash
python -m unittest discover -s tests -v
```

71 offline tests cover free cuts, chain-fact curve exclusion, the three-cap
sizing and veto rules, untrusted-input wrapping, every paper exit rule, the meme
value scorecard (weights, narrative tags, hold horizons, bounded totals), the
dev investigation (bag, rug history, clean creator), the seat dialogue ordering,
the DexScreener view, the access gate, device pairing, and the brain (provider
resolution order, free-beats-paid, local-first, JSON extraction from fenced and
chatty output, `None` on garbage, and the analyst falling back to rules when the
model is down).

### Tests

```bash
python -m unittest discover -s tests -v
```

71 offline tests cover free cuts, chain-fact curve exclusion, the three-cap sizing and veto rules, untrusted-input wrapping, every paper exit rule, the meme value scorecard (weights, narrative tags, hold horizons, bounded totals), the dev investigation (bag, rug history, clean creator), the seat dialogue ordering, the DexScreener view, the access gate (open vs locked, health stays public, wrong key rejected), device pairing (unpaired blocked, paired opens without a key, a second device still needs the key, wrong key cannot pair, revoke works, hashes never exposed), the brain (provider order, free beats paid, local first, JSON extraction, `None` on garbage, rule fallback), and Helius (no key means no breakage, endpoint precedence, DAS failure returns `None`, true holder count beats the top-20 proxy, the cache collapses repeat calls, a failure is never cached, the cache expires).

---

## REST API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/state` | Full desk snapshot: status, stats, broker, events, decisions, dialogue transcripts, config, access state |
| `GET`/`POST` | `/login` | Key form (only active when `DESK_TOKEN` is set) |
| `POST` | `/pair` | Pair this browser as a trusted device |
| `GET` | `/devices` | List and revoke paired devices |
| `GET` | `/api/devices` | Paired devices as JSON (no hashes) |
| `GET` | `/logout` | Clear the session cookie |
| `GET` | `/api/health` | Liveness probe |
| `POST` | `/api/agent/start` | Start the autonomous loop |
| `POST` | `/api/agent/stop` | Pause the loop |
| `POST` | `/api/agent/step?demo=1` | Run exactly one cycle (optionally forced demo) |
| `POST` | `/api/agent/reset` | Reset the active book, bench, logs |
| `GET` | `/api/mode` | Current mode, caps, wallet, arming state |
| `POST` | `/api/mode` | `{"mode":"paper"}` or `{"mode":"live","confirm":"<phrase>"}` |
| `POST` | `/api/kill` | Trip the kill switch (stops agent, blocks entries) |
| `DELETE` | `/api/kill` | Clear the kill switch |

---

## Project layout

```
solana-desk/
├── app.py                  FastAPI app + REST API + static serving
├── config.py               every threshold in one place
├── requirements.txt
├── requirements-live.txt   solders + base58, only for real-money signing
├── START-HERE.md           zero-assumptions launch guide
├── ANDROID.md              phone walkthrough (laptop, Termux, tunnel)
├── desk/
│   ├── scanner.py          Seat 1 — GeckoTerminal new pools, free cuts, quotes
│   ├── chain.py            Seat 2 — Solana RPC facts, real-wallet concentration
│   ├── devcheck.py         Seat 2b — RugCheck dev/creator investigation
│   ├── meme.py             Seat 2c — what gives a coin value, hold horizons
│   ├── dexscreener.py      second market view (price, volume, buys/sells)
│   ├── analyst.py          Seat 3 — typed thesis, any brain or heuristic fallback
│   ├── risk.py             Seat 4 — pure-code three-cap sizing and limits
│   ├── broker.py           Seat 5 — paper fills and rule-based exits
│   ├── live.py             Seat 5b — real-money broker, Jupiter + solders
│   ├── brain.py            one interface, many models, free ones first
│   ├── helius.py           optional node, DAS holder counts, priority fees
│   ├── dialogue.py         seat-to-seat transcript (template or model)
│   ├── devices.py          paired-device registry (hashed tokens)
│   ├── ops.py              journal, bench, alerts, kill switch
│   ├── links.py            FOMO URLs (always r=romsq, never shortened)
│   ├── chart.py            ticket OHLC (ticks + cached GeckoTerminal)
│   └── solana_agent.py     the autonomous Solana agent
├── web/
│   ├── index.html          dashboard markup (7 seats + meme/dev/dialogue panels)
│   ├── styles.css          dark trading-desk design system
│   ├── app.js              live 2s polling, controls, pipeline viz
│   ├── assets/cat-pixel/    pixel mascot (idle/scan/win/loss/sleep/trader)
│   └── assets/              icons
└── tests/
    └── test_desk.py        95 offline tests
```

---

## Access control

Two ways in:

1. **A paired device** — pair once with the key; that browser then opens the desk with no key, forever (one year). This is how "only my phone gets in" works.
2. **The key** — any other device can still get in by typing `DESK_TOKEN`.

With no `DESK_TOKEN` the desk is open to anyone who can reach the port.

### Start it

```bash
./run.sh
```

```
==============================================================
  Solana Desk is LOCKED

  Open:  http://localhost:8000
  Key:   <your-desk-key>
  Paired devices: 0

  On your phone: open the URL, then 'Pair this device'.
  After that the phone opens with no key. Every other device
  still needs the key above.

  Manage devices: http://localhost:8000/devices
==============================================================
```

Then on your Android: open the URL → **Pair this device** → enter the key once. The phone now opens straight to the dashboard. Every other device still sees the login screen.

```bash
./run.sh mypassword   # your own key instead of a random one
./run.sh --open       # no lock at all
```

### What each device sees

| Request | Unpaired, no key | Paired device | Key session |
|---------|------------------|---------------|-------------|
| `/` dashboard | 303 → `/login` | 200 | 200 |
| `/api/state` | 401 | 200 | 200 |
| `POST /api/agent/reset` | 401 | 200 | 200 |
| `POST /api/kill` | 401 | 200 | 200 |
| `/api/health` | 200 | 200 | 200 |
| `/static/*` | 200 | 200 | 200 |

### How a device is remembered

Pairing mints a 32-byte random token and sets it as an `HttpOnly`, `SameSite=Lax` cookie valid for one year. **Only the SHA-256 of that token is written to `desk_devices.json`** — a stolen file cannot be replayed, and the token is never readable from JavaScript. Comparison is constant-time.

The master key session is a separate 7-day `HttpOnly` cookie holding a SHA-256 of `DESK_TOKEN`.

### Managing devices

`http://localhost:8000/devices` lists every paired device with its name, pairing date, and last-seen time, each with a **Revoke** button. Revoking deletes the hash immediately — that browser is locked out on its next request. The top bar shows `this device · Pixel 7` when you are on a paired device, `access: key` when you unlocked with the key, and `access: open` when there is no lock.

This is a single shared key plus per-device tokens, not per-user accounts. It stops strangers and casual snooping; it is not a substitute for TLS in front of a public deployment.

---

## Safety

- **Paper by default, every boot.** Live mode is never remembered across a restart.
- **Key from the environment only.** `SOLANA_PRIVATE_KEY` is never written to a
  file, never logged, and redacted out of every error string.
- **Two locks on every live order.** `check_live_trade()` sizes it; the broker
  re-checks the caps, the kill switch and the daily loss stop before it signs.
- **Kill switch.** A `KILL` file (and the UI button) stops the agent and blocks
  new live buys immediately. Exits still run, so you can flatten.
- **No demo money in live.** Synthetic `DEMO…` mints are refused by the broker
  and never produced by the scanner while the mode is live.
- **Local signing.** The transaction is signed on your machine. No key ever
  leaves it.

This is a research tool, not financial advice. Live mode can lose real money.
