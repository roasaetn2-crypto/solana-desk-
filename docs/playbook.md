# Desk playbook

Rules the paper desk follows before, during and after a trade, and the free data
sources behind them. Everything here is **PAPER only** and **tighten-only**: it can
add a flag, cut a ticket, pause new entries or write a note. It cannot loosen a gate,
raise a limit, lift a pause, touch LIVE, or place an order. Live trading stays
disabled in code and no private key is ever read.

## 1. Pre-trade checklist (`desk/guard.py: pretrade_checklist`)

Every ticket passes the list below before a paper fill. A failed line refuses the entry.

| Line | Meaning |
|---|---|
| plan: stop and horizon set | the exit profile (scalp or swing) gave a stop |
| size within the per-trade cap | after all cuts the ticket is at most 25% of the sizing base |
| breaker not tripped | the drawdown breaker is not in cooldown |
| no security block flags | no GoPlus / GMGN block flag on the mint |
| paper mode | never true in live mode |
| price confirmed on a second read | the confirm quote matched (added after the confirm step) |

The result shows on the ticket as a small `✓ 6/6` chip. The risk seat's prompt and the
analyst's prompt carry the same discipline in words.

## 2. Drawdown circuit breaker (`guard.circuit_breaker`)

Sits on top of the existing 3% daily-loss halt in `desk/risk.py`, which is unchanged.

| Trigger | Effect |
|---|---|
| day P&L at or below -2% of start-of-day equity | ticket halved |
| 2 losing exits in a row | ticket halved |
| 3 losing exits in a row | no new entries for 30 minutes after the last loss, then half size while the streak stands |

Open positions keep being managed during a pause. A win ends a streak.

## 3. Market regime tag (`desk/regime.py`)

BTC and SOL 24h change from CoinGecko's free simple-price endpoint, refreshed every
10 minutes. `RISK_OFF` (SOL -5% or worse, or BTC -3% or worse) halves paper tickets.
`RISK_ON`, `NEUTRAL` and `UNKNOWN` change nothing. It is shown on the Guard line and
recorded on each exit review.

## 4. Post-trade review (`guard.review_exit`, `guard.coach`)

Each exit gets a one-line note: win or loss, P&L, exit reason, hold time, multiple, peak
gain, regime, and one lesson (for example "gave back over half the peak gain"). It is
written to the journal (`review` rows), the exit event, the activity feed and the Guard
panel. Jupiter sell-side price impact is appended when a quote is available. After 5 exits
the coach adds advisory rules ("most exits are stops", "flagged entries mostly lose").
The coach never changes a setting.

## 5. Free data sources

| Source | File | Used for | Limit we keep | Status |
|---|---|---|---|---|
| Healthchecks.io | `desk/heartbeat.py` | outage email if pings stop | 1 ping / min | code ready, waiting on signup |
| GoPlus Solana | `desk/goplus.py` | Token-2022 traps: transfer fee, transfer hook, default frozen, non-transferable, closable, balance-mutable authority, malicious creator | 24 / min (limit 30), 1 h cache | on |
| GMGN (read only) | `desk/gmgn.py` | smart money, KOL, sniper, bundler, insider, fresh-wallet rates, wash and honeypot flags | 20 / min, 10 min cache | waiting on key |
| Jupiter quote | `desk/jupiter.py` | price impact on entry and exit, quotes only | 1 call / 2 s (0.5 rps) | on |
| PumpPortal | `desk/pumpportal.py` | launch facts: dev initial buy, serial launcher; one websocket, `subscribeNewToken` only. Starts with the web process, reconnects forever (5 s to 5 min backoff), and reconnects if silent for 2 min | 1 connection | on |
| DexScreener | `desk/promo.py` | paid-promotion flag from token-profiles and token-boosts | about 1 / min (limit 60) | on |

Rules every source follows: hard timeout (8 s), cache, a rate limiter that skips instead of
sleeping, and "unknown" (no flags, no cut) on any failure. Secrets come from environment
variables only and never appear in the UI, state API, logs or URLs.

### Flag effects (PAPER)

