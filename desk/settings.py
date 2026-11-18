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
from config import AGENT, CHAIN, ENTRY, EXECUTION, EXITS, MEME, PAPER, RISK, SCALP, SCAN, SIGNAL, SWING
from desk import meme_tactics
from desk.ops import data_file

PROPOSAL_TTL_S = 24 * 3600
MAX_PENDING = 30
# How many values one proposal may change. A full preset ("Legend Scalper") moves
# ~45, and the old cap of 20 silently truncated it -- the card looked applied but
# half the values never landed. 64 is still a sane guard against a runaway proposal.
MAX_CHANGES = 64

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
    Setting("paper.real_fees", "Charge real Jupiter fees", "Risk", "bool",
            lambda: bool(PAPER.get("use_live_fee_model", True)),
            lambda v: PAPER.__setitem__("use_live_fee_model", bool(v)),
            help="PAPER only. On: charge the real Solana fee (base signature + capped priority, "
                 "priced off the live SOL spot) on every buy and sell, so paper P&L matches a live "
                 "fill. Off: fall back to the flat 'Simulated fee' percentage below."),
    _num("paper.fee_pct", "Simulated fee (legacy)", "Risk", "pct", _d(PAPER, "fee_pct"), 0.0025, 0.05, "per swap",
         help="Only used when 'Charge real Jupiter fees' is off."),
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
    Setting("breaker.cooldown_enabled", "Breaker: pause after a loss streak", "Breaker", "bool",
            lambda: bool(_guard().get("cooldown_enabled", True)),
            lambda v: _guard().__setitem__("cooldown_enabled", bool(v)),
            help="Off skips only the pause. Half size after losses and the daily loss stop stay on."),
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
    Setting("meme.retest_entry", "Also buy breakout retests", "Entry", "bool",
            lambda: bool(MEME.get("retest_entry", False)),
            lambda v: MEME.__setitem__("retest_entry", bool(v)),
            help="OFF (default, unmeasured). ON: besides the 20% dip, also buy when price broke a "
                 "20-bar resistance on a volume surge, came back to retest it, held, and is bouncing. "
                 "Only works while Pullback entry is ON (dip OR retest, same exit ladder)."),
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
    _own("execution.live_min_ticket_usd", "Live fee floor", "Owner \u00b7 Live caps", "float",
         _d(EXECUTION, "live_min_ticket_usd"), 1.0, 500.0, "USD",
         "smallest real-money ticket. Must stay at or below Live max per trade, "
         "or every entry is refused."),
    _own("execution.live_max_notional_usd", "Live max notional", "Owner \u00b7 Live caps", "float",
         _d(EXECUTION, "live_max_notional_usd"), 1.0, 5_000.0, "USD",
         "total real-money notional the desk may hold at once."),
    _own("execution.live_daily_loss_pct", "Live daily loss stop", "Owner \u00b7 Live caps", "pct",
         _d(EXECUTION, "live_daily_loss_pct"), 0.005, 0.25, "of book",
         "real-money daily stop. This one is never disabled by the paper override."),
    _own("execution.live_max_slippage_bps", "Live max slippage", "Owner \u00b7 Live caps", "int",
         _d(EXECUTION, "live_max_slippage_bps", int), 10, 2_000, "bps",
         "a live fill worse than this is refused rather than chased."),
    _own("execution.live_priority_fee_max_lamports", "Priority fee cap",
         "Owner \u00b7 Live fees", "int",
         _d(EXECUTION, "live_priority_fee_max_lamports", int), 0, 20_000_000,
         "lamports",
         "ceiling on the priority fee Jupiter may bid per transaction. 0 = "
         "\"auto\", which has no ceiling and can exceed the trade's edge on a "
         "hot launch."),
    _own("execution.live_max_fee_share_pct", "Max fee share", "Owner \u00b7 Live fees",
         "pct", _d(EXECUTION, "live_max_fee_share_pct"), 0.005, 0.5, "of ticket",
         "refuse to arm when the round-trip cost on the smallest ticket exceeds "
         "this share of it. Stops the live floor drifting back under the fee."),
    _own("execution.live_sol_price_usd", "SOL price for fees", "Owner \u00b7 Live fees",
         "float", _d(EXECUTION, "live_sol_price_usd"), 0.0, 10_000.0, "USD",
         "pins the SOL price used to price lamport fees. 0 = use the pessimistic "
         "fallback, which over-states fees rather than under-stating them."),
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
    _own("meme.retest_volume_mult", "Retest breakout volume", "Owner \u00b7 Meme", "float",
         _d(MEME, "retest_volume_mult"), 1.0, 5.0, "x avg"),
    _own("meme.retest_zone_pct", "Retest zone", "Owner \u00b7 Meme", "pct",
         _d(MEME, "retest_zone_pct"), 0.005, 0.10, "of level"),
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

