# Local changes (not pushed anywhere)

Owner rule: this desk never holds a wallet key and never signs or sends a transaction. Paper only.

## What changed
- `desk/live.py`: new `LIVE_DISABLED = True`. `ready()` always returns false, `_key_material()` never reads `SOLANA_PRIVATE_KEY`, and `_keypair()`, `_sign_and_send()`, `LiveBroker.buy()` and `LiveBroker.sell()` raise `LiveError`.
- `desk/solana_agent.py`: `set_mode("live", ...)` is rejected with a clear message even with the right confirm phrase, and `broker` always returns the paper broker. `emit()` also scrubs `SOLANA_RPC_URL` / `HELIUS_API_KEY` values from UI log messages, so an RPC key in an error string cannot show up in the dashboard.
- `tests/test_live_disabled.py`: new tests for the above.
- `tests/test_desk.py`: the old `LiveExecution` class (tests that live can arm) is skipped.
- Not installed: `requirements-live.txt`, `solders`, `base58`. `SOLANA_PRIVATE_KEY` is never set.

## Known pre-existing test failures (not caused by this change)
4 `RiskEngine` tests in `tests/test_desk.py` fail on a clean checkout because `config.py` was loosened after the tests were written (ticket 28.57 vs 20, quiet hours off, score bar 5).
Testing the access-gate tests needs `httpx2` in the venv (test-only, not in requirements.txt).

