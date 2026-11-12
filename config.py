"""Every number the desk uses lives here. Retune the desk by editing this file only.

These are STARTING values, not advice. Run in paper mode, read the journal,
and move a number only when the journal tells you to.
"""

# ---------- Seat 1: scanner (free cuts, data already in the GeckoTerminal batch) ----------
SCAN = {
    # 3 feeds x `pages` calls per cycle. GeckoTerminal allows ~10 calls/min and a
    # 30s cycle means 2 cycles/min, so pages=1 (3 calls/cycle = 6/min) stays under
    # the limit. pages=2 was 12 calls/min: over the limit every single cycle, which
    # is why the desk kept hitting 429s and looked frozen.
    "pages": 1,                    # pages per cycle, per feed (1 page = 20 pools)
    "pump_only": False,            # True = only pump.fun curve + PumpSwap pools
    "pump_dexes": {"pump-fun", "pumpswap"},
    "min_age_minutes": 1,          # younger: the data is noise
    "max_age_hours": 48,           # older with no new move: not a launch any more
    "min_liquidity_usd": 3_000,
    "min_volume_h1_usd": 3_000,
    "min_mcap_usd": 8_000,
    "max_mcap_usd": 25_000_000,
    "min_trades_h1": 30,
    "min_sells_ratio_h1": 0.10,    # sells / buys. Below this nobody can sell = honeypot smell
    # --- young + thin guard ---------------------------------------------------
    # Paper testing showed both rugs (WINU, GTA 6) were 3-4 minutes old with
    # ~$20k liquidity and collapsed to near-zero inside a single 30s cycle, so
    # the stop could not fill. Under `young_minutes` we demand real depth.
    "young_minutes": 10,
    "young_min_liquidity_usd": 30_000,
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
MIN_SCORE = 5                        # 0-10, the analyst's score must clear this

# The bar for the analyst to say "valid_setup" (the only verdict the risk engine
# will size a ticket for). Lower = more trades, more noise. Raise it back to 7
# for the original, stricter desk.
ANALYST_VALID_SCORE = 5              # 0-10
ANALYST_MAX_RED_FOR_VALID = 3        # red flags allowed on a valid setup

# ---------- Seat 4: risk engine (pure code) ----------
RISK = {
    "max_risk_per_trade": 0.015,   # lose at most 1.5% of equity if the stop hits
    "max_position_pct": 0.05,      # no position above 5% of equity (memecoins)
    "max_pool_share": 0.02,        # never be more than 2% of the pool: you'd be the exit
    "max_open_positions": 6,
    # The breaker must be bigger than one position, or a single rug ends the day.
    # max_position_pct is 5%, so a token that goes to zero costs 5% of equity --
    # a 3% cap therefore halted the desk after the *first* bad trade. Measured in
    # paper: one rug (WINU, -91% gap) put the day at -4.8% and nothing traded
    # again until tomorrow. 8% allows roughly two full losses before standing down.
    "max_daily_loss_pct": 0.08,    # down 8% on the day -> desk stops until tomorrow
    "min_ticket_usd": 10,          # below this, fees eat the edge
    "quiet_hours": (0, 0),         # local hours with no new entries. (0,0) = trade 24/7.
                                   # Was (1, 7); disabled so the paper desk can trade now.
}

# ---------- Seat 5: exits (paper broker applies these, no model involved) ----------
EXITS = {
    "stop_pct": 0.30,              # -30% from entry -> close all
    "take_half_at": 1.5,           # 1.5x -> sell half, let the rest ride
    "volume_decay_ratio": 0.20,    # h6 volume / (h24/4) under this -> close
    "volume_decay_min_minutes": 30,
    "max_hold_hours": 4,           # memecoin momentum dies in hours, not days
    # --- runner rules: what happens to the half left after take_half_at ---
    # Without these the remainder sits in a slot until max_hold_hours, so all
    # slots fill and the desk stops taking new setups.
    "breakeven_after_half": True,  # after banking half, never give back entry
    "runner_trail_pct": 0.20,      # close the runner 20% off its peak
    "runner_max_minutes": 180,     # ...or after 3h, whichever comes first
    "max_no_quote_cycles": 3,      # quote API misses tolerated before forcing a close
}

# ---------- Entry confirmation (second read before a fill) ----------
# The scanner sees a price; this is the re-read that happens a second before the
# broker fills. It exists because a scan can be stale or the pool can be draining
# while the desk is still deciding. If the second read disagrees, there is no trade.
ENTRY = {
    "require_confirm_quote": True,   # no confirm quote at all -> do not fill
    "max_price_gap_pct": 5.0,        # scan vs confirm price more than 5% apart -> skip
    "min_confirm_liq_ratio": 0.80,   # confirm liquidity under 80% of scan -> pool is draining
}

PAPER = {
    "starting_equity_usd": 1_000,
    "fee_pct": 0.01,               # pump.fun charges ~1% per swap on the curve
    "slippage_pct": 0.02,          # assume 2% worse than the quoted price
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
    # entry-confirm rejects: transient, so retry quickly instead of sitting out 30m
    "no_confirm_quote": 5, "price_gap": 5, "liq_unconfirmed": 10,
    "age": 10, "liquidity": 10, "volume": 10, "mcap": 15, "trades": 10,
}
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
    "min_community_score": 35,       # 0-100, below this the desk will not call it a community coin
    # score -> verdict bands for the meme scorecard
    "bands": {"strong": 70, "watch": 50},
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
    "demo_when_empty": True,       # if a live scan returns nothing usable, feed a demo batch
    "max_events": 400,             # ring buffer size for the agent log
}