# ---------- fast scalper (1-10 minute lane) ----------
# These drive config.SCALP. When SCALP["enabled"] is on, exits.profile() overlays
# this block onto the scalp horizon (applied last, so it wins over the pullback
# ladder), and scanner.free_kill() enforces the freshness/size/depth window.
_R += [
    Setting("scalp.enabled", "Fast scalp lane", "Scalp", "bool",
            lambda: bool(SCALP.get("enabled", False)),
            lambda v: SCALP.__setitem__("enabled", bool(v)),
            help="ON: the SCALP block below becomes the scalp exit ladder and the scanner only "
                 "passes fresh coins inside the age/mcap/liquidity window. OFF: the normal "
                 "scalp/swing profiles run unchanged."),
    _num("scalp.max_hold_minutes", "Scalp max hold", "Scalp", "int",
         _d(SCALP, "max_hold_minutes", int), 1, 60, "min",
         "hard exit after this many minutes (1-10 is a true scalp)"),
    _num("scalp.timeout_minutes", "Scalp timeout", "Scalp", "int",
         _d(SCALP, "timeout_minutes", int), 1, 60, "min",
         "if not green by this many minutes, free the slot"),
    _num("scalp.timeout_min_gain", "Scalp timeout min gain", "Scalp", "pct",
         _d(SCALP, "timeout_min_gain"), 0.0, 0.20),
    Setting("scalp.entry_style", "Scalp entry style", "Scalp", "choice",
            lambda: str(SCALP.get("entry_mode") or "moving_candle"),
            lambda v: SCALP.__setitem__("entry_mode", str(v)),
            choices=("moving_candle", "dip", "breakout"),
            help="moving_candle = buy a green 5m candle that is still rising; dip = buy the "
                 "shakeout; breakout = only a fresh high of the lookback window"),
    _num("scalp.moving_candle_bars", "Moving-candle lookback", "Scalp", "int",
         _d(SCALP, "moving_candle_bars", int), 1, 6, "bars"),
    _num("scalp.moving_candle_min_body_pct", "Moving-candle min body", "Scalp", "pct",
         _d(SCALP, "moving_candle_min_body_pct"), 0.0, 0.05),
    _num("scalp.min_age_minutes", "Scalp min age", "Scalp", "float",
         _d(SCALP, "min_age_minutes"), 0, 120, "min"),
    _num("scalp.max_age_minutes", "Scalp max age (fresh)", "Scalp", "float",
         _d(SCALP, "max_age_minutes"), 30, 1440, "min"),
    _num("scalp.min_mcap_usd", "Scalp min mcap", "Scalp", "int",
         _d(SCALP, "min_mcap_usd", int), 5_000, 5_000_000, "USD"),
    _num("scalp.max_mcap_usd", "Scalp max mcap (cap)", "Scalp", "int",
         _d(SCALP, "max_mcap_usd", int), 20_000, 50_000_000, "USD"),
    _num("scalp.min_liquidity_usd", "Scalp min liquidity", "Scalp", "int",
         _d(SCALP, "min_liquidity_usd", int), 1_000, 500_000, "USD"),
    _num("scalp.max_liquidity_usd", "Scalp max liquidity", "Scalp", "int",
         _d(SCALP, "max_liquidity_usd", int), 2_000, 5_000_000, "USD",
         "pools deeper than this have no 5m move left to scalp"),
    _num("scalp.min_volume_m5_usd", "Scalp min 5m volume", "Scalp", "int",
         _d(SCALP, "min_volume_m5_usd", int), 0, 1_000_000, "USD"),
    _num("scalp.stop_pct", "Scalp stop", "Scalp", "pct", _d(SCALP, "stop_pct"), 0.01, 0.30),
    _num("scalp.tp1_at", "Scalp TP1", "Scalp", "float", _d(SCALP, "tp1_at"), 1.01, 3.0, "x"),
    _num("scalp.tp1_frac", "Scalp TP1 sell share", "Scalp", "pct", _d(SCALP, "tp1_frac"), 0.10, 1.0),
    _num("scalp.tp2_at", "Scalp TP2", "Scalp", "float", _d(SCALP, "tp2_at"), 1.02, 5.0, "x"),
    _num("scalp.tp2_frac", "Scalp TP2 sell share", "Scalp", "pct", _d(SCALP, "tp2_frac"), 0.0, 1.0),
    _num("scalp.trail_pct", "Scalp trailing stop", "Scalp", "pct", _d(SCALP, "trail_pct"), 0.005, 0.30),
]

