"""Every number the desk uses lives here. Retune the desk by editing this file only.

These are STARTING values, not advice. Run in paper mode, read the journal,
and move a number only when the journal tells you to.
"""

# ---------- Seat 1: scanner (free cuts, data already in the GeckoTerminal batch) ----------
SCAN = {
    "pages": 1,                    # rotated GeckoTerminal feed pages per cycle (1 page = 20 pools), on top of new_pools p1
    "pump_only": False,            # True = only pump.fun curve + PumpSwap pools
    "pump_dexes": {"pump-fun", "pumpswap"},
    "min_age_minutes": 1,          # younger: the data is noise (retune 2026-11-12: was 2)
    "max_age_hours": 48,           # older with no new move: not a launch any more (retune: was 24)
    "min_liquidity_usd": 3_000,    # retune: was 5_000
    "min_volume_h1_usd": 3_000,    # retune: was 8_000
    "min_mcap_usd": 8_000,         # retune: was 15_000
    "max_mcap_usd": 25_000_000,
    "min_trades_h1": 30,           # retune: was 60
    "max_h1_change_pct": 250.0,    # MERGE (old desk scanner.max_h1_change_pct, paper 250): do not buy a vertical chart
    "min_sells_ratio_h1": 0.10,    # sells / buys. Below this nobody can sell = honeypot smell
}

# ---------- Scanner fetch (rate limits, cache, cooldown). Does not change filters. ----------
SCAN_FETCH = {
    "gt_per_min": 10,              # well under GeckoTerminal's ~30/min public cap
    "gt_burst": 2,                 # at most 1-2 live GT calls in a short burst
    "ds_per_min": 30,              # under profiles/boosts 60/min and search 300/min
    "ds_burst": 4,
    "cache_ttl_s": 90,             # reuse an endpoint body this long
    "gt_max_page": 3,              # GT rotation walks pages 1..3 of new_pools / trending 5m / trending 1h
    "pool_ttl_s": 600,             # rolling candidate pool, keyed by mint (~10 min)
    "quote_ttl_s": 15,             # short cache for open-position price refreshes
    "cooldown_floor_s": 30,        # even if Retry-After is 1s, back off at least this (per host)
    "cooldown_cap_s": 120,         # exponential 429 cooldown cap; never sleep the cycle
    "cooldown_jitter": 0.15,       # up to +15% on every cooldown so probes never sync up
    # Jupiter tokens v2 (free, keyless): 1 cached discovery list + 1 PumpPortal hydrate per cycle
    "jup_enabled": True,
    "jup_per_min": 6,
    "jup_burst": 3,
    "jup_batch": 100,              # mints per search call (Jupiter's max)
    "jup_rehydrate_s": 60,         # re-price a PumpPortal mint at most this often
    "pp_max_age_s": 6 * 3600,      # hydrate PumpPortal launches up to this old
    "pp_hot_trades": 15,           # a launch with this many 1h trades (or the volume below) is "hot"
    "pp_hot_volume_usd": 1_500,
    "pp_cold_recheck_s": 300,      # re-check a quiet launch this often (late movers)
}

# ---------- Seat 2: chain facts (Solana RPC) ----------
CHAIN = {
    "max_top_wallet": 0.08,        # biggest real wallet, excl. curve / pool / program accounts
    "max_top_10": 0.35,            # top 10 real wallets combined
    "allow_mint_authority": False, # supply can be printed -> hard no
    "allow_freeze_authority": False,
    "largest_accounts_checked": 20,
}

# ---------- Seat 3: analyst ----------
ANALYST_MODEL = "claude-sonnet-4-20250514"
REVIEW_MODEL = "claude-sonnet-4-20250514"
ANALYST_MAX_TOKENS = 4_000
MIN_SCORE = 6                        # 0-10, the analyst's score must clear this (owner set 6, 2026-11-12)

# The bar for the analyst to say "valid_setup" (the only verdict the risk engine
# will size a ticket for). Lower = more trades, more noise. Raise it back to 7
# for the original, stricter desk.
ANALYST_VALID_SCORE = 6              # 0-10 (owner set 6 on 2026-11-12; was 5)
ANALYST_MAX_RED_FOR_VALID = 3        # red flags allowed on a valid setup

