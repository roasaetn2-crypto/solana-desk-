"""Operator-confirmed PAPER settings: registry, proposals, confirm / reject.

Desk AI (or a preset button) may only PROPOSE a change. Nothing is applied until the operator
taps Confirm, which re-validates every value against the registry bounds below, applies it to
the running paper config, persists it (local file + Turso kv when configured) so it survives a
redeploy, and journals it as a "settings_change" row.

Two tiers of setting:

  * PUBLIC (REGISTRY). The Desk AI, a preset, and any signed-in browser may propose
    these. They are the trading knobs: style, risk, exits, scanner gates, signal.
  * OWNER (OWNER_REGISTRY). Only an owner session (see app._owner) may propose these:
    the paper bankroll, the live caps, the chain scam kills, the dev/sniper kills,
    the quote confirmation, and the loop's call budgets. The AI never sees them and
    create() drops them unless owner=True.

Neither tier can ever reach:
  * secrets: keys, tokens, passwords, SOLANA_PRIVATE_KEY, the live confirm phrase
    (refused by _SECRET_RE before any role check; never listed in a snapshot)
  * desk.mode: the mode switch is its own endpoint and still needs the confirm
    phrase and live_mod.ready(), so no settings proposal can arm live trading.

Every change in either tier is still a PENDING proposal that must be Confirmed,
is re-validated against the registry bounds, and is journaled as settings_change
with owner=True/False so the audit trail shows who changed what.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable

import config
from config import AGENT, CHAIN, ENTRY, EXECUTION, EXITS, MEME, PAPER, RISK, SCAN, SIGNAL
from desk import meme_tactics
from desk.ops import data_file

PROPOSAL_TTL_S = 24 * 3600
MAX_PENDING = 30
MAX_CHANGES = 20

# Names an AI might use for things it must never touch. Matched against the proposed key.
_EXCLUDED_RE = re.compile(
    r"(^|[._])(mode|live|execution|private|secret|token|password|pass|api_?key|key|wallet|"
    r"mint_authority|freeze_authority|top_wallet|top_10|max_dev_hold|dev_hold|sniper|prev_rugs|"
    r"creator|rug|kill|scam|require_confirm_quote|starting_equity|bankroll|confirm_phrase)($|[._])",
    re.I)
EXCLUDED = (
    "desk.mode", "execution.*", "live.*", "SOLANA_PRIVATE_KEY", "keys / tokens / secrets",
    "chain.allow_mint_authority", "chain.allow_freeze_authority", "chain.max_top_wallet",
    "chain.max_top_10", "meme.max_dev_hold_pct", "meme.max_sniper_pct",
    "meme.max_creator_prev_rugs", "entry.require_confirm_quote", "paper.starting_equity_usd",
)

# A secret is refused for EVERY role, owner included. There is no code path in
# this module that can read a key back out or write one in: the desk reads keys
# from the environment at boot and never exposes them. Matched against the key.
_SECRET_RE = re.compile(
    r"(private|secret|token|password|passphrase|api_?key|confirm_phrase|seed|mnemonic"
    r"|(^|[._])key($|[._]))", re.I)
SECRETS = ("SOLANA_PRIVATE_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "HELIUS_API_KEY",
           "DESK_TOKEN", "the live confirm phrase", "any paired-device token")

# What an owner still cannot reach through the settings path, and why.
OWNER_EXCLUDED = (
    "desk.mode", "keys / tokens / secrets", "the live confirm phrase",
)


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    kind: str                 # "pct" (fraction 0-1), "float", "int", "bool", "choice"
    get: Callable[[], Any]
    set: Callable[[Any], None]
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()
    unit: str = ""
    help: str = ""
    owner_only: bool = False   # reachable only through an owner session

    def coerce(self, v: Any) -> tuple[Any, str | None]:
        """(value clamped into bounds, note) or raise ValueError when it can't be read at all."""
        if self.kind == "bool":
            if isinstance(v, bool):
                return v, None
            s = str(v).strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True, None
            if s in ("false", "0", "no", "off"):
                return False, None
            raise ValueError("not a yes/no value")
        if self.kind == "choice":
            s = str(v).strip().lower()
            if s not in self.choices:
                raise ValueError(f"must be one of {', '.join(self.choices)}")
            return s, None
        if isinstance(v, bool):
            raise ValueError("not a number")
        if isinstance(v, str):
            s = v.strip().rstrip("%").strip()
            f = float(s)
            if self.kind == "pct" and v.strip().endswith("%"):
                f /= 100.0
        else:
            f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("not a number")
        # "pct" settings are fractions; accept 15 meaning 15% when it is clearly out of range
        if self.kind == "pct" and self.hi is not None and self.hi <= 1 and 1 < f <= 100:
            f /= 100.0
        note = None
        if self.lo is not None and f < self.lo:
            f, note = self.lo, f"raised to the floor {self.lo}"
        if self.hi is not None and f > self.hi:
            f, note = self.hi, f"lowered to the cap {self.hi}"
        if self.kind == "int":
            return int(round(f)), note
        return round(f, 4), note

    def describe(self) -> dict:
        d = {"key": self.key, "label": self.label, "group": self.group, "type": self.kind,
             "current": self.get(), "unit": self.unit}
        if self.kind == "choice":
            d["choices"] = list(self.choices)
        elif self.kind != "bool":
            d["min"], d["max"] = self.lo, self.hi
        if self.help:
            d["help"] = self.help
        return d


# ---------- setters for the odd ones ----------
def _set_score(v):
    config.ANALYST_VALID_SCORE = config.MIN_SCORE = int(v)


def _set_max_red(v):
    config.ANALYST_MAX_RED_FOR_VALID = int(v)
    try:
        from desk import analyst
        analyst.ANALYST_MAX_RED_FOR_VALID = int(v)
    except Exception:
        pass


def _guard():
    from desk import guard
    return guard.BREAKER


def _d(dct: dict, k: str, cast=float):
    return (lambda: dct[k]), (lambda v: dct.__setitem__(k, cast(v)))


def _prof(name: str, k: str):
    return (lambda: EXITS["profiles"][name][k]), (lambda v: EXITS["profiles"][name].__setitem__(k, float(v)))


