"""Self-tune for PAPER entry gates only. Logged, bounded by hard floors, never touches risk.

MERGE (idea from the old desk's autotune): if the desk has been idle (no paper entry) it loosens
the gate that rejects the most, one notch, every interval. If the recent paper win rate is bad
it tightens one notch. It can only move the four gates in config.AUTOTUNE["gates"], and only
inside [loose_end, tight_end]. It never changes stops, size caps, the daily loss stop, the scam
kills (mint/freeze authority, top wallet, dev checks) or live mode. Live is disabled anyway.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import config
from config import AUTOTUNE, CHAIN, SCAN
from desk.ops import data_file, journal

STATE = Path("autotune.json")
LOG = Path("autotune_log.jsonl")
# bench reason -> gate that produced it
REASON_GATE = {"volume": "volume_h1", "trades": "trades_h1", "top_10": "top_10",
               "score": "score", "watch": "score", "skip": "score"}
ORDER = ["score", "volume_h1", "trades_h1", "top_10"]


def get_gate(name: str) -> float:
    return {"score": lambda: config.ANALYST_VALID_SCORE,
            "volume_h1": lambda: SCAN["min_volume_h1_usd"],
            "trades_h1": lambda: SCAN["min_trades_h1"],
            "top_10": lambda: CHAIN["max_top_10"]}[name]()


def set_gate(name: str, v: float) -> None:
    g = AUTOTUNE["gates"][name]
    lo, hi = sorted((g["loose_end"], g["tight_end"]))
    v = min(max(v, lo), hi)                    # hard floors / ceilings, always re-applied
    if name == "score":
        config.ANALYST_VALID_SCORE = config.MIN_SCORE = int(round(v))
    elif name == "volume_h1":
        SCAN["min_volume_h1_usd"] = int(round(v))
    elif name == "trades_h1":
        SCAN["min_trades_h1"] = int(round(v))
    elif name == "top_10":
        CHAIN["max_top_10"] = round(v, 3)


def _toward_loose(name: str) -> int:
    g = AUTOTUNE["gates"][name]
    return 1 if g["loose_end"] > g["tight_end"] else -1


def can_move(name: str, loosen: bool) -> bool:
    g = AUTOTUNE["gates"][name]
    end = g["loose_end"] if loosen else g["tight_end"]
    cur = get_gate(name)
    return cur < end if (end > cur) else cur > end if end < cur else False


def notch(name: str, loosen: bool) -> tuple[float, float]:
    g = AUTOTUNE["gates"][name]
    cur = get_gate(name)
    d = g["step"] * _toward_loose(name) * (1 if loosen else -1)
    set_gate(name, cur + d)
    return cur, get_gate(name)


# The config defaults the saved gates were tuned from. When an approved retune changes a
# default, a gate the self-tuner saved against the OLD default is dropped on restore so the new
# default goes live. Operator-confirmed values (desk/settings.py) are re-applied after this and
# still win. State files written before baselines existed were tuned from these defaults.
DEFAULTS = {n: get_gate(n) for n in AUTOTUNE["gates"]}
LEGACY_BASELINE = {"score": 5, "volume_h1": 8_000, "trades_h1": 60, "top_10": 0.35}


class AutoTune:
    def __init__(self, agent, state_path: Path | str | None = None, log_path: Path | str | None = None):
        self.agent = agent
        self.state_path = Path(state_path) if state_path is not None else data_file("autotune.json")
        self.log_path = Path(log_path) if log_path is not None else data_file("autotune_log.jsonl")
        self.last_run = time.time()
        self.last_change_ts = 0.0
        self.history: list[dict] = []
        self.dropped: dict = {}
        self._restore()

    def _restore(self):
        if not self.state_path.exists():
            return
        try:
            st = json.loads(self.state_path.read_text())
        except Exception:
            return
        base = st.get("baseline") if isinstance(st.get("baseline"), dict) else LEGACY_BASELINE
        self.dropped: dict = {}
        for name, v in (st.get("gates") or {}).items():
            if name not in AUTOTUNE["gates"]:
                continue
            if name in base and name in DEFAULTS and base[name] != DEFAULTS[name]:
                self.dropped[name] = v     # tuned from an old default: the new default stands
                continue
            set_gate(name, v)              # clamped by the hard floors
        if self.dropped:
            print("[desk] autotune: new defaults replace saved self-tuned gate(s) "
                  + ", ".join(f"{n} {v} -> {get_gate(n)}" for n, v in self.dropped.items()), flush=True)
        self.history = st.get("history", [])[-50:]

    def _persist(self):
        try:
            self.state_path.write_text(json.dumps(
                {"gates": {n: get_gate(n) for n in AUTOTUNE["gates"]}, "baseline": DEFAULTS,
                 "history": self.history[-50:]}, indent=1))
        except Exception:
            pass
        try:
            from desk.store import sync_kv_file
            sync_kv_file("settings", self.state_path)
        except Exception:
            pass

    def snapshot(self) -> dict:
        return {"enabled": AUTOTUNE["enabled"],
                "gates": {n: get_gate(n) for n in AUTOTUNE["gates"]},
                "floors": {n: {"loose_end": g["loose_end"], "tight_end": g["tight_end"]}
                           for n, g in AUTOTUNE["gates"].items()},
                "last_changes": self.history[-5:]}

    def _recent_closed(self) -> list[dict]:
        c = [x for x in self.agent.broker.s["closed"] if not str(x.get("mint", "")).startswith("DEMO")]
        return c[-AUTOTUNE["window_trades"]:]

    def tick(self, now: float | None = None) -> dict | None:
        now = now or time.time()
        a = self.agent
        if not AUTOTUNE["enabled"] or a.mode != "paper" or getattr(a, "paused_tune", False):
            return None
        if now - self.last_run < AUTOTUNE["interval_minutes"] * 60:
            return None
        self.last_run = now
        closed = self._recent_closed()
        n = len(closed)
        wins = sum(1 for c in closed if c.get("pnl_usd", 0) > 0)
        wr = wins / n if n else None
        idle_since = max(a.started_at, a.last_entry_ts or 0)
        idle_h = (now - idle_since) / 3600
        action = None
        if n >= AUTOTUNE["min_closed_to_judge"] and wr < AUTOTUNE["tighten_below_win_rate"]:
            # tighten the gate that was loosened most recently, else the first one with room
            cands = [h["gate"] for h in reversed(self.history) if h["dir"] == "loosen"] + ORDER
            gate = next((g for g in cands if can_move(g, False)), None)
            if gate:
                action = (gate, False, f"win rate {wr:.0%} over last {n} closed paper trades")
        elif idle_h >= AUTOTUNE["idle_loosen_hours"] and now - self.last_change_ts >= AUTOTUNE["interval_minutes"] * 60:
            tally = {g: 0 for g in ORDER}
            for reason, cnt in a.reject_reasons.items():
                if reason in REASON_GATE:
                    tally[REASON_GATE[reason]] += cnt
            ranked = sorted(ORDER, key=lambda g: -tally[g])
            gate = next((g for g in ranked if can_move(g, True)), None)
            if gate:
                action = (gate, True, f"no paper entry for {idle_h:.1f}h; '{gate}' blocked "
                                      f"{tally[gate]} tokens this window")
        a.reject_reasons.clear()
        if not action:
            return None
        gate, loosen, why = action
        before, after = notch(gate, loosen)
        rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "gate": gate,
               "dir": "loosen" if loosen else "tighten", "from": before, "to": after,
               "why": why, "closed_n": n, "win_rate": None if wr is None else round(wr, 3)}
        self.history.append(rec)
        self.last_change_ts = now
        self._persist()
        try:
            with self.log_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        journal("autotune", **rec)
        a.emit("info", f"AUTOTUNE {rec['dir']} {gate}: {before} -> {after} ({why})")
        return rec