## Outbound calls found in the code (review)
- Data: api.geckoterminal.com, api.dexscreener.com, api.rugcheck.xyz, Solana RPC (mainnet-beta public, or `SOLANA_RPC_URL`, or Helius via `HELIUS_API_KEY`).
- Optional LLM brain (only if a key or local Ollama exists): Groq, Gemini (key in URL query), OpenRouter, Anthropic. Default is the offline heuristic.
- Optional Telegram alerts (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`): the only "posting" path, sends desk alerts to the owner's own chat.
- Jupiter (`lite-api.jup.ag`) is used only in `live.py`, now disabled.
- The web UI loads Google Fonts. No other third-party scripts.
- No subprocess, eval/exec, or posting to social media.
- `QUICKSTART.md` step 4 downloads a zip from `sites.super.myninja.ai`. Never use it. The repo code is the source.
- `/devices`, `/api/mode`, `/api/agent/*`, `/api/kill` are behind the DESK_TOKEN gate. If `DESK_TOKEN` is unset the desk is fully open (start.sh refuses to start without it; run.sh --open does not).
- `render.yaml` has `autoDeploy: true`: any push to the repo redeploys Render.

# Branch `merge-best` (on top of `disable-live`): old-desk ideas ported, PAPER only

| Piece | From | Where |
|---|---|---|
| Scalp vs swing horizon picked at entry (pool age < 6h = scalp) | old `agents/timeframe.py` | `desk/exits.py`, `config.EXITS["profiles"]` |
| Hard stop that only moves up (breakeven after TP1), armed trail, timeout only if under +5% | old `agents/risk.py` | `desk/exits.py` |
| Laddered take-profit: 33% at 1.5x, 50% of the rest at 2x, remainder trails | new desk's half-at-1.5x, extended | `desk/exits.py` |
| Needs 3 quote misses in a row before a "no quote" close (was 1) | fix | `config.EXITS["missing_quote_closes"]` |
| Second-source price + liquidity confirm before the fill; fills at the fresh price | old `agents/research.py` (price gap 25% paper, liq ratio 0.5) | `desk/solana_agent.py`, `config.ENTRY` |
| h1 gain cap 250% ("chase") | old scanner `max_h1_change_pct` | `desk/scanner.py` |
| Compounding: size from realized equity (start + closed P&L), same caps, 2% of pool, daily loss stop | new | `desk/risk.py`, `PaperBroker.realized_equity()` |
| Self-tune of 4 PAPER entry gates inside hard floors, every change logged | old autotune idea | `desk/autotune.py`, `config.AUTOTUNE` |
| Demo pools no longer feed the feed or the P&L | fix | `config.AGENT["demo_when_empty"]=False` + demo never fills |
| Exit-rule replay on the old desk's recorded ticks | new | `scripts/replay_exits.py` |

Not changed: stop/size/daily-loss caps (`RISK`), scam kills (mint/freeze authority, top wallet, top 10, dev checks, meme community), LIVE (still disabled).
The old `desk/` repo is untouched. Old tests that pinned stale numbers (RiskEngine, take-half) were synced to the current config; 
new tests: `tests/test_merge_best.py` (exits, compounding, entry confirm end-to-end with mocked network, autotune floors).
Render free disk is ephemeral: `paper_ledger.json`, `autotune.json`, `desk.db` reset on every restart/redeploy/sleep.

# Scanner fix (2026-11-11): GeckoTerminal 429s were silent, so Render saw 0 pools

Root cause: `_get` in `desk/scanner.py` returned `None` after a 429 and `new_tokens()`
just stopped, with no status code in the log. The agent then emitted
"0 pools from geckoterminal" and incremented no error counter. GeckoTerminal
allows ~10 calls/min; a cycle of 6 calls plus Render's Singapore IP being
rate-limited (or blocked with 429) matches every cycle since the merge
deploy returning nothing. Confirmed from this box: the same endpoints and
headers return HTTP 200 with 20 pools/page, so the API shape itself is fine.

What changed
- `desk/scanner.py`: log every non-200 (status + reason, no secrets); honor
  Retry-After; cap backoff at 15s so a 429 cannot stall a cycle for minutes;
  60s cooldown on GeckoTerminal after a 429; User-Agent/Accept tightened.
- DexScreener is a merged fallback (token-profiles/latest, token-boosts
  latest+top, search `SOL` and `pump`), normalized to the same pool shape
  via `parse_dex_pair`, deduped by mint (GeckoTerminal wins on a clash).
  Existing `free_kill` filters, risk caps, PAPER-only and LIVE_DISABLED
  are untouched.
- `desk/solana_agent.py`: scan line now reports `gt=` / `ds=` counts and
  emits each fetch warning into the event log.
- `tests/test_scanner_sources.py`: 429/403 logging, merge+dedup, labels.
- Cycle interval stays 30s. The 3–4 minute cycles on Render were the 20s+40s
  429 sleeps; those are now capped, so the loop is effectively shorter.

# Scanner rate limits (scan-rate, 2026-11-12): don't sleep on 429, rotate, cache

The first scanner fix stopped swallowing 429s, but still retried and slept
inside the cycle (up to 15s) and called every DexScreener endpoint every
cycle. On Render both hosts 429'd; cycles dragged and GeckoTerminal stayed
at 0 while the log filled with repeat "search q=SOL -> 429" lines.

What changed
- Per-host token bucket (`config.SCAN_FETCH`): GeckoTerminal <= 10/min, burst 2;
  DexScreener 30/min, burst 4. A cycle never waits for a token — it skips.
- 1-2 GeckoTerminal calls per cycle: `new_pools` page 1 every cycle, page 2
  every other cycle. DexScreener discovery rotates one endpoint per cycle
  (search pump, search SOL, profiles, boosts latest, boosts top) — never
  both search queries in the same cycle.
- Endpoint responses cached 90s; rolling candidate pool keyed by mint for
  10 minutes, so a 429 on one endpoint does not shrink the set. Age is
  advanced from the original fetch time.
- On 429: no in-cycle retry or sleep. The endpoint cools (Retry-After vs
  exponential, floor 15s, cap 120s). A 429 is logged once per cooldown.
- Open positions refresh via one batched `GET /tokens/v1/solana/{mints}`
  (max 30) in `quote_many`, used by `_manage_positions`. Short quote / pair
  cache also feeds `quote()`, `socials()`, and `dexscreener.best_pair`.
- Filters, risk caps, PAPER-only and LIVE_DISABLED are untouched. Cycle
  interval stays 30s.
- Tests: `tests/test_scanner_sources.py` covers limiter, cooldown, no-sleep
  429, one search per cycle, and batched quotes.

# The -80.53% day (2026-11-14 01:12): a stop that never stopped

Screenshot: equity **$13.01**, day P&L **-$53.80 (-80.53%)** on a book that had
been $66.81 -- while the dashboard still advertised "Daily loss stop 3%". Three
separate holes, all now closed:

1. **A stop filled at the rug print, not at the stop.** `exits.evaluate` marked
   `pos["last"]` from the quote and returned a stop signal; `_manage_positions`
   then sold at `pos["last"]`. On a memecoin a single tick can gap far past the
   stop (a rug), so a 1.5%-risk trade realised a 20%-of-book loss.
   Fix: `EXITS["stop_fill_at_stop"] = True` -- a stop exit carries `pos["fill_at"]
   = pos["stop_price"]`, and the sell uses that level. `False` restores raw
   market fills.

2. **The daily stop blocked new buys but never closed the book.** `daily_loss_hit`
   was only consulted in `check_token_trade`. Open positions kept bleeding all
   day. Fix: `SolanaAgent._enforce_daily_stop()` flattens every open position the
   moment the day's loss hits the stop (paper only, once per day, never when the
   operator tapped Continue). It runs before and after `_manage_positions`, and
   again after **every** realised exit, so a batch of rugs in one cycle cannot
   realise the whole book before the next cycle notices.

3. **Risk was sized per ticket, never for the book.** Six 1.5%-risk trades is 9%
   at risk on a 3% day. Fix: `risk.open_risk_usd()` sums what the open book loses
   if every stop fills at its stop, and the entry gate refuses a ticket that would
   push total open risk past the daily stop. A single ticket is still the sizing
   layer's job; the gate only governs *stacking*.

Replaying the screenshot (six risk-sized positions, all rug to 1/10th) now ends
the day at **-4.65%** instead of **-80.53%**, and the rest of the book is closed
at the stop rather than held through the rug.

Termux: `bash termux/fix-balance.sh` repairs a corrupt ledger; the new
`bash termux/reset-balance.sh [amount]` archives the current ledger and re-bases
the book to a clean bankroll (default $50) without deleting the old file.

# Bankroll set to $50 (owner, 2026-11-14)

`PAPER["starting_equity_usd"] = 50` and `PAPER["min_ticket_usd"] = 0.50`.

The floor had to come down with the bankroll. On $50 a two-caution-cut ticket is
$0.62 (swing) to $0.94 (scalp); the old $2 floor would reject every setup in
risk-off + a security flag, which is the sizing deadlock this repo already fixed
once. At $0.50 the desk keeps trading through two cuts and only skips at three
(0.125x), which is the desk being genuinely cautious.

Three tests hardcoded the old $2 floor / $50-liquidity cases and were updated to
be floor-relative instead of magic-number-relative. `tests/legacy_book.py` still
pins the $20 book for the older sizing tests, by design.

A fresh install now boots at equity $50.00, cash $50.00, day P&L $0.00.

# "pumpportal: websocket-client missing" (2026-11-14)

The import is `import websocket`, but the PyPI package is `websocket-client`.
`pip install websocket` installs a DIFFERENT project that shadows the right
module, so the desk keeps failing even after a "successful" install.

- `desk/pumpportal.py`: the retry log now prints the exact fix, and
  `health()["hint"]` carries it too, so the dashboard can show it.
- `termux/fix-deps.sh`: uninstalls the wrong `websocket` package, installs
  `requirements.txt`, then verifies `create_connection` /
  `WebSocketTimeoutException` / `WebSocketApp` exist and that every desk import
  resolves.
- `termux/start-desk.sh`: one-line preflight warning at boot instead of a
  mystery in the log.

The feed is optional: without it the desk falls back to the REST scanner.
Verified after install: `pumpportal: connected, subscribed subscribeNewToken`,
health `status: on`, `error: null`.

## Cold start: a fresh DESK_DATA_DIR must not crash the ledger

`LiveBroker.__init__` writes an empty `live_ledger.json` at construction, and
`PaperBroker._save` used `write_text()` without creating the parent directory.
On a brand-new `DESK_DATA_DIR` (first install, or a reset that recreates the
dir) `import app` died with `FileNotFoundError: live_ledger.json`, which is
exactly what the one-command update script hit on a cold dir.

- `desk/broker.py:_save` now `mkdir(parents=True, exist_ok=True)` first.
- `scripts/check_ledger.py --fix` now persists the fresh book it creates
  (`PaperBroker` only builds state in memory until something needs repair).
- `termux/update-all.sh` restarts exactly once (reset-balance runs with
  `NO_RESTART=1`), reads the port the server actually bound from `desk.log`,
  and logs in with `$DESK_TOKEN`/`~/.desk_key` before reading `/api/state`
  so the balance line is real numbers instead of `unauthorized`.
- `tests/test_cold_start.py` pins all three.