# ---------- Swing band: the hour-to-day lane's own gates ----------
# These drive config.SWING. They are the swing lane's answer to the same four
# questions the Scalp block asks: how old, how deep, how big, how active. Before
# this block existed, swing silently reused SCAN -- so it had no independent
# opinion and the two lanes could never be tuned apart.
_R += [
    Setting("swing.enabled", "Swing lane", "Swing", "bool",
            lambda: bool(SWING.get("enabled", True)),
            lambda v: SWING.__setitem__("enabled", bool(v)),
            help="ON: coins outside the scalp age window are gated by the SWING numbers below. "
                 "OFF: they fall back to the plain SCAN gate."),
    _num("swing.min_age_minutes", "Swing min age", "Swing", "float",
         _d(SWING, "min_age_minutes"), 0, 1440, "min",
         "younger than this is not a swing yet"),
    _num("swing.max_age_hours", "Swing max age", "Swing", "float",
         _d(SWING, "max_age_hours"), 1, 168, "h",
         "older than this is not a swing any more (the established lane can still extend it)"),
    _num("swing.min_liquidity_usd", "Swing min liquidity", "Swing", "int",
         _d(SWING, "min_liquidity_usd", int), 1_000, 500_000, "USD",
         "an hour-scale hold needs real depth to exit into"),
    _num("swing.max_liquidity_usd", "Swing max liquidity", "Swing", "int",
         _d(SWING, "max_liquidity_usd", int), 0, 5_000_000, "USD",
         "0 = no ceiling. Deep pools are good on a swing; the cap exists only if you want one"),
    _num("swing.min_mcap_usd", "Swing min mcap", "Swing", "int",
         _d(SWING, "min_mcap_usd", int), 5_000, 5_000_000, "USD",
         "the journal's sub-100k bleed is why this floor is 100k and not 10k"),
    _num("swing.max_mcap_usd", "Swing max mcap", "Swing", "int",
         _d(SWING, "max_mcap_usd", int), 20_000, 1_000_000_000, "USD"),
    _num("swing.min_volume_h1_usd", "Swing min 1h volume", "Swing", "int",
         _d(SWING, "min_volume_h1_usd", int), 0, 1_000_000, "USD",
         "an hour-scale hold is judged on hourly volume, not on a 5m print"),
    _num("swing.min_trades_h1", "Swing min 1h trades", "Swing", "int",
         _d(SWING, "min_trades_h1", int), 0, 5_000, "trades",
         "too few trades and the exit is you against one bot"),
]

# REGISTRY is built above (line ~364) from _R *before* this scalp block was
# appended, so without this the whole scalp.* surface -- and therefore the
# `hyper_scalp` preset -- was silently dropped as "unknown setting" by
# resolve(). Fold the late additions in, with the same exclusion guard.
for _s in _R:
    if _s.key in REGISTRY:
        continue
    assert not _EXCLUDED_RE.search(_s.key), _s.key
    REGISTRY[_s.key] = _s