def _num(key, label, group, kind, getset, lo, hi, unit="", help=""):
    g, s = getset
    return Setting(key, label, group, kind, g, s, lo, hi, (), unit, help)


def _own(key, label, group, kind, getset, lo, hi, unit="", help=""):
    """A setting only an owner session may propose (still needs Confirm)."""
    return replace(_num(key, label, group, kind, getset, lo, hi, unit, help), owner_only=True)


EXITS.setdefault("style", "both")

_R: list[Setting] = [
    # style
    Setting("trading.style", "Trading style", "Style", "choice",
            lambda: EXITS.get("style", "both"), lambda v: EXITS.__setitem__("style", str(v)),
            choices=("both", "scalp", "swing"),
            help="scalp = only young coins (scalp exits), swing = only older coins, both = pick by age"),
    # risk / sizing
    _num("risk.max_risk_per_trade", "Risk per trade", "Risk", "pct", _d(RISK, "max_risk_per_trade"), 0.005, 0.10, "of book"),
    _num("risk.max_position_pct", "Max position size", "Risk", "pct", _d(RISK, "max_position_pct"), 0.02, 0.50, "of book"),
    _num("risk.max_pool_share", "Max share of pool", "Risk", "pct", _d(RISK, "max_pool_share"), 0.002, 0.05, "of pool"),
    _num("risk.max_open_positions", "Max open positions", "Risk", "int", _d(RISK, "max_open_positions", int), 1, 10),
    _num("risk.max_daily_loss_pct", "Daily loss stop", "Risk", "pct", _d(RISK, "max_daily_loss_pct"), 0.0, 0.10, "of book",
         "the desk halts new entries for the day past this; capped at 10%. 0 = OFF: no daily halt, "
         "the desk trades through a losing day (per-trade stops and the breaker still apply)."),
    Setting("risk.enforce_risk_budget", "Enforce open-risk budget", "Risk", "bool",
            lambda: bool(RISK.get("enforce_risk_budget", True)),
            lambda v: RISK.__setitem__("enforce_risk_budget", bool(v)),
            help="ON: an entry is trimmed or refused if the whole book's open risk would pass the "
                 "budget. OFF: every entry is allowed, so up to max-open-positions can stack. The "
                 "daily loss stop still fires and flattens the book either way. Turn OFF only if "
                 "the desk is refusing entries you want to take."),
    _num("risk.risk_budget_usd", "Open-risk budget", "Risk", "float", _d(RISK, "risk_budget_usd"), 0.0, 500.0, "USD",
         "dollars of open risk the whole book may carry at once. 0 = auto: the larger of the "
         "daily stop and what all max-open slots are allowed to risk, so the desk never locks "
         "itself out. Raise it if the desk says 'over its risk budget' and you accept more "
         "concurrency; lower it to cap simultaneous exposure."),
    Setting("risk.risk_budget_split_across_slots", "Split budget across slots", "Risk", "bool",
            lambda: bool(RISK.get("risk_budget_split_across_slots", True)),
            lambda v: RISK.__setitem__("risk_budget_split_across_slots", bool(v)),
            help="when on, per-trade risk is budget / max-open-positions, so every slot fits the "
                 "budget by construction. Turn off to size every ticket at Risk per trade and let "
                 "the budget limit how many can open."),
    _num("risk.max_daily_loss_usd", "Daily loss stop (USD)", "Risk", "float", _d(RISK, "max_daily_loss_usd"), 0.0, 500.0, "USD",
         "hard dollar daily stop: the desk halts new entries once the day is down this much. "
         "0 = off (use the % limit above). When above 0 this replaces the % limit, so you can "
         "name the day's loss in cash on a small paper book."),
    Setting("risk.auto_continue_after_daily_stop", "Keep trading after daily stop", "Risk", "bool",
            lambda: bool(RISK.get("auto_continue_after_daily_stop", False)),
            lambda v: RISK.__setitem__("auto_continue_after_daily_stop", bool(v)),
            help="PAPER only. The daily stop still fires and is still logged, but it will not pause "
                 "new entries (same as tapping Continue today every day). Live trading always honours "
                 "its daily stop and ignores this."),
    Setting("risk.compound", "Compound wins", "Risk", "bool", lambda: bool(RISK["compound"]),
            lambda v: RISK.__setitem__("compound", bool(v))),
    _num("paper.min_ticket_usd", "Min paper ticket", "Risk", "float", _d(PAPER, "min_ticket_usd"), 0.10, 10, "USD"),
    _num("paper.fee_pct", "Simulated fee", "Risk", "pct", _d(PAPER, "fee_pct"), 0.0025, 0.05, "per swap"),
    _num("paper.slippage_pct", "Simulated slippage", "Risk", "pct", _d(PAPER, "slippage_pct"), 0.005, 0.10, "per swap"),
    # drawdown breaker
    _num("breaker.reduce_day_loss_pct", "Breaker: half size at day loss", "Breaker", "pct",
         (lambda: _guard()["reduce_day_loss_pct"], lambda v: _guard().__setitem__("reduce_day_loss_pct", float(v))),
         0.005, 0.05, "of book"),
    _num("breaker.streak_reduce", "Breaker: losses in a row to half size", "Breaker", "int",
         (lambda: _guard()["streak_reduce"], lambda v: _guard().__setitem__("streak_reduce", int(v))), 1, 10),
    _num("breaker.streak_pause", "Breaker: losses in a row to pause", "Breaker", "int",
         (lambda: _guard()["streak_pause"], lambda v: _guard().__setitem__("streak_pause", int(v))), 2, 15),
    _num("breaker.cooldown_minutes", "Breaker: pause length", "Breaker", "int",
         (lambda: int(_guard()["cooldown_s"] // 60), lambda v: _guard().__setitem__("cooldown_s", int(v) * 60)),
         5, 240, "min"),
    # exits
    _num("exits.scalp_max_age_hours", "Scalp if coin younger than", "Exits", "float", _d(EXITS, "scalp_max_age_hours"), 0.5, 24, "h"),
    _num("exits.stop_pct", "Fallback stop loss", "Exits", "pct", _d(EXITS, "stop_pct"), 0.05, 0.60, "",
         "stop loss can't be removed: floor 5%, cap 60%"),
    _num("exits.volume_decay_ratio", "Volume decay exit", "Exits", "float", _d(EXITS, "volume_decay_ratio"), 0.05, 0.8),
    _num("exits.missing_quote_closes", "Quote misses before close", "Exits", "int", _d(EXITS, "missing_quote_closes", int), 1, 10),
]
for _p, _stop_lo, _stop_hi, _mh_hi, _to_hi in (("scalp", 0.03, 0.50, 24, 12), ("swing", 0.05, 0.60, 168, 72)):
    _P = _p.capitalize()
    _R += [
        _num(f"exits.{_p}.stop_pct", f"{_P} stop loss", "Exits", "pct", _prof(_p, "stop_pct"), _stop_lo, _stop_hi, "",
             f"stop loss can't be removed: floor {int(_stop_lo * 100)}%"),
        _num(f"exits.{_p}.tp1_at", f"{_P} take profit 1", "Exits", "float", _prof(_p, "tp1_at"), 1.05, 5.0, "x"),
        _num(f"exits.{_p}.tp1_frac", f"{_P} TP1 sell share", "Exits", "pct", _prof(_p, "tp1_frac"), 0.10, 0.90),
        _num(f"exits.{_p}.tp2_at", f"{_P} take profit 2", "Exits", "float", _prof(_p, "tp2_at"), 1.10, 10.0, "x"),
        _num(f"exits.{_p}.tp2_frac", f"{_P} TP2 sell share", "Exits", "pct", _prof(_p, "tp2_frac"), 0.10, 1.0),
        _num(f"exits.{_p}.trail_pct", f"{_P} trailing stop", "Exits", "pct", _prof(_p, "trail_pct"), 0.05, 0.60),
        _num(f"exits.{_p}.timeout_hours", f"{_P} timeout", "Exits", "float", _prof(_p, "timeout_hours"), 0.25, _to_hi, "h"),
        _num(f"exits.{_p}.timeout_min_gain", f"{_P} timeout min gain", "Exits", "pct", _prof(_p, "timeout_min_gain"), 0.0, 0.50),
        _num(f"exits.{_p}.max_hold_hours", f"{_P} max hold", "Exits", "float", _prof(_p, "max_hold_hours"), 0.5, _mh_hi, "h"),
    ]
_R += [
    # scanner gates
    _num("scan.min_age_minutes", "Min coin age", "Scanner", "float", _d(SCAN, "min_age_minutes"), 0, 240, "min"),
    _num("scan.max_age_hours", "Max coin age", "Scanner", "float", _d(SCAN, "max_age_hours"), 1, 168, "h"),
    _num("scan.min_liquidity_usd", "Min liquidity", "Scanner", "int", _d(SCAN, "min_liquidity_usd", int), 2_000, 500_000, "USD"),
    _num("scan.min_volume_h1_usd", "Min 1h volume", "Scanner", "int", _d(SCAN, "min_volume_h1_usd", int), 1_000, 1_000_000, "USD"),
    _num("scan.min_mcap_usd", "Min market cap", "Scanner", "int", _d(SCAN, "min_mcap_usd", int), 5_000, 5_000_000, "USD"),
    _num("scan.max_mcap_usd", "Max market cap", "Scanner", "int", _d(SCAN, "max_mcap_usd", int), 100_000, 1_000_000_000, "USD"),
    _num("scan.min_trades_h1", "Min 1h trades", "Scanner", "int", _d(SCAN, "min_trades_h1", int), 5, 2_000),
    _num("scan.max_h1_change_pct", "Max 1h pump (chase)", "Scanner", "float", _d(SCAN, "max_h1_change_pct"), 20, 2_000, "%"),
    _num("scan.min_sells_ratio_h1", "Min sells/buys ratio", "Scanner", "float", _d(SCAN, "min_sells_ratio_h1"), 0.05, 0.8, "",
         "honeypot guard: floor 0.05"),
    Setting("scan.pump_only", "Pump.fun pools only", "Scanner", "bool", lambda: bool(SCAN["pump_only"]),
            lambda v: SCAN.__setitem__("pump_only", bool(v))),
    # entry confirmation
    _num("entry.max_price_gap_pct", "Max price gap between sources", "Entry", "float", _d(ENTRY, "max_price_gap_pct"), 2, 50, "%"),
    _num("entry.min_confirm_liq_ratio", "Min confirming liquidity", "Entry", "pct", _d(ENTRY, "min_confirm_liq_ratio"), 0.2, 1.0),
    # analyst / meme
    _num("analyst.valid_score", "Analyst min score", "Analyst", "int",
         (lambda: config.ANALYST_VALID_SCORE, _set_score), 3, 9, "/10"),
    _num("analyst.max_red_flags", "Analyst max red flags", "Analyst", "int",
         (lambda: config.ANALYST_MAX_RED_FOR_VALID, _set_max_red), 0, 5),
    _num("meme.min_holders", "Min holders", "Analyst", "int", _d(MEME, "min_holders", int), 20, 5_000),
    _num("meme.min_community_score", "Min community score", "Analyst", "int", _d(MEME, "min_community_score", int), 0, 90, "/100"),
    # loop
    _num("agent.cycle_seconds", "Scan cycle", "Loop", "int", _d(AGENT, "cycle_seconds", int), 20, 300, "s"),
    # AI budget: where the free-tier calls are spent
    Setting("brain.advise_on", "AI advice on", "AI", "choice",
            lambda: (os.getenv("BRAIN_ADVISE_ON", "exit") or "exit").strip().lower(),
            lambda v: os.environ.__setitem__("BRAIN_ADVISE_ON", str(v)),
            choices=("exit", "entry", "both", "off"),
            help="exit = only when a sell/exit rule fires (default, cheapest); "
                 "entry = only on buy candidates; both = every candidate and every exit; "
                 "off = never ask the AI, rules only. Takes effect immediately but is not "
                 "saved to .env, so re-apply it after a restart."),
]
_R += [
    # ---------- exit ladder (the TP geometry that actually runs) ----------
    # The pullback package ships its own ladder (fast/patient/slow/single) and,
    # while MEME["pullback_entry"] is on, it OVERRIDES the per-profile scalp/swing
    # take-profit rungs in exits.profile(). That made the visible "Scalp TP1/TP2"
    # rows look editable but do nothing. This setting exposes the ladder that is
    # really in force, so "fast" (and the rest) can be chosen from the dashboard.
    Setting("exits.ladder", "Exit ladder", "Exits", "choice",
            lambda: meme_tactics.current_ladder(),
            lambda v: meme_tactics.set_ladder(str(v)),
            choices=("fast", "patient", "slow", "single"),
            help="the take-profit geometry applied to every trade while the pullback package "
                 "is on. fast = TP1 +25% (sell 50%), TP2 +60%, trail 15%, stop 30%. "
                 "patient = TP1 +100%, TP2 +200%, trail 20%. slow = TP1 +50%, TP2 +100%. "
                 "single = one TP +35% then trail. Changing this clears any BRAIN_EXIT_LADDER "
                 "env override so the choice actually takes effect."),
    Setting("meme.pullback_entry", "Pullback entry + ladder", "Entry", "bool",
            lambda: bool(MEME.get("pullback_entry", False)),
            lambda v: MEME.__setitem__("pullback_entry", bool(v)),
            help="ON (default): only buy >=20% below the 20-bar high, and the Exit ladder above "
                 "overrides the per-profile TP rungs. OFF: entries no longer wait for a dip AND "
                 "the ladder stops overriding, so the Scalp/Swing TP1/TP2/trail rows take over. "
                 "Turn OFF only if you want the profile ladders to be the source of truth."),
    # ---------- take-profit mode (operator-selectable) ----------
    Setting("exits.tp_plan", "Take-profit plan", "Exits", "choice",
            lambda: str(EXITS.get("take_profit_mode") or "profile"),
            lambda v: EXITS.__setitem__("take_profit_mode", str(v)),
            choices=("profile", "global", "single"),
            help="profile = per scalp/swing ladder (default); global = one ladder for every trade; "
                 "single = one take-profit + trail for every trade"),
    _num("exits.global.tp1_at", "Global take profit 1", "Exits", "float",
         _d(EXITS.setdefault("global", {}), "tp1_at"), 1.05, 5.0, "x"),
    _num("exits.global.tp1_frac", "Global TP1 sell share", "Exits", "pct",
         _d(EXITS.setdefault("global", {}), "tp1_frac"), 0.10, 0.90),
    _num("exits.global.tp2_at", "Global take profit 2", "Exits", "float",
         _d(EXITS.setdefault("global", {}), "tp2_at"), 1.10, 10.0, "x"),
    _num("exits.global.tp2_frac", "Global TP2 sell share", "Exits", "pct",
         _d(EXITS.setdefault("global", {}), "tp2_frac"), 0.10, 1.0),
    _num("exits.global.trail_pct", "Global trailing stop", "Exits", "pct",
         _d(EXITS.setdefault("global", {}), "trail_pct"), 0.05, 0.60),
    _num("exits.single.tp_at", "Single take profit", "Exits", "float",
         _d(EXITS.setdefault("single", {}), "tp_at"), 1.05, 5.0, "x"),
    _num("exits.single.tp_frac", "Single sell share", "Exits", "pct",
         _d(EXITS.setdefault("single", {}), "tp_frac"), 0.10, 1.0),
    _num("exits.single.trail_pct", "Single trailing stop", "Exits", "pct",
         _d(EXITS.setdefault("single", {}), "trail_pct"), 0.05, 0.60),
    # ---------- signal engine (buy/sell recommendation gate) ----------
    Setting("signal.enabled", "Signal gate on", "Signal", "bool",
            lambda: bool(SIGNAL.get("enabled", True)),
            lambda v: SIGNAL.__setitem__("enabled", bool(v)),
            help="when on, entries must pass the BUY/SELL signal (bullish + confidence + win-prob)"),
    _num("signal.min_confidence", "Signal min confidence", "Signal", "int",
         _d(SIGNAL, "min_confidence", int), 30, 95, "%",
         "how sure the bullish read must be before an entry is allowed"),
    _num("signal.min_win_probability", "Signal min win probability", "Signal", "pct",
         _d(SIGNAL, "min_win_probability"), 0.30, 0.90, "",
         "calibrated win-probability floor for a BUY"),
    Setting("signal.require_bullish", "Require bullish entry", "Signal", "bool",
            lambda: bool(SIGNAL.get("require_bullish", True)),
            lambda v: SIGNAL.__setitem__("require_bullish", bool(v)),
            help="only take entries whose signal direction is bullish"),
    Setting("signal.sell_on_bearish", "Sell on bearish flip", "Signal", "bool",
            lambda: bool(SIGNAL.get("sell_on_bearish", True)),
            lambda v: SIGNAL.__setitem__("sell_on_bearish", bool(v)),
            help="exit a held position when its signal flips bearish above the sell confidence"),
    _num("signal.sell_confidence", "Bearish sell confidence", "Signal", "int",
         _d(SIGNAL, "sell_confidence", int), 50, 95, "%",
         "how sure a bearish read must be before a held position is sold"),
]
REGISTRY: dict[str, Setting] = {s.key: s for s in _R}
for _k in REGISTRY:  # belt and braces: nothing excluded may sneak into the registry
    assert not _EXCLUDED_RE.search(_k), _k

# ---------------------------------------------------------------------------
# OWNER-ONLY SETTINGS
# ---------------------------------------------------------------------------
# These are the knobs the AI and a paired phone are deliberately kept away
# from: the paper bankroll, the live caps, the scam/dev kills, the quote
# confirmation, and the loop's call budgets. They are reachable ONLY from the
# master session (the DESK_TOKEN login), and even then a change is still a
# pending proposal that must be Confirmed and is journaled. Nothing here can
# arm live trading: /api/mode still demands EXECUTION["live_confirm_phrase"]
# and live_mod.ready(), and no secret is ever settable or readable.
_OWNER_R: list[Setting] = [
    # bankroll
    _own("paper.starting_equity_usd", "Paper starting equity", "Owner \u00b7 Bankroll", "float",
         _d(PAPER, "starting_equity_usd"), 5.0, 100_000.0, "USD",
         "the paper book's starting balance. Changing it does NOT rewrite the ledger; "
         "reset the desk if you want a clean run from the new number."),
    # live caps (arming still needs the phrase)
    _own("execution.live_max_trade_usd", "Live max per trade", "Owner \u00b7 Live caps", "float",
         _d(EXECUTION, "live_max_trade_usd"), 1.0, 500.0, "USD",
         "hard per-ticket cap for real money. Live arming still needs the confirm phrase."),
    _own("execution.live_max_notional_usd", "Live max notional", "Owner \u00b7 Live caps", "float",
         _d(EXECUTION, "live_max_notional_usd"), 1.0, 5_000.0, "USD",
         "total real-money notional the desk may hold at once."),
    _own("execution.live_daily_loss_pct", "Live daily loss stop", "Owner \u00b7 Live caps", "pct",
         _d(EXECUTION, "live_daily_loss_pct"), 0.005, 0.25, "of book",
         "real-money daily stop. This one is never disabled by the paper override."),
    _own("execution.live_max_slippage_bps", "Live max slippage", "Owner \u00b7 Live caps", "int",
         _d(EXECUTION, "live_max_slippage_bps", int), 10, 2_000, "bps",
         "a live fill worse than this is refused rather than chased."),
    # chain kills
    _own("chain.max_top_wallet", "Max top-wallet share", "Owner \u00b7 Chain kills", "pct",
         _d(CHAIN, "max_top_wallet"), 0.01, 1.0, "of supply",
         "refuse a coin whose largest wallet holds more than this."),
    _own("chain.max_top_10", "Max top-10 share", "Owner \u00b7 Chain kills", "pct",
         _d(CHAIN, "max_top_10"), 0.05, 1.0, "of supply",
         "refuse a coin whose top 10 wallets hold more than this."),
    Setting("chain.allow_mint_authority", "Allow mint authority", "Owner \u00b7 Chain kills", "bool",
            lambda: bool(CHAIN.get("allow_mint_authority", False)),
            lambda v: CHAIN.__setitem__("allow_mint_authority", bool(v)), owner_only=True,
            help="OFF is the rug guard: a coin whose mint can still be inflated is refused. "
                 "Turning this ON lets the dev print new supply into your position."),
    Setting("chain.allow_freeze_authority", "Allow freeze authority", "Owner \u00b7 Chain kills", "bool",
            lambda: bool(CHAIN.get("allow_freeze_authority", False)),
            lambda v: CHAIN.__setitem__("allow_freeze_authority", bool(v)), owner_only=True,
            help="OFF is the honeypot guard: a coin whose accounts can be frozen is refused. "
                 "Turning this ON means your tokens can be locked so you cannot sell."),
    # dev / sniper kills
    _own("meme.max_dev_hold_pct", "Max dev holding", "Owner \u00b7 Dev kills", "pct",
         _d(MEME, "max_dev_hold_pct"), 0.0, 0.50, "of supply"),
    _own("meme.max_sniper_pct", "Max sniper share", "Owner \u00b7 Dev kills", "pct",
         _d(MEME, "max_sniper_pct"), 0.0, 1.0, "of supply"),
    _own("meme.max_creator_prev_rugs", "Max creator prior rugs", "Owner \u00b7 Dev kills", "int",
         _d(MEME, "max_creator_prev_rugs", int), 0, 10, "rugs"),
    # entry confirmation
    Setting("entry.require_confirm_quote", "Require confirming quote", "Owner \u00b7 Entry", "bool",
            lambda: bool(ENTRY.get("require_confirm_quote", True)),
            lambda v: ENTRY.__setitem__("require_confirm_quote", bool(v)), owner_only=True,
            help="ON: a second price source must agree before a buy. Turning it OFF removes the "
                 "cross-check that catches stale or spoofed quotes."),
    # risk behaviour
    Setting("risk.flatten_on_daily_stop", "Flatten on daily stop", "Owner \u00b7 Risk", "bool",
            lambda: bool(RISK.get("flatten_on_daily_stop", True)),
            lambda v: RISK.__setitem__("flatten_on_daily_stop", bool(v)), owner_only=True,
            help="ON: when the daily stop fires the desk closes what it holds. OFF: it only stops "
                 "new entries and lets open positions run."),
    # meme behaviour
    Setting("meme.flash_pump_guard", "Flash-pump guard", "Owner \u00b7 Meme", "bool",
            lambda: bool(MEME.get("flash_pump_guard", False)),
            lambda v: MEME.__setitem__("flash_pump_guard", bool(v)), owner_only=True),
    _own("meme.pullback_min_drop", "Pullback depth", "Owner \u00b7 Meme", "pct",
         _d(MEME, "pullback_min_drop"), 0.05, 0.80, "below high"),
    # scanner breadth
    _own("scan.pages", "Scanner pages", "Owner \u00b7 Scanner", "int", _d(SCAN, "pages", int), 1, 5),
    # loop budgets
    _own("agent.chain_budget", "Chain calls per cycle", "Owner \u00b7 Loop", "int",
         _d(AGENT, "chain_budget", int), 1, 100),
    _own("agent.analyst_budget", "AI calls per cycle", "Owner \u00b7 Loop", "int",
         _d(AGENT, "analyst_budget", int), 0, 50),
    _own("agent.max_events", "Event buffer size", "Owner \u00b7 Loop", "int",
         _d(AGENT, "max_events", int), 50, 2_000),
]
OWNER_REGISTRY: dict[str, Setting] = {s.key: s for s in _OWNER_R}
for _k in OWNER_REGISTRY:  # no secret may ever be an owner setting
    assert not _SECRET_RE.search(_k), _k
    assert _k not in REGISTRY, _k

_ALIASES = {"stop_loss": "exits.scalp.stop_pct", "stop": "exits.scalp.stop_pct", "risk": "risk.max_risk_per_trade",
            "style": "trading.style", "min_score": "analyst.valid_score", "max_positions": "risk.max_open_positions",
            "daily_loss": "risk.max_daily_loss_pct", "risk_budget": "risk.risk_budget_usd", "enforce_budget": "risk.enforce_risk_budget", "trailing_stop": "exits.scalp.trail_pct",
            "ladder": "exits.ladder", "exit_ladder": "exits.ladder"}

# ---------- presets: one card, every value that changes ----------
PRESETS: dict[str, dict] = {
    "scalper": {"label": "Scalper mode", "reason": "Scalp only: young coins, tight stop, quick take profit, short clock.",
                # tp_plan is pinned to "profile" so a previous global/single plan
                # can never leave scalper mode without its 1.3x/1.8x ladder. The
                # fractions are pinned too, so a tampered value cannot neuter TP.
                "values": {"trading.style": "scalp", "scan.max_age_hours": 6, "exits.scalp_max_age_hours": 6,
                           "exits.tp_plan": "profile", "exits.ladder": "fast",
                           "exits.scalp.stop_pct": 0.15, "exits.scalp.tp1_at": 1.3, "exits.scalp.tp2_at": 1.8,
                           "exits.scalp.tp1_frac": 0.33, "exits.scalp.tp2_frac": 0.5,
                           "exits.scalp.trail_pct": 0.15, "exits.scalp.timeout_hours": 1.5,
                           "exits.scalp.max_hold_hours": 4, "agent.cycle_seconds": 30}},
    "swing": {"label": "Swing mode", "reason": "Swing only: coins older than the scalp window, wider stop, 4h max hold.",
              "values": {"trading.style": "swing", "scan.max_age_hours": 72, "exits.scalp_max_age_hours": 6,
                         "exits.tp_plan": "profile", "exits.ladder": "patient",
                         "exits.swing.stop_pct": 0.30, "exits.swing.tp1_at": 1.5, "exits.swing.tp2_at": 2.5,
                         "exits.swing.trail_pct": 0.30, "exits.swing.timeout_hours": 3,
                         "exits.swing.max_hold_hours": 4}},
    "balanced": {"label": "Both (desk default)", "reason": "Back to the desk's default mix: scalp young coins, swing older ones.",
                 "values": {"trading.style": "both", "scan.max_age_hours": 48, "exits.scalp_max_age_hours": 6.0,
                            "exits.tp_plan": "profile", "exits.ladder": "fast",
                            "exits.scalp.stop_pct": 0.20, "exits.scalp.tp1_at": 1.5, "exits.scalp.tp2_at": 2.0,
                            "exits.scalp.tp1_frac": 0.33, "exits.scalp.tp2_frac": 0.5,
                            "exits.scalp.trail_pct": 0.20, "exits.scalp.timeout_hours": 3.0,
                            "exits.scalp.max_hold_hours": 4.0, "exits.swing.stop_pct": 0.30,
                            "exits.swing.tp1_at": 1.5, "exits.swing.tp2_at": 2.0, "exits.swing.trail_pct": 0.30,
                            "exits.swing.timeout_hours": 3.0, "exits.swing.max_hold_hours": 4.0}},
}


STYLE_PRESET = {"scalp": "scalper", "swing": "swing", "both": "balanced"}


def expand_style(raw: list[dict]) -> tuple[list[dict], str | None]:
    """If a proposal switches trading.style, bring the matching preset's related values along
    (the proposal's own explicit values win), so the card shows every value that changes."""
    style = next((str(x.get("proposed", "")).strip().lower() for x in raw or []
                  if isinstance(x, dict) and str(x.get("setting", "")) in ("trading.style", "style")), None)
    name = STYLE_PRESET.get(style or "")
    if not name:
        return raw, None
    own = set()
    for x in raw:
        try:
            own.add(resolve(x.get("setting")).key)
        except Exception:
            pass
    extra = [{"setting": k, "proposed": v, "reason": f"part of {PRESETS[name]['label']}"}
             for k, v in PRESETS[name]["values"].items() if k not in own]
    return list(raw) + extra, name


def style_allows(horizon: str) -> bool:
    st = EXITS.get("style", "both")
    return st == "both" or st == horizon


# ---------- validation ----------
class Rejected(ValueError):
    pass


def resolve(key: Any, owner: bool = False) -> Setting:
    """Look a setting up. owner=True opens the owner-only registry.

    Secrets are refused for every role: there is no branch below that can name
    one, and _SECRET_RE runs before the owner check.
    """
    k = str(key or "").strip()
    if not k:
        raise Rejected("no setting named")
    if _SECRET_RE.search(k):
        raise Rejected(f"'{k[:60]}' is a secret: keys, tokens and the confirm phrase are never changeable here")
    if not owner and (_EXCLUDED_RE.search(k) or k.lower() in ("mode", "desk.mode")):
        raise Rejected(f"'{k[:60]}' is owner-only: sign in with the desk key to change it")
    s = REGISTRY.get(k) or REGISTRY.get(_ALIASES.get(k.lower(), ""))
    if s is None and owner:
        s = OWNER_REGISTRY.get(k)
    if s is None:
        raise Rejected(f"unknown setting '{k[:60]}'")
    return s


def _cross_check(vals: dict[str, Any]) -> list[str]:
    """vals: full proposed state {key: value}. Returns problems."""
    bad = []
    for p in ("scalp", "swing"):
        if vals[f"exits.{p}.tp2_at"] <= vals[f"exits.{p}.tp1_at"]:
            bad.append(f"{p} take profit 2 must be above take profit 1")
        if vals[f"exits.{p}.timeout_hours"] > vals[f"exits.{p}.max_hold_hours"]:
            bad.append(f"{p} timeout can't be longer than its max hold")
    if vals["scan.min_mcap_usd"] >= vals["scan.max_mcap_usd"]:
        bad.append("min market cap must be below max market cap")
    if vals["scan.min_age_minutes"] >= vals["scan.max_age_hours"] * 60:
        bad.append("min coin age must be below max coin age")
    # Only cross-check when a daily stop is actually armed. With the stop OFF
    # (0) the breaker's half-size threshold has nothing to sit under, and the
    # old check rejected every proposal that disabled the daily stop.
    if (vals["risk.max_daily_loss_pct"] > 0
            and vals["breaker.reduce_day_loss_pct"] > vals["risk.max_daily_loss_pct"]):
        bad.append("breaker half-size loss can't be above the daily loss stop")
    if vals["breaker.streak_reduce"] >= vals["breaker.streak_pause"]:
        bad.append("breaker half-size streak must be shorter than the pause streak")
    if vals["risk.max_risk_per_trade"] > vals["risk.max_position_pct"]:
        bad.append("risk per trade can't exceed the max position size")
    st = vals["trading.style"]
    if st == "swing" and vals["scan.max_age_hours"] <= vals["exits.scalp_max_age_hours"]:
        bad.append("swing only needs max coin age above the scalp window, or nothing can pass")
    if st == "scalp" and vals["scan.min_age_minutes"] >= vals["exits.scalp_max_age_hours"] * 60:
        bad.append("scalp only needs min coin age below the scalp window, or nothing can pass")
    return bad


def current_values(owner: bool = False) -> dict[str, Any]:
    vals = {k: s.get() for k, s in REGISTRY.items()}
    if owner:
        vals.update({k: s.get() for k, s in OWNER_REGISTRY.items()})
    return vals


def validate_changes(raw: list[dict], owner: bool = False) -> tuple[list[dict], list[dict]]:
    """raw: [{setting, proposed, reason?}]. Returns (valid changes, dropped with reasons).
    Each valid change: {setting, label, current, proposed, note?}. No-ops are dropped."""
    ok, dropped, seen = [], [], set()
    for r in (raw or [])[:MAX_CHANGES * 2]:
        if not isinstance(r, dict):
            continue
        key = r.get("setting") or r.get("key")
        try:
            s = resolve(key, owner)
            v, note = s.coerce(r.get("proposed", r.get("value")))
        except (Rejected, ValueError, TypeError) as e:
            dropped.append({"setting": str(key)[:60], "why": str(e)[:160]})
            continue
        if s.key in seen:
            continue
        seen.add(s.key)
        cur = s.get()
        if v == cur:
            continue
        c = {"setting": s.key, "label": s.label, "type": s.kind, "unit": s.unit, "current": cur, "proposed": v}
        if note:
            c["note"] = note
        if r.get("reason"):
            c["reason"] = _clean(r.get("reason"), 160)
        ok.append(c)
    if ok:
        vals = current_values(owner)
        vals.update({c["setting"]: c["proposed"] for c in ok})
        bad = _cross_check(vals)
        if bad:
            return [], dropped + [{"setting": "(combined)", "why": b} for b in bad]
    return ok[:MAX_CHANGES], dropped


# ---------- store ----------
_lock = threading.RLock()
_OVERRIDES = "operator_settings.json"
_PROPOSALS = "proposals.json"
_state = {"proposals": [], "loaded": False}


def _now() -> float:
    return time.time()


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def _read(name: str, default):
    p = data_file(name)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def _write(name: str, kv_key: str, obj) -> None:
    p = data_file(name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    except Exception as e:
        print(f"[desk] settings: write {name} failed ({type(e).__name__})")
    try:
        from desk.store import sync_kv_file
        sync_kv_file(kv_key, p)
    except Exception:
        pass


def _load_proposals() -> list[dict]:
    if not _state["loaded"]:
        d = _read(_PROPOSALS, {})
        _state["proposals"] = list(d.get("proposals") or []) if isinstance(d, dict) else []
        _state["loaded"] = True
    return _state["proposals"]


def _save_proposals() -> None:
    _write(_PROPOSALS, "proposals", {"proposals": _state["proposals"][-200:]})


def reset_runtime() -> None:
    """Tests only."""
    _state["proposals"], _state["loaded"] = [], False


def apply_saved() -> dict:
    """Boot: re-apply operator-confirmed values (re-validated, clamped). Returns what applied."""
    d = _read(_OVERRIDES, {})
    vals = (d.get("values") or {}) if isinstance(d, dict) else {}
    applied = {}
    for k, v in vals.items():
        try:
            # owner=True so a confirmed owner-only value (e.g. the bankroll or a
            # live cap) survives a redeploy. Secrets still cannot resolve.
            s = resolve(k, owner=True)
            cv, _ = s.coerce(v)
        except (Rejected, ValueError, TypeError):
            continue
        s.set(cv)
        applied[s.key] = cv
    if applied:
        print(f"[desk] settings: re-applied {len(applied)} operator-confirmed paper setting(s)")
    return applied


def _persist_values(changed: dict) -> None:
    d = _read(_OVERRIDES, {})
    if not isinstance(d, dict):
        d = {}
    vals = d.get("values") or {}
    vals.update(changed)
    d["values"] = vals
    _write(_OVERRIDES, "operator_settings", d)


def _expire(now: float) -> None:
    changed = False
    for p in _load_proposals():
        if p["status"] == "pending" and now >= p["expires"]:
            p["status"], p["decided_at"] = "expired", _iso(now)
            changed = True
    if changed:
        _save_proposals()


def create(changes_raw: list[dict], reason: str, source: str, title: str = "",
           preset: str | None = None, owner: bool = False) -> dict:
    """Validate and store a PENDING proposal. Never applies anything.

    owner=True lets the proposal carry owner-only settings. It is recorded on
    the proposal so confirm() re-validates with the same authority, and so the
    journal shows an owner change as an owner change.
    """
    changes, dropped = validate_changes(changes_raw, owner)
    if not changes:
        return {"ok": False, "error": "nothing valid to propose", "dropped": dropped}
    now = _now()
    p = {"id": uuid.uuid4().hex[:12], "status": "pending", "source": str(source)[:20],
         "title": _clean(title, 60), "preset": preset, "reason": _clean(reason, 300),
         "owner": bool(owner),
         "changes": changes, "dropped": dropped, "created": now, "created_at": _iso(now),
         "expires": now + PROPOSAL_TTL_S}
    with _lock:
        props = _load_proposals()
        _expire(now)
        pend = [x for x in props if x["status"] == "pending"]
        for old in pend[:max(0, len(pend) - MAX_PENDING + 1)]:
            old["status"], old["decided_at"] = "expired", _iso(now)
        props.append(p)
        _save_proposals()
    return {"ok": True, "proposal": public(p)}


def create_preset(name: str, source: str = "operator") -> dict:
    pr = PRESETS.get(str(name or "").lower())
    if not pr:
        return {"ok": False, "error": f"unknown preset; pick one of {', '.join(PRESETS)}"}
    out = create([{"setting": k, "proposed": v} for k, v in pr["values"].items()],
                 pr["reason"], source, pr["label"], preset=name)
    if not out["ok"] and not out.get("dropped"):
        out["error"] = f"{pr['label']} is already what the desk runs"
    return out


def create_values(values: dict, title: str = "Operator change", source: str = "operator",
                  owner: bool = False) -> dict:
    """Operator-made proposal from explicit {key: value} pairs (no AI needed).

    Still goes through validate_changes and still needs Confirm, so the audit
    trail and the bounds are the same as an AI recommendation. owner=True is
    what lets an owner session name a bankroll / live-cap / kill setting.
    """
    if not isinstance(values, dict) or not values:
        return {"ok": False, "error": "no values given"}
    raw = [{"setting": k, "proposed": v} for k, v in list(values.items())[:MAX_CHANGES]]
    out = create(raw, "operator set " + ", ".join(sorted(values)[:4]), source, title, owner=owner)
    if not out["ok"] and not out.get("dropped"):
        out["error"] = "those values are already what the desk runs"
    return out


def _clean(s, n):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f<>`]", " ", str(s or ""))).strip()[:n]


def public(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "status", "source", "title", "preset", "reason", "changes", "dropped",
                                   "owner", "created_at", "expires", "decided_at", "decided_by", "applied", "error")}


def listing(include_done: int = 10) -> dict:
    with _lock:
        _expire(_now())
        props = _load_proposals()
        pend = [public(p) for p in props if p["status"] == "pending"]
        done = [public(p) for p in props if p["status"] != "pending"][-include_done:]
    return {"pending": pend[::-1], "recent": done[::-1], "ttl_hours": PROPOSAL_TTL_S // 3600}


def _find(pid: str) -> dict | None:
    for p in _load_proposals():
        if p["id"] == pid:
            return p
    return None


def proposal_owner(pid: str) -> bool:
    """True when this proposal carries owner-only settings.

    The route uses it to refuse a paired device confirming an owner proposal,
    so an owner change can never be applied by a phone that merely saw the id.
    """
    p = _find(str(pid))
    return bool(p and p.get("owner"))


def confirm(pid: str, who: str) -> dict:
    """Re-validate against today's bounds and apply. All changes or none."""
    with _lock:
        now = _now()
        _expire(now)
        p = _find(str(pid))
        if p is None:
            return {"ok": False, "code": 404, "error": "no such proposal"}
        if p["status"] != "pending":
            return {"ok": False, "code": 409, "error": f"proposal is {p['status']}"}
        owner = bool(p.get("owner"))
        raw = [{"setting": c["setting"], "proposed": c["proposed"]} for c in p["changes"]]
        changes, dropped = validate_changes(raw, owner)
        if not changes or len(changes) != len(raw) or any(c.get("note") for c in changes):
            p["status"], p["decided_at"], p["decided_by"] = "invalid", _iso(now), _clean(who, 40)
            p["error"] = "; ".join(d["why"] for d in dropped)[:300] or "values no longer valid or already set"
            _save_proposals()
            return {"ok": False, "code": 409, "error": p["error"], "proposal": public(p)}
        applied = []
        for c in changes:
            s = resolve(c["setting"], owner)
            old = s.get()
            s.set(c["proposed"])
            applied.append({"setting": s.key, "label": s.label, "from": old, "to": s.get()})
        _persist_values({a["setting"]: a["to"] for a in applied})
        p["status"], p["decided_at"], p["decided_by"], p["applied"] = "confirmed", _iso(now), _clean(who, 40), applied
        _save_proposals()
    _log("confirm", p, who, applied)
    return {"ok": True, "proposal": public(p), "applied": applied}


def reject(pid: str, who: str) -> dict:
    with _lock:
        now = _now()
        _expire(now)
        p = _find(str(pid))
        if p is None:
            return {"ok": False, "code": 404, "error": "no such proposal"}
        if p["status"] != "pending":
            return {"ok": False, "code": 409, "error": f"proposal is {p['status']}"}
        p["status"], p["decided_at"], p["decided_by"] = "rejected", _iso(now), _clean(who, 40)
        _save_proposals()
    _log("reject", p, who, [])
    return {"ok": True, "proposal": public(p)}


def _log(action: str, p: dict, who: str, applied: list[dict]) -> None:
    from desk.ops import journal
    journal("settings_change", action=action, proposal=p["id"], source=p.get("source"),
            title=p.get("title"), by=_clean(who, 40), owner=bool(p.get("owner")), mode="paper",
            changes=applied or [{"setting": c["setting"], "from": c["current"], "to": c["proposed"]}
                                for c in p["changes"]])
    if action == "confirm":
        print(f"[desk] settings: {who} confirmed {p['id']}: "
              + ", ".join(f"{a['setting']} {a['from']} -> {a['to']}" for a in applied))
    else:
        print(f"[desk] settings: {who} rejected {p['id']}")


def snapshot(owner: bool = False) -> dict:
    """GET /api/settings: every setting this session may propose, with bounds.

    owner=True adds the owner-only registry (bankroll, live caps, chain/dev
    kills, entry confirmation, loop budgets) and swaps the exclusion note for
    the two things even an owner cannot reach here: the mode switch and
    secrets. Secrets are never listed, in any role.
    """
    groups: dict[str, list] = {}
    for s in REGISTRY.values():
        groups.setdefault(s.group, []).append(s.describe())
    if owner:
        for s in OWNER_REGISTRY.values():
            groups.setdefault(s.group, []).append(s.describe())
    return {"mode": "paper", "owner": bool(owner), "groups": groups,
            "presets": {k: {"label": v["label"], "reason": v["reason"], "values": v["values"]}
                        for k, v in PRESETS.items()},
            "excluded": list(OWNER_EXCLUDED if owner else EXCLUDED),
            "ttl_hours": PROPOSAL_TTL_S // 3600}


def compact_for_ai() -> list[list]:
    """[[key, current, min, max or choices]] for the Desk AI prompt."""
    out = []
    for s in REGISTRY.values():
        if s.kind == "choice":
            out.append([s.key, s.get(), list(s.choices)])
        elif s.kind == "bool":
            out.append([s.key, s.get(), "bool"])
        else:
            out.append([s.key, s.get(), s.lo, s.hi])
    return out