# ---------- Seat 4: risk engine (pure code) ----------
RISK = {
    "max_risk_per_trade": 0.015,   # 1.5% of the paper book (retune 2026-11-12: was 5%)
    "max_position_pct": 0.25,      # $5 cap on a $20 book (was 5% of $1,000 = $50)
    "max_pool_share": 0.02,        # never be more than 2% of the pool: you'd be the exit
    "max_open_positions": 6,       # retune: was 4
    "max_daily_loss_pct": 0.03,    # down 3% on the day -> desk stops until tomorrow
    "enforce_risk_budget": True,   # Whole-book stacking gate. True = refuse/trim an entry that
                                   # would push total open risk past the budget. False = let every
                                   # entry through (the daily loss stop still fires and flattens).
                                   # Turn off only if you accept that N concurrent positions can put
                                   # max_risk_per_trade x N at risk on a max_daily_loss_pct day.
    "risk_budget_usd": 0.0,        # EXPLICIT whole-book open-risk budget in USD. 0 = auto.
                                   # Auto = max(daily stop, max_risk_per_trade * book * slots),
                                   # i.e. the larger of "the 3% day" and "what all six slots are
                                   # allowed to risk". Without that floor the config contradicts
                                   # itself (1.5% x 6 = 9% at risk vs a 3% budget) and the desk
                                   # refuses every entry after two positions. Set this to a
                                   # dollar figure to override the auto budget from the dashboard.
    "max_daily_loss_usd": 0.0,     # hard dollar daily stop. 0 = off (use the % above). When set it
                                   # REPLACES the % limit, so you can name the day's loss in cash.
                                   # Useful on a small book where 3% is only a dollar or two.
    "flatten_on_daily_stop": True,  # PAPER only. When the day's loss hits the stop, CLOSE every
                                   # open position instead of merely blocking new entries. Without
                                   # this a "3% daily stop" desk could bleed to -80% on open rugs.
    "auto_continue_after_daily_stop": False,  # PAPER only. When True the daily stop still fires and is
                                   # still journaled, but it does not block new entries (same effect as
                                   # tapping "Continue today" every day). LIVE ignores this entirely.
    "min_ticket_usd": 10,          # live fee floor (unchanged). Paper floor is PAPER["min_ticket_usd"]
    "risk_budget_split_across_slots": True,   # Divide the whole-book risk budget across
                                   # max_open_positions, so the per-trade risk cap agrees with
                                   # the daily stop by construction. Without this the config
                                   # contradicts itself (1.5% x 6 slots = 9% at risk on a 3%
                                   # day), only two positions can ever open, and the third is
                                   # refused forever. Set False to size every ticket at
                                   # max_risk_per_trade and let the budget limit concurrency.
    "compound": True,              # MERGE: size off REALIZED equity (start + closed P&L), so wins are reinvested
                                   # and losses shrink size. Still capped by every cap below + 2% of pool.
    "quiet_hours": (0, 0),         # local hours with no new entries. (0,0) = trade 24/7.
                                   # Was (1, 7); disabled so the paper desk can trade now.
}