del _s

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
    "swing": {"label": "Swing mode", "reason": "Swing only: 1h-3d old, 100k-2M mcap, 15k+ liquidity, 10k+ 1h volume, wider stop, 4h max hold.",
              "values": {"trading.style": "swing", "scan.max_age_hours": 72,
                         "exits.tp_plan": "profile", "exits.ladder": "patient",
                         "scalp.enabled": False, "swing.enabled": True,
                         "swing.min_age_minutes": 60, "swing.max_age_hours": 72,
                         "swing.min_liquidity_usd": 15000, "swing.max_liquidity_usd": 0,
                         "swing.min_mcap_usd": 100000, "swing.max_mcap_usd": 2000000,
                         "swing.min_volume_h1_usd": 10000, "swing.min_trades_h1": 40,
                         "exits.swing.stop_pct": 0.30, "exits.swing.tp1_at": 1.5, "exits.swing.tp2_at": 2.5,
                         "exits.swing.trail_pct": 0.30, "exits.swing.timeout_hours": 3,
                         "exits.swing.max_hold_hours": 4}},
    "hyper_scalp": {"label": "Hyper Scalp (1-10 min)", "reason": "Fresh launches only (1-60 min, 10k-100k mcap, 5k-20k liquidity), moving 5m candle, in and out inside 10 minutes.",
                    "values": {"trading.style": "scalp", "scan.max_age_hours": 1,
                               "exits.tp_plan": "profile", "meme.pullback_entry": False,
                               "swing.enabled": False,
                               "scalp.enabled": True, "scalp.max_hold_minutes": 10, "scalp.timeout_minutes": 5,
                               "scalp.timeout_min_gain": 0.01, "scalp.entry_style": "moving_candle",
                               "scalp.moving_candle_bars": 1, "scalp.moving_candle_min_body_pct": 0.003,
                               "scalp.min_age_minutes": 1, "scalp.max_age_minutes": 60,
                               "scalp.min_mcap_usd": 10000, "scalp.max_mcap_usd": 100000,
                               "scalp.min_liquidity_usd": 5000, "scalp.max_liquidity_usd": 20000,
                               "scalp.min_volume_m5_usd": 500,
                               "scalp.stop_pct": 0.05, "scalp.tp1_at": 1.04, "scalp.tp1_frac": 0.5,
                               "scalp.tp2_at": 1.08, "scalp.tp2_frac": 0.5, "scalp.trail_pct": 0.03,
                               "agent.cycle_seconds": 20}},
    "legend_scalp": {
        "label": "Legend Scalper (always trades)",
        "reason": "Every entry gate at its floor: both lanes on, 0 min age, 5k-50M mcap, "
                  "1k-5M liquidity, no volume floor, signal/meme/analyst gates relaxed. "
                  "The desk always has candidates. Expect a low win rate and many small losses.",
        "values": {
            # ---- style: both lanes, fastest loop ----
            "trading.style": "both", "agent.cycle_seconds": 20,
            "scan.max_age_hours": 168, "scan.min_age_minutes": 0,
            "scan.min_liquidity_usd": 2000, "scan.min_volume_h1_usd": 1000,
            "scan.min_mcap_usd": 5000, "scan.max_mcap_usd": 50000000,
            "scan.min_trades_h1": 5, "scan.max_h1_change_pct": 2000.0,
            "scan.min_sells_ratio_h1": 0.05,
            # ---- the gates that actually starve entries ----
            "meme.pullback_entry": False,          # do not wait for a 20% dip
            "meme.min_holders": 20, "meme.min_community_score": 0,
            "analyst.valid_score": 3, "analyst.max_red_flags": 5,
            "signal.min_confidence": 30, "signal.min_win_probability": 0.30,
            "signal.require_bullish": False,
            "entry.max_price_gap_pct": 50.0, "entry.min_confirm_liq_ratio": 0.2,
            # ---- scalp band: wide, short clock ----
            "scalp.enabled": True, "scalp.max_hold_minutes": 15, "scalp.timeout_minutes": 8,
            "scalp.min_age_minutes": 0, "scalp.max_age_minutes": 1440,
            "scalp.min_mcap_usd": 5000, "scalp.max_mcap_usd": 50000000,
            "scalp.min_liquidity_usd": 1000, "scalp.max_liquidity_usd": 5000000,
            "scalp.min_volume_m5_usd": 0,
            "scalp.stop_pct": 0.08, "scalp.tp1_at": 1.05, "scalp.tp1_frac": 0.5,
            "scalp.tp2_at": 1.12, "scalp.tp2_frac": 0.5, "scalp.trail_pct": 0.04,
            # ---- swing band: wide, hour clock ----
            "swing.enabled": True, "swing.min_age_minutes": 0, "swing.max_age_hours": 168,
            "swing.min_liquidity_usd": 2000, "swing.max_liquidity_usd": 0,
            "swing.min_mcap_usd": 5000, "swing.max_mcap_usd": 50000000,
            "swing.min_volume_h1_usd": 1000, "swing.min_trades_h1": 5,
            # ---- concurrency: more slots, do not halt after a bad day ----
            "risk.max_open_positions": 10, "risk.max_daily_loss_pct": 0.10,
            "risk.auto_continue_after_daily_stop": True,
        }},
    "guide_scalp": {
        "label": "Guide Scalp (BONKbot playbook, cost-corrected)",
        "reason": "The BONKbot scalping guide's PRINCIPLES with its NUMBERS corrected "
                  "for Solana. The guide is written for CEXs (it names Binance, "
                  "Coinbase, OKX) where a 0.5-1% target works because the fee is "
                  "~0.1%. On a Solana DEX the round trip costs ~0.5% in fees and "
                  "real AMM impact, so a 1% target leaves nothing and a 0.5% target "
                  "is a guaranteed loss: backtesting the guide's literal numbers on "
                  "4,200 real entries gave a 0.0% win rate, because every 'win' was "
                  "smaller than the cost of taking it. So this preset keeps the "
                  "guide's filters (deep liquidity, real volume, no 800%-in-3h "
                  "pumps, trend confirmation) and rebuilds the exit geometry so a "
                  "win is worth more than the toll: TP1 at 1.30 and TP2 at 1.40, "
                  "both far above cost, with a 20% stop and a full 60-minute hold. "
                  "The guide's 'get out fast for a small gain' is the one piece of "
                  "its advice that is backwards on Solana -- a fast exit only "
                  "guarantees you pay the toll without covering it. Even so: on the "
                  "4200-entry replay this preset is the best of every geometry "
                  "tested and still nets about -0.03% per trade in a $40k pool, so "
                  "treat it as the least-bad configuration, not a money printer. "
                  "It cannot be profitable until the paper slippage assumption "
                  "(2%/side) is replaced with real AMM impact.",
        "values": {
            # ---- guide step 1: liquidity and volatility, not hype ----
            "trading.style": "both", "agent.cycle_seconds": 20,
            "scan.min_age_minutes": 5, "scan.max_age_hours": 48,
            "scan.min_liquidity_usd": 15000, "scan.min_volume_h1_usd": 5000,
            "scan.min_mcap_usd": 20000, "scan.max_mcap_usd": 50000000,
            "scan.min_trades_h1": 40,
            # guide: "if a coin has gone up 800% in 3 hours, leave it alone"
            "scan.max_h1_change_pct": 300.0,
            "scan.min_sells_ratio_h1": 0.10,
            "meme.pullback_entry": False,
            "meme.min_holders": 20, "meme.min_community_score": 0,
            "analyst.valid_score": 6, "analyst.max_red_flags": 3,
            # ---- guide step 2/3: volume + trend confirmation ----
            "signal.min_confidence": 45, "signal.min_win_probability": 0.35,
            "signal.require_bullish": True,
            "entry.max_price_gap_pct": 20.0, "entry.min_confirm_liq_ratio": 0.3,
            # ---- scalp band: deep enough to exit, tight spread ----
            "scalp.enabled": True, "scalp.max_hold_minutes": 60, "scalp.timeout_minutes": 60,
            "scalp.timeout_min_gain": 0.02,
            "scalp.min_age_minutes": 5, "scalp.max_age_minutes": 1440,
            "scalp.min_mcap_usd": 20000, "scalp.max_mcap_usd": 50000000,
            "scalp.min_liquidity_usd": 15000, "scalp.max_liquidity_usd": 5000000,
            "scalp.min_volume_m5_usd": 500,
            # ---- exit geometry: a win must clear the round-trip cost ----
            "scalp.stop_pct": 0.20, "scalp.tp1_at": 1.30, "scalp.tp1_frac": 0.5,
            "scalp.tp2_at": 1.40, "scalp.tp2_frac": 0.5, "scalp.trail_pct": 0.05,
            # ---- swing band: same filters, hour clock ----
            "swing.enabled": True, "swing.min_age_minutes": 5, "swing.max_age_hours": 48,
            "swing.min_liquidity_usd": 15000, "swing.max_liquidity_usd": 0,
            "swing.min_mcap_usd": 20000, "swing.max_mcap_usd": 50000000,
            "swing.min_volume_h1_usd": 5000, "swing.min_trades_h1": 40,
            "risk.max_open_positions": 10, "risk.max_daily_loss_pct": 0.10,
            "risk.auto_continue_after_daily_stop": True,
        }},
    "ultra_scalp": {
        "label": "Ultra Scalper (on-chain spec: 15m-6h, $30k LP, +9%/+15%)",
        "reason": "The operator's own on-chain spec, written for Solana memes instead of "
                  "CEX majors. The generic article it replaces says 'take 0.5-1%' and "
                  "'a few million in daily volume' -- on-chain that is a guaranteed loss, "
                  "because fees, priority fees and AMM impact take 1-3% per round trip, so "
                  "a 1% target is already underwater before the trade starts. This preset "
                  "uses the numbers that survive that cost: a 7% stop against a blended "
                  "1.12x target, which is about 1:1.7 risk-to-reward, and targets of +9% "
                  "and +15% that clear a 1-3% round trip with room left over. "
                  "Entries are restricted to the window where a meme is actually tradable: "
                  "15 minutes old at the floor (younger is sniper and rug territory), 6 "
                  "hours at the ceiling (older is usually past the momentum), $30k of "
                  "liquidity so an exit exists at all, $100k-$3M of market cap so a move "
                  "can still happen, and 300 holders so the float is not one wallet. "
                  "It refuses anything already up 300% inside an hour, and it caps the "
                  "buy share at 60% by requiring at least 40% sells -- a token nobody can "
                  "sell is a honeypot, and a token everybody is buying into a vertical "
                  "candle is the top. "
                  "The clock is deliberately brutal: if the trade has not moved in 5 "
                  "minutes it is closed, because a scalper's edge is turnover, not "
                  "patience. The trail is 5%, the volume-decay exit fires when 6h volume "
                  "falls under half of its 24h pace, and three losses in a row pause new "
                  "entries for an hour while two losses in a row halve the size. "
                  "KNOWN GAPS -- these parts of the spec are not settings, they need code: "
                  "(1) the 9 EMA on the 1m chart is not wired into the entry path, so "
                  "'price above the 9 EMA' is not enforced; (2) 'volume at least 2x the "
                  "10-candle average' has no setting, the closest is the moving-candle "
                  "body filter; (3) top-10 holder concentration under 25-30% and dev "
                  "under 5% have no setting; (4) mint/freeze authority revocation and "
                  "bundled-supply detection have no setting; (5) the 5% trail cannot be "
                  "armed only after +8% -- it arms at TP1; (6) priority fee and tip are "
                  "not exposed as settings; (7) paper.slippage_pct is capped at 0.10, so "
                  "the spec's 10-15% exit slippage is expressed at its floor. "
                  "SIZING WARNING: 2% of the book per trade is the spec's number, but the "
                  "desk's live minimum ticket is $10, so a 2% position needs a $500 book. "
                  "Below that the live desk cannot size a trade at all and will sit idle "
                  "while looking healthy. On a $46 book this preset is paper-only.",
        "values": {
            # ---- cadence: this is a scalper, it must look often ----
            "trading.style": "scalp", "agent.cycle_seconds": 20,
            # ---- entry filters: the tradable window of a meme's life ----
            "scan.min_age_minutes": 15, "scan.max_age_hours": 6,
            "scan.min_liquidity_usd": 30000,
            "scan.min_volume_h1_usd": 50000,
            "scan.min_mcap_usd": 100000, "scan.max_mcap_usd": 3000000,
            "scan.min_trades_h1": 100,
            "scan.max_h1_change_pct": 300.0,
            # 40% sells is the closest the desk gets to "buys at most 60%":
            # it rejects both the honeypot (no sells) and the vertical top.
            "scan.min_sells_ratio_h1": 0.40,
            # ---- float quality ----
            "meme.min_holders": 300, "meme.min_community_score": 0,
            "meme.pullback_entry": True,
            # ---- quality gate: safety-first, so fewer red flags tolerated ----
            "analyst.valid_score": 6, "analyst.max_red_flags": 2,
            "signal.min_confidence": 50, "signal.min_win_probability": 0.40,
            "signal.require_bullish": True,
            # ---- entry trigger: green and rising, never chasing ----
            "entry.max_price_gap_pct": 20.0, "entry.min_confirm_liq_ratio": 0.5,
            "scalp.enabled": True,
            "scalp.entry_style": "moving_candle",
            "scalp.moving_candle_bars": 1,
            "scalp.moving_candle_min_body_pct": 0.005,
            # ---- scalp band: same window, minute clock ----
            "scalp.min_age_minutes": 15, "scalp.max_age_minutes": 360,
            "scalp.min_mcap_usd": 100000, "scalp.max_mcap_usd": 3000000,
            "scalp.min_liquidity_usd": 30000, "scalp.max_liquidity_usd": 3000000,
            "scalp.min_volume_m5_usd": 10000,
            # ---- exit geometry: 7% risk, +9%/+15% reward, 5m time stop ----
            "scalp.stop_pct": 0.07,
            "scalp.tp1_at": 1.09, "scalp.tp1_frac": 0.5,
            "scalp.tp2_at": 1.15, "scalp.tp2_frac": 0.5,
            "scalp.trail_pct": 0.05,
            "scalp.timeout_minutes": 5, "scalp.timeout_min_gain": 0.0,
            "scalp.max_hold_minutes": 60,
            # ---- emergency exit: volume halved ----
            "exits.volume_decay_ratio": 0.5, "exits.ladder": "fast",
            # ---- no swing band: this desk only scalps ----
            "swing.enabled": False,
            # ---- sizing: 2% per trade, 3 losses pause the day ----
            "risk.max_position_pct": 0.02, "risk.max_risk_per_trade": 0.015,
            "risk.max_open_positions": 10, "risk.max_daily_loss_pct": 0.05,
            "risk.auto_continue_after_daily_stop": False,
            "breaker.streak_pause": 3, "breaker.streak_reduce": 2,
            "breaker.cooldown_minutes": 60,
            # ---- cost assumption: the spec's exit slippage, at its floor ----
            "paper.slippage_pct": 0.10,
        }},
    "super_legend_scalp": {
        "label": "Super Legend Scalper (floor of every floor)",
        "reason": "Legend Scalper with every remaining limit pushed to its registry "
                  "floor: 0 min age, 5k-1B mcap, 1k+ liquidity, zero volume and "
                  "zero trade floors, no bullish requirement, 30% scalp stop, "
                  "60 min holds, all 10 slots. This is the loosest the desk can be "
                  "configured to run. It will trade almost anything that is not a "
                  "honeypot. Expect a very low win rate and many small losses.",
        "values": {
            # ---- style: both lanes, fastest loop ----
            "trading.style": "both", "agent.cycle_seconds": 20,
            # ---- scan: every floor, every ceiling ----
            "scan.min_age_minutes": 0, "scan.max_age_hours": 168,
            "scan.min_liquidity_usd": 2000, "scan.min_volume_h1_usd": 1000,
            "scan.min_mcap_usd": 5000, "scan.max_mcap_usd": 1000000000,
            "scan.min_trades_h1": 5, "scan.max_h1_change_pct": 2000.0,
            "scan.min_sells_ratio_h1": 0.05,
            # ---- the gates that actually starve entries ----
            "meme.pullback_entry": False,
            "meme.min_holders": 20, "meme.min_community_score": 0,
            "analyst.valid_score": 3, "analyst.max_red_flags": 5,
            "signal.min_confidence": 30, "signal.min_win_probability": 0.30,
            "signal.require_bullish": False,
            "entry.max_price_gap_pct": 50.0, "entry.min_confirm_liq_ratio": 0.2,
            # ---- scalp band: widest stop, longest clock the lane allows ----
            "scalp.enabled": True, "scalp.max_hold_minutes": 60, "scalp.timeout_minutes": 30,
            "scalp.min_age_minutes": 0, "scalp.max_age_minutes": 1440,
            "scalp.min_mcap_usd": 5000, "scalp.max_mcap_usd": 50000000,
            "scalp.min_liquidity_usd": 1000, "scalp.max_liquidity_usd": 5000000,
            "scalp.min_volume_m5_usd": 0,
            # 30% stop is the widest the registry allows, and the ladder replay
            # showed win rate is set by stop width, not by TP geometry.
            "scalp.stop_pct": 0.30, "scalp.tp1_at": 1.05, "scalp.tp1_frac": 0.5,
            "scalp.tp2_at": 1.15, "scalp.tp2_frac": 0.5, "scalp.trail_pct": 0.05,
            # ---- swing band: zero floors ----
            "swing.enabled": True, "swing.min_age_minutes": 0, "swing.max_age_hours": 168,
            "swing.min_liquidity_usd": 1000, "swing.max_liquidity_usd": 0,
            "swing.min_mcap_usd": 5000, "swing.max_mcap_usd": 1000000000,
            "swing.min_volume_h1_usd": 0, "swing.min_trades_h1": 0,
            # ---- concurrency ----
            "risk.max_open_positions": 10, "risk.max_daily_loss_pct": 0.10,
            "risk.auto_continue_after_daily_stop": True,
        }},
    "super_legend_v2": {
        "label": "Super Legend v2 (loose entries, tight exits)",
        "reason": "Super Legend's entry gates -- the loose ones are what produced "
                  "the 64% win rate -- with the exit geometry rebuilt. The 30% stop "
                  "was the defect: it capped winners at +21% while letting losers "
                  "run to -30%, a payoff of 0.69 that needs a 59% win rate just to "
                  "break even before fees. Here the stop is 12%, TP2 is 1.30 so a "
                  "runner can pay for two stops, the hold is 25 min instead of 60, "
                  "and the dead-token floors (0 volume, 0 trades, $1B mcap) are "
                  "restored because those are what turn a 12% stop into a 30% gap.",
        "values": {
            # ---- style: both lanes, fastest loop ----
            "trading.style": "both", "agent.cycle_seconds": 20,
            # ---- scan: still loose, but not into the void ----
            "scan.min_age_minutes": 0, "scan.max_age_hours": 168,
            "scan.min_liquidity_usd": 2000, "scan.min_volume_h1_usd": 1000,
            "scan.min_mcap_usd": 5000, "scan.max_mcap_usd": 50000000,
            "scan.min_trades_h1": 5, "scan.max_h1_change_pct": 2000.0,
            "scan.min_sells_ratio_h1": 0.05,
            # ---- entry gates: unchanged from super legend ----
            "meme.pullback_entry": False,
            "meme.min_holders": 20, "meme.min_community_score": 0,
            "analyst.valid_score": 3, "analyst.max_red_flags": 5,
            "signal.min_confidence": 30, "signal.min_win_probability": 0.30,
            "signal.require_bullish": False,
            "entry.max_price_gap_pct": 50.0, "entry.min_confirm_liq_ratio": 0.2,
            # ---- scalp band: the actual fix ----
            "scalp.enabled": True, "scalp.max_hold_minutes": 25, "scalp.timeout_minutes": 12,
            "scalp.min_age_minutes": 0, "scalp.max_age_minutes": 1440,
            "scalp.min_mcap_usd": 5000, "scalp.max_mcap_usd": 50000000,
            "scalp.min_liquidity_usd": 1000, "scalp.max_liquidity_usd": 5000000,
            "scalp.min_volume_m5_usd": 0,
            # 12% stop: wide enough to keep most of the 64% win rate, narrow
            # enough that one loss is not three winners.
            "scalp.stop_pct": 0.12, "scalp.tp1_at": 1.08, "scalp.tp1_frac": 0.4,
            "scalp.tp2_at": 1.30, "scalp.tp2_frac": 0.6, "scalp.trail_pct": 0.05,
            # ---- swing band: floors back on ----
            "swing.enabled": True, "swing.min_age_minutes": 0, "swing.max_age_hours": 168,
            "swing.min_liquidity_usd": 2000, "swing.max_liquidity_usd": 0,
            "swing.min_mcap_usd": 5000, "swing.max_mcap_usd": 50000000,
            "swing.min_volume_h1_usd": 1000, "swing.min_trades_h1": 5,
            # ---- concurrency ----
            "risk.max_open_positions": 10, "risk.max_daily_loss_pct": 0.10,
            "risk.auto_continue_after_daily_stop": True,
        }},
    "balanced": {"label": "Both lanes", "reason": "Both at once: fresh launches 1-60 min / 10k-100k / 5k-20k scalp in minutes; 1h-48h / 100k-2M / 15k+ swings for hours.",
                 "values": {"trading.style": "both", "scan.max_age_hours": 48,
                            "exits.tp_plan": "profile", "exits.ladder": "fast",
                            # scalp band
                            "scalp.enabled": True, "scalp.max_hold_minutes": 10, "scalp.timeout_minutes": 5,
                            "scalp.timeout_min_gain": 0.01, "scalp.entry_style": "moving_candle",
                            "scalp.moving_candle_bars": 1, "scalp.moving_candle_min_body_pct": 0.003,
                            "scalp.min_age_minutes": 1, "scalp.max_age_minutes": 60,
                            "scalp.min_mcap_usd": 10000, "scalp.max_mcap_usd": 100000,
                            "scalp.min_liquidity_usd": 5000, "scalp.max_liquidity_usd": 20000,
                            "scalp.min_volume_m5_usd": 500,
                            "scalp.stop_pct": 0.05, "scalp.tp1_at": 1.04, "scalp.tp1_frac": 0.5,
                            "scalp.tp2_at": 1.08, "scalp.tp2_frac": 0.5, "scalp.trail_pct": 0.03,
                            # swing band
                            "swing.enabled": True, "swing.min_age_minutes": 60, "swing.max_age_hours": 48,
                            "swing.min_liquidity_usd": 15000, "swing.max_liquidity_usd": 0,
                            "swing.min_mcap_usd": 100000, "swing.max_mcap_usd": 2000000,
                            "swing.min_volume_h1_usd": 10000, "swing.min_trades_h1": 40,
                            # legacy scalp profile, restored so this card is a true
                            # "back to default" (the scalp.* block above overlays it
                            # while the fast lane is on)
                            "exits.scalp_max_age_hours": 6.0,
                            "exits.scalp.stop_pct": 0.20, "exits.scalp.tp1_at": 1.5, "exits.scalp.tp2_at": 2.0,
                            "exits.scalp.tp1_frac": 0.33, "exits.scalp.tp2_frac": 0.5,
                            "exits.scalp.trail_pct": 0.20, "exits.scalp.timeout_hours": 3.0,
                            "exits.scalp.max_hold_hours": 4.0,
                            # swing exits
                            "exits.swing.stop_pct": 0.30, "exits.swing.tp1_at": 1.5, "exits.swing.tp2_at": 2.0,
                            "exits.swing.trail_pct": 0.30, "exits.swing.timeout_hours": 3.0,
                            "exits.swing.max_hold_hours": 4.0}},
    # Built from the ChartingLens scalping rules, keeping only what holds up on meme coins: liquid pools,
    # trade only the pullback, ~1.5-2R target (fast ladder 1.25x/1.6x on a 30% stop), risk <= 1% per
    # trade, hard daily stop. Stock/forex triggers (EMA/VWAP) are NOT used.
    "strategy": {"label": "Strategy (scalp rules)",
                 "reason": "Liquid pools only, pullback entry, quick 1.5-2R exits, 1% risk per trade, hard daily stop.",
                 # Self-contained: it also resets every gate a looser preset (legend_scalp etc.) leaves
                 # behind, including the swing lane's own gates and the fast scalp lane switch.
                 "values": {"scalp.enabled": False, "swing.enabled": True,
                            "scan.min_liquidity_usd": 50_000, "scan.min_volume_h1_usd": 20_000,
                            "scan.min_trades_h1": 100, "scan.min_mcap_usd": 100_000,
                            "scan.max_mcap_usd": 2_000_000, "scan.min_age_minutes": 120,
                            "scan.max_age_hours": 48, "scan.max_h1_change_pct": 250.0,
                            "scan.min_sells_ratio_h1": 0.1,
                            "swing.min_liquidity_usd": 50_000, "swing.min_volume_h1_usd": 20_000,
                            "swing.min_trades_h1": 100, "swing.min_mcap_usd": 100_000,
                            "swing.max_mcap_usd": 2_000_000, "swing.min_age_minutes": 120,
                            "swing.max_age_hours": 48,
                            "analyst.valid_score": 6, "analyst.max_red_flags": 3,
                            "signal.min_confidence": 60, "signal.min_win_probability": 0.52,
                            "signal.require_bullish": True,
                            "meme.min_holders": 150, "meme.min_community_score": 35,
                            "entry.max_price_gap_pct": 25.0, "entry.min_confirm_liq_ratio": 0.5,
                            "meme.pullback_entry": True,
                            "exits.tp_plan": "profile", "exits.ladder": "fast",
                            "risk.max_risk_per_trade": 0.01, "risk.max_daily_loss_pct": 0.03,
                            "risk.max_open_positions": 6, "risk.auto_continue_after_daily_stop": False,
                            "agent.cycle_seconds": 30}},
}