| Flag | Source | Effect |
|---|---|---|
| non_transferable, default_frozen, transfer_hook, balance_mutable, closable, malicious_creator | GoPlus | **block** |
| transfer_fee above 5% | GoPlus | **block** |
| transfer_fee 0 to 5% | GoPlus | ticket halved |
| gmgn_rug_ratio at or above 0.5, gmgn_honeypot | GMGN | **block** |
| rug ratio 0.3 to 0.5, wash trading, bundlers, insiders, fresh wallets, snipers, bot wallets | GMGN | ticket halved |
| dev_buy_big (dev bought 10% or more of supply at launch), serial_launcher (3+ launches in 6 h) | PumpPortal | ticket halved |
| jup_impact_high (5% or more) | Jupiter | ticket halved, flag on exit |
| jup_no_route | Jupiter | chip only |
| paid_promo | DexScreener | chip only. Not a quality score, never scored up or down |
| regime_risk_off, breaker_reduce | regime, breaker | ticket halved |

Cuts multiply, with a floor of a quarter of the ticket. A ticket that falls under the $2
fee floor after cuts is refused.


## 5b. Backtest (read only)

The dashboard Backtest card and `GET /api/backtest` (same login as the rest of
the desk) replay recorded journal tickets against other gate settings: a 0–100
score of 50/55/60/65/70, max chase, min alive 6h, and the price-gap cap. Sizing
and exits stay the desk's own paper rules ($20 book, $5 scalp with a 20% stop,
$3.33 swing with a 30% stop, max 4 positions, $2 fee floor).

A coin that would have been taken but has no later recorded price is marked
unknown. Prices are never invented. The run does not change live settings; any
retune is a separate, logged step. Early samples are thin — often only hours of
tickets, and a deploy wipes `journal.jsonl` unless `DESK_DATA_DIR` points at a
disk.

## 6. Environment variables

| Name | Needed for | Notes |
|---|---|---|
| `HEALTHCHECKS_URL` | heartbeat | the full ping URL from your Healthchecks.io check. Secret. https only |
| `HEALTHCHECKS_PING_WHEN_PAUSED=1` | optional | keep pinging while the loop is paused (default: a paused loop is silent, so you hear about it) |
| `GMGN_API_KEY` | GMGN | free key from gmgn.ai/ai. GMGN may restrict by IP. Never needs a private key |
| `DESK_OFFLINE=1` | tests | turns every optional lookup into "unknown" |
| `DESK_DATA_DIR` | optional | folder for `journal.jsonl`, the paper ledger and `desk.db`. Use a mounted disk so a deploy does not wipe history |
| `OLD_DESK_DB` | optional | path to the previous desk's sqlite file, included in a local backtest only |

## 7. Sources and licences

Ideas were read and rewritten for a paper memecoin desk. No large blocks were copied; no
files from these repositories are included. Both repositories are MIT licensed
(copyright Galleon Labs, and TraderMonty respectively).

- galleonlabs/hypergrok-trading-desk: `desk-risk-limits` (tighten-only limits, daily loss
  stop), `desk-trade-lifecycle` (ticket, checklist, review stage), `desk-post-trade-review`,
  `desk-monitoring` (heartbeat), `desk-incident-response` (stop adding, look, contain).
  `desk-execution-protocol` was read but nothing from it was ported: the desk never
  sends orders. A read-only journal replay lives in `desk/backtest.py` and on the
  Backtest card; it never auto-applies a setting.
  `desk-operating-model` informed the role split between seats. Skipped all `hyperliquid-*`.
- tradermonty/claude-trading-skills: `pre-trade-discipline-gate` (checklist),
  `drawdown-circuit-breaker` (loss-streak cooldown, daily loss), `position-sizer`
  (fixed-fractional sizing; already in `risk.py`), `signal-postmortem` and
  `trade-performance-coach` (exit review, advisory coaching), `crypto-regime-analyzer`
  (regime posture; a much smaller keyless version).
- GMGNAI/gmgn-skills (MIT): read-only endpoint list for `/v1/token/security` and
  `/v1/token/info` and the field names. No swap, order or signed-request code was used.
- GoPlus Solana token security docs; Jupiter `/swap/v1/quote`; PumpPortal
  `subscribeNewToken`; DexScreener `token-profiles/latest` and `token-boosts/latest`.