# ---------- Seat 5: exits (paper broker applies these, no model involved) ----------
EXITS = {
    # legacy single-profile keys (kept: dashboard + volume-decay rule read them)
    "stop_pct": 0.30,              # fallback stop for positions opened before profiles existed (retune: was 0.35)
    "take_half_at": 1.5,
    "volume_decay_ratio": 0.20,    # h6 volume / (h24/4) under this -> close
    "volume_decay_min_minutes": 30,
    "max_hold_hours": 4,           # legacy fallback (retune: was 24)
    # MERGE (from old desk agents/timeframe.py + agents/risk.py): the horizon is picked at entry.
    # A pool younger than scalp_max_age_hours is a scalp (tight stop, short clock); older = swing.
    # Ladder: sell tp1_frac at tp1_at (stop then moves to breakeven, never down), sell tp2_frac of
    # what is left at tp2_at, and the remainder trails trail_pct below its peak.
    "style": "both",               # PAPER trading style: both | scalp (young coins only) | swing (older only)
    "scalp_max_age_hours": 6.0,
    # ---------- take-profit mode (operator-selectable) ----------
    # "profile" = use the scalp/swing ladder below (default, unchanged behaviour).
    # "global"  = override both profiles with EXITS["global"] (one ladder for every trade).
    # "single"  = one take-profit: sell EXITS["single"]["tp_frac"] at tp_at, let the rest
    #             trail at trail_pct. Simplest "sell take profit" for a high-win-rate scalp.
    "take_profit_mode": "profile",
    "global": {"tp1_at": 1.5, "tp1_frac": 0.33, "tp2_at": 2.0, "tp2_frac": 0.5, "trail_pct": 0.20},
    "single": {"tp_at": 1.35, "tp_frac": 0.5, "trail_pct": 0.18},
    "missing_quote_closes": 3,     # close only after N quote misses in a row (one API blip is not an exit)
    # PAPER runner rules (after TP1 sells part): the rest must not squat on a slot for days.
    "breakeven_after_half": True,  # after TP1 the stop moves up to entry (never down)
    "runner_trail_pct": 0.20,      # runner trails at most 20% below its peak (tighter profile trail wins)
    "runner_max_minutes": 180,     # runner closes 3h after TP1, freeing the slot
    # ---------- quote sanity: a bad print must never reprice the book ----------
    # A position is marked from a quote that refreshes every ~15s. A single print
    # that jumps 25x (or falls to 1/25th) in one tick is not a rally: it is a
    # decimals mismatch, a different pool for the same mint, or a duplicated pair.
    # The equity bug of 2026-11-14 was exactly this: one bad mark turned a $160
    # book into $549,195,436 (3,432,471x) on the dashboard.
    # A rejected print is ignored until `quote_confirm_reads` reads agree with each
    # other, so a genuine gap-up still gets through on the second look.
    "quote_max_jump_x": 25.0,      # up: price may at most 25x in one tick
    "quote_max_drop_x": 25.0,      # down: at most 1/25th in one tick
    "quote_confirm_reads": 2,      # agreeing reads needed to accept a new level
    "stop_fill_at_stop": True,     # PAPER: a stop fills at the stop level, not at a gapped-through
                                   # rug print. Keeps a 1.5%-risk trade a 1.5% loss, so the daily
                                   # stop is reachable instead of being blown past in one tick.
                                   # False = raw market fill (accept full gap risk).
    "profiles": {
        "scalp": {"stop_pct": 0.20, "tp1_at": 1.5, "tp1_frac": 0.33, "tp2_at": 2.0, "tp2_frac": 0.5,
                  "trail_pct": 0.20, "timeout_hours": 3.0, "timeout_min_gain": 0.05,
                  "max_hold_hours": 4.0},     # retune: was 6h
        "swing": {"stop_pct": 0.30, "tp1_at": 1.5, "tp1_frac": 0.33, "tp2_at": 2.0, "tp2_frac": 0.5,
                  "trail_pct": 0.30, "timeout_hours": 3.0, "timeout_min_gain": 0.05,
                  "max_hold_hours": 4.0},     # owner: swing max hold 4h (was 72h; timeout was 24h)
    },
}

# ---------- Entry confirmation (MERGE, from old desk agents/research.py + scanner gates) ----------
# Re-quote the coin on a second source right before the paper fill. The paper fill uses the fresh price.
ENTRY = {
    "max_price_gap_pct": 25.0,     # old desk research.max_price_gap_pct_paper (LIVE was 10)
    "min_confirm_liq_ratio": 0.5,  # old desk research.confirm_liq_ratio_min: 2nd source must show >= 50% of the liquidity
    "require_confirm_quote": True, # no confirming quote -> no trade
}