# ---- 2026-11 fix: every scalper preset was losing (0% wins on the dashboard) ----------------
# What they shared, and what the repo's own tests say about it:
#   * the fast scalp lane ON: its 5-12% stop sits inside normal meme-coin noise, and its +4..+15%
#     targets are eaten by ~2% simulated slippage + fees on each side; the lane also OVERRIDES the
#     ladder, so even the presets that turned the dip entry on exited at the lane's tiny stop;
#   * no dip entry: a plain BUY signal won 15.4% of trades (profit factor 0.50);
#   * floors at $1-5k liquidity, so thin pools and rugs.
# The only entry that cleared in the tests is the 20% dip with a 30% stop on liquid pools, which is
# exactly the `strategy` preset. So every scalper preset is now `strategy` plus its own exit ladder;
# the ladder is the one real trade-off left (same win rate, patient earns more per trade).
_FIXED_SCALPERS = {
    "scalper": "fast", "hyper_scalp": "single", "legend_scalp": "fast", "guide_scalp": "fast",
    "ultra_scalp": "fast", "super_legend_scalp": "patient", "super_legend_v2": "patient",
}


def _stricter(key: str, own, core):
    """Merge one value: a preset's own protection survives when it is already stricter.

    min_* / valid_score: higher is stricter (a 300-holder floor must not drop to 150);
    max_*: lower is stricter (a 6h age cap must not widen to 48h). Switches and choices
    (lane off, dip entry on, bullish required) take the core value.
    """
    num = (int, float)
    if isinstance(own, bool) or isinstance(core, bool) or not (isinstance(own, num) and isinstance(core, num)):
        return core
    if "max_" in key:
        return min(own, core)
    if "min_" in key or key.endswith("valid_score"):
        return max(own, core)
    return core


for _name, _ladder in _FIXED_SCALPERS.items():
    _p = PRESETS[_name]
    _own = _p["values"]
    _p["values"] = {**_own, **{k: _stricter(k, _own[k], v) if k in _own else v
                               for k, v in PRESETS["strategy"]["values"].items()},
                    "exits.ladder": _ladder}
    # The dip needs ~100 min of candles, so min age is 120 min. A preset whose own age cap was
    # shorter (Hyper Scalp: 1h) would leave an impossible window; widen the cap to the core's.
    for _k in ("scan.max_age_hours", "swing.max_age_hours"):
        if _p["values"][_k] * 60 <= _p["values"]["scan.min_age_minutes"] + 60:
            _p["values"][_k] = PRESETS["strategy"]["values"][_k]
    _p["label"] = _p["label"].split(" (")[0] + " (fixed: dip entry, 30% stop)"
    _p["reason"] = ("FIXED. Liquid pools only, 20% dip entry, 30% stop, " + _ladder + " exits, "
                    "fast scalp lane off, 1% risk, 6 slots, hard 3% daily stop. "
                    "Was: " + _p["reason"][:140])
del _name, _ladder, _p, _own, _k


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
