# Fix: risk budget locks the desk out of every entry

## Diagnosis
- Budget = 3% x $50 = $1.50; per-trade risk = 1.5% x $50 = $0.76
- split_across_slots was False, so 2 positions exceeded the budget and every later entry was refused.

## Done
- [x] config.py: split on, paper floor $0.20, added risk_budget_usd override
- [x] desk/risk.py: budget = max(daily stop, full-book risk) + explicit override + clearer message
- [x] desk/settings.py: Risk -> Open-risk budget + Split budget across slots
- [x] tests updated + 3 regression tests (19 risk tests pass, 734 total pass)
- [x] RISK-BUDGET-FIX.md v2 section
- [x] Commit + push to roasaetn2-crypto/solana-desk-

# Feature: tabbed dashboard (stop endless scrolling)

## Done
- [x] web/index.html: sticky Live / Control / Research tab bar + data-col tags
- [x] web/styles.css: pill tabs, sticky bar, mobile rule shows only active column
- [x] web/app.js: initDeskTabs() with localStorage persistence, arrow keys, count badges
- [x] fix Research badge selector (decision rows now carry data-mint)
- [x] verify: mobile shows exactly one column per tab, reload keeps tab, 737 tests pass
- [x] Commit 41f0e48 + push to roasaetn2-crypto/solana-desk- main

# Feature: Ollama in the AI second opinion

## Why
brain.py (analyst seat) already supported LOCAL Ollama, but brain_advisor.py
(the second opinion) only spoke to Ollama Cloud. The supplied key authenticates
(401 for a bogus key, 402 for this one) but the account has no credits, so cloud
Ollama can never answer. Local Ollama is free and needs no key at all.

## Done
- [x] desk/brain_advisor.py: local mode via OLLAMA_BASE_URL / OLLAMA_HOST / cached
      localhost:11434 probe; no Authorization header; no per-minute pacing
- [x] ordering: free local Ollama first, paid cloud Ollama still last
- [x] DESK_NO_LOCAL_OLLAMA=1 kill switch
- [x] redact() now covers every provider key (was gemini/groq/openrouter only)
- [x] status/health/snapshot report mode: local|cloud
- [x] web/app.js: BOT_NAME gains ollama, cerebras, cloudflare, local
- [x] termux/set-ai-keys.sh: `local` arg writes OLLAMA_BASE_URL, not a key
- [x] tests: 9 new local-Ollama tests + 4 probe tests (750 total pass)
- [x] tests/conftest.py: suite hermetic even with a real `ollama serve` running
- [x] scripts/verify_ollama_local.py: 18-check end-to-end proof
- [x] README: "Ollama local is the free one, and it goes first"

# Task: Apply the Medium memecoin-bot guide to raise win rate (honestly)

Guide: medium.com/@gwrx2005/building-a-high-profit-cryptocurrency-trading-bot-with-meme-coin-support

## Audit
- [x] Scrape the guide, extract concrete rules
- [x] Audit desk: signal_engine, signals, meme, exits, risk, config, indicators
- [x] Existing backtests already tested the guide's technical toolkit
      (RSI/MACD/ATR-band/Bollinger/EMA/Stoch/ADX) on 3,013 + 5,974 REAL trades
      -> every one DO-NOT-ADD. Do not re-add them.

## Test the guide's memecoin-specific tactics the harness has NOT tested
- [x] Fetch fresh real OHLCV sample (indicator_eval_fresh.json, 60 pools)
- [x] Experiment: (a) ATR-scaled dynamic stop, (b) pullback-from-high entry,
      (c) flash-pump circuit breaker, (d) volatility ceiling filter
- [x] Run on real data (3,629 trades), measure win rate / PF vs baseline
- [x] Result: none clears the bar. Pullback is the only one that lifts win
      rate (+11.5pp, n=424) but PF 0.745 < 1.2. All four ship OFF.
- [x] Test pullback on the GATED set -> NOT POSSIBLE honestly: 101 pools give
      only 5-14 gated trades, below the 30-trade minimum. Documented as a limit.
- [x] Deep sweep (72 combos): pullback 20% + stop 30% + TP 2x/3x is the only
      ADD -> n=145, win 30.3% (+15.0pp), PF 1.587. Holds on both halves
      (PF 1.85 / 1.27), so not a lucky slice.

## Implement what wins
- [x] Wire winning tactic(s) into config + signal_engine/exits
      -> desk/meme_tactics.py, exits.profile override, signal_engine.apply,
         agent high_20 fetch. All flags default OFF (costs 96% of trade freq).
- [x] Add tests, keep suite green (766 passed, 17 skipped)
- [ ] Commit + push

## Prove it
- [x] Before/after table with real trade counts (MEME_TACTICS.md)
- [ ] Report to user