# ---------- Trade signal engine (buy / sell recommendation) ----------
# Turns the recorded fields into a clear, auditable call:
#   action      BUY | SELL | HOLD | AVOID
#   direction   bullish | bearish | neutral
#   confidence  0-100 conviction
#   win_prob    calibrated probability the trade reaches TP1 before the stop
#   entry / stop / TP1 / TP2 price plan + a one-line signal string
# It is deterministic (no model, no I/O) so the same inputs always produce the
# same call, and the backtest can measure it honestly.
SIGNAL = {
    "enabled": True,
    # Only a BUY at or above this conviction may open a paper/live position.
    # Lower = more trades and more noise; raise it when the journal shows the
    # low-confidence bucket losing.
    "min_confidence": 60,
    "min_win_probability": 0.52,
    # A bearish read must never be bought. When True, a bearish direction blocks
    # entry even if the raw score is high (e.g. a pump that is already fading).
    "require_bullish": True,
    # Exit a held position when the signal flips bearish at/above this conviction.
    # This is the "sell take profit" safety valve: it banks a winner that has
    # stopped going up instead of waiting for the trailing stop.
    "sell_on_bearish": True,
    "sell_confidence": 68,
    # Component weights (sum to 1.0). Each component is scored -1..+1, so the
    # composite is also -1..+1 before it is mapped to 0-100 confidence.
    "weights": {
        "momentum": 0.24,       # change_m5 / change_h1, volume still arriving
        "flow": 0.22,           # buys vs sells, buyer diversity
        "liquidity": 0.16,      # liquidity depth + volume/mcap
        "holders": 0.14,        # top wallet / top 10 concentration
        "narrative": 0.12,      # meme scorecard total
        "safety": 0.12,         # security flags (block/halve) and regime
    },
    # Calibration: the logistic slope that maps composite score -> win probability.
    # A score of 0 maps to 0.50; +0.5 maps to ~0.62 with slope 0.55. Capped so the
    # desk never claims certainty.
    "win_prob_slope": 0.55,
    "win_prob_floor": 0.30,
    "win_prob_cap": 0.85,
}

PAPER = {
    # Paper bankroll. NOT cosmetic: it sets the largest ticket the risk cap can
    # produce, and the ticket must clear min_ticket_usd AFTER the caution layer
    # multiplies it down.
    #
    #   ticket = max_risk_per_trade * equity / stop_pct * caution_mult
    #
    # The caution cuts multiply (regime halve, security halve, breaker, Jupiter
    # impact), so the realistic worst case is 0.5 * 0.5 = 0.25x -- risk-off
    # regime PLUS one security flag. On a $50 book that is $0.62-$0.94, so the
    # paper floor is $0.50 (NOT $2) or the desk would silently skip every setup
    # in that state -- the original "sizing deadlock":
    #
    #   cuts  mult   scalp(20%)   swing(30%)
    #     0   1.000   $3.75 OK     $2.50 OK
    #     1   0.500   $1.88 OK     $1.25 OK
    #     2   0.250   $0.94 OK     $0.62 OK      <- still trades
    #     3   0.125   $0.47 SKIP   $0.31 SKIP    <- genuinely cautious, by design
    #
    # A three-cut skip is the desk being risk-off, and it shows up in
    # stats["floor_rejects"] instead of looking like a normal reject.
    # If you raise the bankroll, raise the floor with it -- and never set the
    # floor above risk.min_viable_equity(stop, 0.25) or the deadlock returns.
    "starting_equity_usd": 50,     # owner's bankroll (2026-11-14). $50 is small, so the paper
                                   # floor below is scaled to it: at $50 a two-cut ticket is
                                   # $0.63-$0.94, and a $2 floor would silently reject every
                                   # setup (the original "sizing deadlock").
    "min_ticket_usd": 0.20,        # paper fee floor; live still uses RISK["min_ticket_usd"]
    "fee_pct": 0.01,               # pump.fun charges ~1% per swap on the curve
    "slippage_pct": 0.02,          # assume 2% worse than the quoted price
    # A position is never marked above max_mark_mult times what it cost, nor
    # below min_mark_mult of it. Outside that band the mark is corrupt (a wrong
    # pool, a decimals mismatch, a hand-edited ledger) and the position is valued
    # at cost instead, so one bad number cannot fake a $549M equity again.
    # 1000x is far beyond any real memecoin outcome from a $2-$6 ticket.
    "max_mark_mult": 1000.0,
    "min_mark_mult": 0.001,
}

# ---------- Seat 5b: LIVE execution (real money, off by default) ----------
# The desk boots in paper mode every single time. Live is never remembered
# across a restart, and arming it needs the phrase below plus a key in the
# environment. These caps are the last line of defence, not a suggestion.
EXECUTION = {
    "default_mode": "paper",           # paper | live. Always paper on boot.
    "live_max_trade_usd": 25.0,        # hard ceiling on any single live ticket
    "live_max_notional_usd": 100.0,    # hard ceiling on all live money at risk at once
    "live_daily_loss_pct": 0.03,       # -3% on the live book -> no new live buys today
    "live_max_slippage_bps": 300,      # 3%. Wider than this and the route is a trap
    "live_confirm_phrase": "I understand this is real money",
    "jupiter_base": "https://lite-api.jup.ag/swap/v1",
    "usdc_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "sol_mint": "So11111111111111111111111111111111111111112",
    "confirm_timeout_seconds": 45,
    "starting_equity_usd": 0.0,        # live equity is measured, not seeded
}

