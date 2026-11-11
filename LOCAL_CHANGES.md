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
