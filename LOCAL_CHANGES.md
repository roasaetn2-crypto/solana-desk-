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