CYCLE_SECONDS = 30                 # crypto scan cycle
STOCK_CYCLE_SECONDS = 15 * 60      # stocks: every 15 min while the market is open

# How long a rejected token sits out, by the check that failed (minutes)
BENCH_MINUTES = {
    "exited": 360,                 # stopped out or closed: no revenge re-entry for 6h
    "mint_authority": 100_000, "freeze_authority": 100_000,
    "top_wallet": 120, "top_10": 120, "no_sells": 240,
    "skip": 60, "watch": 15, "score": 20,
    "age": 10, "liquidity": 10, "volume": 10, "mcap": 15, "trades": 10,
}
BENCH_MINUTES.update({"style": 30, "chase": 20, "price_gap": 10, "liq_unconfirmed": 15, "no_confirm_quote": 5})
DEFAULT_BENCH = 30

# ---------- Meme coin value framework ----------
# What actually gives a memecoin value. Weights sum to 1.0.
MEME = {
    "weights": {
        "community": 0.30,      # real, distributed holders + socials that talk back
        "dev_legit": 0.25,      # dev wallet clean, not sitting on the float, no rug history
        "narrative": 0.20,      # animal / celebrity / culture: does it have a story
        "distribution": 0.15,   # top holders, bundle/sniper concentration
        "utility": 0.10,        # does it do anything beyond the ticker
    },
    "narratives": {
        "animal": ["dog", "cat", "inu", "frog", "pepe", "bonk", "wif", "rat", "bear", "bull",
                   "monkey", "ape", "penguin", "duck", "shib", "kitty", "pup", "hamster",
                   "hippo", "moose", "goat", "fish", "bird", "wolf", "fox"],
        "celebrity": ["trump", "elon", "musk", "biden", "kanye", "drake", "ronaldo", "messi",
                      "taylor", "bieber", "obama", "putin", "bezos", "zuck", "mrbeast"],
        "utility": ["ai", "agent", "gpt", "bot", "swap", "dex", "lend", "vault", "yield",
                    "stake", "dao", "launch", "tools", "app", "protocol", "infra", "rwa"],
        "community": ["community", "cult", "army", "fam", "together", "united", "people",
                      "movement", "meme", "culture"],
    },
    # dev / creator investigation limits
    "max_dev_hold_pct": 0.05,        # dev holding more than 5% of supply = exit liquidity risk
    "max_creator_prev_rugs": 0,      # any prior rugged launch from the same creator = no
    "max_sniper_pct": 0.20,          # bundled/sniper wallets above this = no
    "min_holders": 150,              # fewer real holders than this = not a community yet
    "min_community_score": 35,       # 0-100, below this the desk will not call it a community coin (retune: was 40)
    # score -> verdict bands for the meme scorecard
    "bands": {"strong": 70, "watch": 50},
    # ---------- memecoin entry tactics (from the public "meme bot" guide) ----------
    # These were measured on real GeckoTerminal candles by scripts/meme_tactics_eval.py,
    # scripts/backtest_gates.py and (the decisive run) scripts/winrate_lab.py on 120
    # pools / 114,033 candles. See RECOMMENDATION.md and MEME_TACTICS.md for the numbers.
    # Flip a flag only if the measurement says it clears the bar
    # (>=30 trades, >=10 pools, +5pp win rate, PF >= 1.2).
    "pullback_entry": True,          # rule B: refuse to buy the top of the range.
                                     # PROMOTED 2026-11-14: 20% dip + fast ladder + 30%
                                     # stop -> n=74, 11 pools, 68.9% win, PF 3.75
                                     # (leave-one-pool-out worst 58.5% / 2.71).
    # Shadow mode: record what the pullback rule WOULD have decided on every
    # candidate the desk actually trades, without blocking a single entry. The
    # package was measured on the ungated signal; this measures it on the real,
    # fully-gated population. scripts/shadow_report.py compares the two groups.
    "pullback_shadow": True,
    "pullback_min_drop": 0.20,       # must be >=20% below the 20-bar high (measured)
    "flash_pump_guard": False,       # rule C: skip entries right after a spike
    "flash_pump_pct": 0.25,          # a >25% 5m candle = do not chase
    "atr_stop": False,               # rule A: stop = clamp(k*ATR%, 8%, 60%)
    "atr_stop_k": 1.5,
    "vol_ceiling": False,            # rule D: skip when ATR% is insane
    "vol_ceiling_pct": 0.20,
}

