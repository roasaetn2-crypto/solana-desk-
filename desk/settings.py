"""Operator-confirmed PAPER settings: registry, proposals, confirm / reject.

Desk AI (or a preset button) may only PROPOSE a change. Nothing is applied until the operator
taps Confirm, which re-validates every value against the registry bounds below, applies it to
the running paper config, persists it (local file + Turso kv when configured) so it survives a
redeploy, and journals it as a "settings_change" row.

Never proposable or changeable through this path (not in the registry, and refused by name):
  * desk.mode / execution / anything live (EXECUTION, live caps, the confirm phrase)
  * the scam and dev kills (CHAIN mint/freeze authority, top wallet, top 10; MEME dev hold,
    sniper share, creator prior rugs; ENTRY.require_confirm_quote)
  * keys, tokens, secrets, SOLANA_PRIVATE_KEY, and the paper bankroll (starting equity)
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import config
from config import AGENT, AUTOTUNE, ENTRY, EXITS, MEME, PAPER, RISK, SCAN
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
    _num("risk.max_daily_loss_pct", "Daily loss stop", "Risk", "pct", _d(RISK, "max_daily_loss_pct"), 0.005, 0.10, "of book",
         "the desk halts new entries for the day past this; capped at 10%"),
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
    _num("paper.min_ticket_usd", "Min paper ticket", "Risk", "float", _d(PAPER, "min_ticket_usd"), 0.5, 10, "USD"),
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
    # loop / self-tune
    _num("agent.cycle_seconds", "Scan cycle", "Loop", "int", _d(AGENT, "cycle_seconds", int), 20, 300, "s"),
    Setting("autotune.enabled", "Self-tune entry gates", "Loop", "bool", lambda: bool(AUTOTUNE["enabled"]),
            lambda v: AUTOTUNE.__setitem__("enabled", bool(v))),
    _num("autotune.interval_minutes", "Self-tune interval", "Loop", "int", _d(AUTOTUNE, "interval_minutes", int), 10, 240, "min"),
    _num("autotune.idle_loosen_hours", "Self-tune: loosen after idle", "Loop", "float", _d(AUTOTUNE, "idle_loosen_hours"), 0.5, 24, "h"),
]
REGISTRY: dict[str, Setting] = {s.key: s for s in _R}
for _k in REGISTRY:  # belt and braces: nothing excluded may sneak into the registry
    assert not _EXCLUDED_RE.search(_k), _k

_ALIASES = {"stop_loss": "exits.scalp.stop_pct", "stop": "exits.scalp.stop_pct", "risk": "risk.max_risk_per_trade",
            "style": "trading.style", "min_score": "analyst.valid_score", "max_positions": "risk.max_open_positions",
            "daily_loss": "risk.max_daily_loss_pct", "trailing_stop": "exits.scalp.trail_pct"}

# ---------- presets: one card, every value that changes ----------
PRESETS: dict[str, dict] = {
    "scalper": {"label": "Scalper mode", "reason": "Scalp only: young coins, tight stop, quick take profit, short clock.",
                "values": {"trading.style": "scalp", "scan.max_age_hours": 6, "exits.scalp_max_age_hours": 6,
                           "exits.scalp.stop_pct": 0.15, "exits.scalp.tp1_at": 1.3, "exits.scalp.tp2_at": 1.8,
                           "exits.scalp.trail_pct": 0.15, "exits.scalp.timeout_hours": 1.5,
                           "exits.scalp.max_hold_hours": 4, "agent.cycle_seconds": 30}},
    "swing": {"label": "Swing mode", "reason": "Swing only: coins older than the scalp window, wider stop, 4h max hold.",
              "values": {"trading.style": "swing", "scan.max_age_hours": 72, "exits.scalp_max_age_hours": 6,
                         "exits.swing.stop_pct": 0.30, "exits.swing.tp1_at": 1.5, "exits.swing.tp2_at": 2.5,
                         "exits.swing.trail_pct": 0.30, "exits.swing.timeout_hours": 3,
                         "exits.swing.max_hold_hours": 4}},
    "balanced": {"label": "Both (desk default)", "reason": "Back to the desk's default mix: scalp young coins, swing older ones.",
                 "values": {"trading.style": "both", "scan.max_age_hours": 48, "exits.scalp_max_age_hours": 6.0,
                            "exits.scalp.stop_pct": 0.20, "exits.scalp.tp1_at": 1.5, "exits.scalp.tp2_at": 2.0,
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


def resolve(key: Any) -> Setting:
    k = str(key or "").strip()
    if not k:
        raise Rejected("no setting named")
    if _EXCLUDED_RE.search(k) or k.lower() in ("mode", "desk.mode"):
        raise Rejected(f"'{k[:60]}' is excluded: mode, live, kills, keys and the bankroll can't be changed here")
    s = REGISTRY.get(k) or REGISTRY.get(_ALIASES.get(k.lower(), ""))
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
    if vals["breaker.reduce_day_loss_pct"] > vals["risk.max_daily_loss_pct"]:
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


def current_values() -> dict[str, Any]:
    return {k: s.get() for k, s in REGISTRY.items()}


def validate_changes(raw: list[dict]) -> tuple[list[dict], list[dict]]:
    """raw: [{setting, proposed, reason?}]. Returns (valid changes, dropped with reasons).
    Each valid change: {setting, label, current, proposed, note?}. No-ops are dropped."""
    ok, dropped, seen = [], [], set()
    for r in (raw or [])[:MAX_CHANGES * 2]:
        if not isinstance(r, dict):
            continue
        key = r.get("setting") or r.get("key")
        try:
            s = resolve(key)
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
        vals = current_values()
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
            s = resolve(k)
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
           preset: str | None = None) -> dict:
    """Validate and store a PENDING proposal. Never applies anything."""
    changes, dropped = validate_changes(changes_raw)
    if not changes:
        return {"ok": False, "error": "nothing valid to propose", "dropped": dropped}
    now = _now()
    p = {"id": uuid.uuid4().hex[:12], "status": "pending", "source": str(source)[:20],
         "title": _clean(title, 60), "preset": preset, "reason": _clean(reason, 300),
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


def create_values(values: dict, title: str = "Operator change", source: str = "operator") -> dict:
    """Operator-made proposal from explicit {key: value} pairs (no AI needed).

    Still goes through validate_changes and still needs Confirm, so the audit
    trail and the bounds are the same as an AI recommendation.
    """
    if not isinstance(values, dict) or not values:
        return {"ok": False, "error": "no values given"}
    raw = [{"setting": k, "proposed": v} for k, v in list(values.items())[:MAX_CHANGES]]
    out = create(raw, "operator set " + ", ".join(sorted(values)[:4]), source, title)
    if not out["ok"] and not out.get("dropped"):
        out["error"] = "those values are already what the desk runs"
    return out


def _clean(s, n):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f<>`]", " ", str(s or ""))).strip()[:n]


def public(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "status", "source", "title", "preset", "reason", "changes", "dropped",
                                   "created_at", "expires", "decided_at", "decided_by", "applied", "error")}


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
        raw = [{"setting": c["setting"], "proposed": c["proposed"]} for c in p["changes"]]
        changes, dropped = validate_changes(raw)
        if not changes or len(changes) != len(raw) or any(c.get("note") for c in changes):
            p["status"], p["decided_at"], p["decided_by"] = "invalid", _iso(now), _clean(who, 40)
            p["error"] = "; ".join(d["why"] for d in dropped)[:300] or "values no longer valid or already set"
            _save_proposals()
            return {"ok": False, "code": 409, "error": p["error"], "proposal": public(p)}
        applied = []
        for c in changes:
            s = REGISTRY[c["setting"]]
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
            title=p.get("title"), by=_clean(who, 40), mode="paper",
            changes=applied or [{"setting": c["setting"], "from": c["current"], "to": c["proposed"]}
                                for c in p["changes"]])
    if action == "confirm":
        print(f"[desk] settings: {who} confirmed {p['id']}: "
              + ", ".join(f"{a['setting']} {a['from']} -> {a['to']}" for a in applied))
    else:
        print(f"[desk] settings: {who} rejected {p['id']}")


def snapshot() -> dict:
    """GET /api/settings: every proposable setting with value and bounds, plus exclusions."""
    groups: dict[str, list] = {}
    for s in REGISTRY.values():
        groups.setdefault(s.group, []).append(s.describe())
    return {"mode": "paper", "groups": groups,
            "presets": {k: {"label": v["label"], "reason": v["reason"], "values": v["values"]}
                        for k, v in PRESETS.items()},
            "excluded": list(EXCLUDED), "ttl_hours": PROPOSAL_TTL_S // 3600}


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
