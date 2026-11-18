"""PAPER analyst on a real model, with a hard budget and a heuristic fallback.

desk/brain.py picks ONE provider for every seat and, on purpose, a bare cloud key does not switch
it on (the analyst's score gates trades). This module is the narrow, explicit opt-in for the
PAPER analyst only (config.ANALYST_AI). It returns a TokenThesis or None; None always means
"use the heuristic". It never sizes, fills, or touches risk, broker, live, kills or limits.

Budgets: ANALYST_AI["daily_cap"] calls per UTC day (persisted), ["per_cycle"] per agent cycle,
a short per-call timeout, and a per-provider cooldown after any failure so a 429 is not hammered.
"""
from __future__ import annotations

import json
import os
import threading
import time

import config

# provider -> (label, base url, key env, model env, default model)
_PROVIDERS = {
    "xai": ("xAI Grok", config.XAI["base_url"], "XAI_API_KEY", "BRAIN_XAI_MODEL",
            config.XAI["model"]),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "BRAIN_ANALYST_GROQ_MODEL",
             "openai/gpt-oss-20b"),
}

_lock = threading.Lock()
_cycle_calls = 0
_cooldown_until: dict[str, float] = {}
_last_error: dict[str, str] = {}
_session = None


def _cfg() -> dict:
    return getattr(config, "ANALYST_AI", {}) or {}


def _state_path() -> str:
    from desk.ops import data_file
    return os.getenv("BRAIN_ANALYST_AI_STATE") or str(data_file("analyst_ai_state.json"))


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            s = json.load(f)
        if s.get("day") == _today():
            return s
    except Exception:
        pass
    return {"day": _today(), "calls": 0}


def _save(s: dict) -> None:
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:
        pass


def _key(name: str) -> str:
    v = (os.getenv(_PROVIDERS[name][2], "") or "").strip().strip('"').strip("'")
    return v if len(v) >= 8 else ""


def model_for(name: str) -> str:
    return (os.getenv(_PROVIDERS[name][3]) or _PROVIDERS[name][4]).strip()


def _off() -> bool:
    return (os.getenv("BRAIN_ANALYST_AI", "") or "").strip().lower() in ("0", "off", "false", "no")


def providers() -> list[str]:
    """Configured providers that have a key and are not resting, in preference order."""
    now = time.time()
    out = []
    for name in _cfg().get("prefer", ()):
        if name in _PROVIDERS and _key(name) and _cooldown_until.get(name, 0.0) <= now:
            out.append(name)
    return out


def enabled(mode: str | None) -> bool:
    """PAPER only: any other mode (live, unknown) keeps the existing analyst."""
    if mode != "paper" or _off() or not _cfg().get("enabled"):
        return False
    return any(_key(n) for n in _cfg().get("prefer", ()) if n in _PROVIDERS)


def new_cycle() -> None:
    global _cycle_calls
    with _lock:
        _cycle_calls = 0


def budget_left() -> int:
    with _lock:
        return max(0, int(_cfg().get("daily_cap", 0)) - _load().get("calls", 0))


def _spend() -> bool:
    """Take one call from both the daily and the per-cycle budget."""
    global _cycle_calls
    with _lock:
        s = _load()
        if s.get("calls", 0) >= int(_cfg().get("daily_cap", 0)):
            return False
        if _cycle_calls >= int(_cfg().get("per_cycle", 0)):
            return False
        s["calls"] = s.get("calls", 0) + 1
        _cycle_calls += 1
        _save(s)
        return True


def _http():
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
    return _session


def _call(name: str, system: str, user: str) -> str:
    label, base, _env, _menv, _dm = _PROVIDERS[name]
    r = _http().post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {_key(name)}", "Content-Type": "application/json"},
        json={"model": model_for(name), "temperature": 0.2,
              "max_tokens": int(_cfg().get("max_tokens", 1500)),
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=float(_cfg().get("timeout_s", 8)),
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()["choices"][0]["message"]["content"]


def _rest(name: str, why: str) -> None:
    _cooldown_until[name] = time.time() + float(_cfg().get("cooldown_s", 300))
    _last_error[name] = why[:80]


def thesis(system: str, user: str, schema):
    """Ask the first available provider for a validated `schema`. None = use the heuristic."""
    from desk import brain
    for name in providers():
        if not _spend():
            return None
        try:
            data = brain.extract_json(_call(name, system, user))
            out = schema.model_validate(data)
            _last_error.pop(name, None)
            return out
        except Exception as e:           # 429, 5xx, timeout, bad JSON: rest, try the next provider
            _rest(name, f"{type(e).__name__}: {e}")
    return None


def describe(mode: str | None = "paper") -> str:
    if not enabled(mode):
        return "off"
    names = providers() or [n for n in _cfg().get("prefer", ()) if n in _PROVIDERS and _key(n)]
    lead = names[0] if names else None
    if not lead:
        return "off"
    return (f"{_PROVIDERS[lead][0]} · {model_for(lead)} "
            f"(paper analyst, {_cfg().get('daily_cap')}/day, {_cfg().get('per_cycle')}/cycle, "
            "heuristic fallback)")


def status() -> dict:
    return {"enabled": enabled("paper"), "label": describe("paper"),
            "providers": [n for n in _cfg().get("prefer", ()) if n in _PROVIDERS and _key(n)],
            "calls_today": _load().get("calls", 0), "daily_cap": _cfg().get("daily_cap"),
            "per_cycle": _cfg().get("per_cycle"),
            "resting": sorted(n for n, t in _cooldown_until.items() if t > time.time()),
            "last_error": dict(_last_error)}