# ---------- FOMO evidence seat (optional) ----------
# fomoapi.io is an independent third-party bridge to fomo.family's coin pages.
# It is OFF unless FOMO_API_KEY is in the environment: with no key every lookup
# returns "unknown", no credits are spent, and the desk behaves exactly as it
# did before. Nothing here can raise into the trading loop.
FOMO = {
    "enabled": True,               # master switch; still requires FOMO_API_KEY
    # "signals" = warnings + devs (500 credits) on every candidate that gets this
    # far. "thesis" = also score what traders wrote (+1,250 credits). At the free
    # 250k/month tier that is ~500 coins vs ~140 coins, so signals is the default.
    "level": "signals",
    # A confident, strongly bearish thesis crowd is a veto, not a nudge. The
    # crowd can only ever say "no" here; it can never say "yes" on its own.
    "bearish_kill_score": 25,
    "bearish_kill_confidence": ("medium", "high"),
    # Bounded nudge to the meme community score, in points. Hard cap in code.
    "max_nudge": 8,
    # Only spend thesis credits on coins that already cleared every other gate.
    "thesis_only_for_finalists": True,
}

# ---------- social attention (ticker-keyed, free, no keys) ----------
# The meme scorecard reads the token's NAME. This reads whether anyone is
# talking about the TICKER right now. Sources: Telegram public previews
# (t.me/s/<channel>) and DexScreener boosts/profiles. X/Twitter is not read --
# no free tier, and scraping it breaks their terms.
SOCIAL = {
    "enabled": True,
    # Record the pulse on every signal so it can be measured against realised
    # P&L. This is a label, never a gate -- same rule as meme_tactics shadow.
    "shadow": True,
    # A dead pulse may only downgrade a BUY once the measurement clears the bar.
    "gate": False,
    "min_score": 10,
    # Public channels with web preview enabled. Most big channels turn the
    # preview off, so this list is the subset that actually works -- verified
    # live, not guessed. The list is a watchlist, not an endorsement.
    "channels": [
        "binance_announcements",   # exchange listings: the loudest catalyst
        "whale_alert",             # large transfers
        "pumpfun",                 # new launches
        "raydium",                 # Solana DEX pools
    ],
    "max_channels": 6,
    "cache_s": 180,          # Telegram page cache
    "dex_cache_s": 300,      # DexScreener boosts/profiles cache
}

# Timeframe must match how long you intend to hold (from the desk playbook card).
HOLD_HORIZONS = [
    {"label": "fresh launch", "max_age_minutes": 60, "hold": "10-60 minutes",
     "chart": "15s or 30s", "rule": "Only draw lines once price has built memory."},
    {"label": "first hours", "max_age_minutes": 360, "hold": "a few hours",
     "chart": "1m or 5m", "rule": "Trade the structure, not the first candle."},
    {"label": "couple of days", "max_age_minutes": 2880, "hold": "1-3 days",
     "chart": "15m, 1H or 4H", "rule": "Higher timeframe decides, lower one times the entry."},
    {"label": "long hold", "max_age_minutes": 10**9, "hold": "weeks+",
     "chart": "weekly", "rule": "Only the weekly trend matters at this horizon."},
]

# ---------- Solana Agent ----------
AGENT = {
    "cycle_seconds": 30,           # how often the agent wakes up
    "chain_budget": 24,            # RPC fact checks per cycle
    "analyst_budget": 6,           # analyst calls per cycle: caps the bill
    "demo_when_empty": False,      # MERGE: no synthetic pools in paper P&L (demo can still be stepped by hand, never filled)
    "max_events": 400,             # ring buffer size for the agent log
}


