"""Operator intent for the paper loop: start, stop, and auto-resume after a deploy.

A deliberate Pause / Stop is written to operator_intent.json (and the remote
store, when configured) so it survives a restart. Auto-start is PAPER only,
never LIVE, and is skipped when the last intent was stopped or paused.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from desk.ops import data_file, killed

INTENT_FILE = "operator_intent.json"
STOPPED = {"stopped", "paused"}


def intent_path() -> Path:
    return data_file(INTENT_FILE)


def paper_autostart_enabled() -> bool:
    v = (os.environ.get("PAPER_AUTOSTART") or "1").strip().lower()
    return v not in ("0", "false", "off", "no")


def load_intent() -> dict:
    p = intent_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def loop_intent() -> str | None:
    v = load_intent().get("loop")
    if v is None:
        return None
    return str(v).strip().lower() or None


def record_loop_intent(state: str, source: str = "operator") -> dict:
    """Persist running / stopped. `paused` is stored as stopped."""
    state = "stopped" if str(state).lower() in STOPPED else ("running" if str(state).lower() == "running" else str(state).lower())
    row = {
        "loop": state,
        "source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = intent_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"[desk] intent: could not write ({e})")
    try:
        from desk.store import sync_kv_file
        sync_kv_file("operator_intent", path)
    except Exception:
        pass
    return row


def should_autostart(mode: str, intent: str | None = None) -> tuple[bool, str]:
    """Return (go, reason). Hard-coded PAPER only; never auto-starts LIVE."""
    if (mode or "").lower() != "paper":
        return False, "mode is live" if (mode or "").lower() == "live" else f"mode is {mode or 'unknown'}"
    if not paper_autostart_enabled():
        return False, "off"
    if killed():
        return False, "kill switch is on"
    last = intent if intent is not None else loop_intent()
    if last in STOPPED:
        return False, "operator paused"
    return True, "starting the loop"


def maybe_autostart(agent) -> dict:
    """Start the paper loop on boot unless the operator paused it. Logs once."""
    from desk.netutil import offline
    if offline():
        return {"ok": True, "started": False, "reason": "offline"}
    mode = getattr(agent, "mode", None) or "paper"
    go, reason = should_autostart(mode)
    if not go:
        print(f"[desk] paper autostart: skipped, {reason}")
        return {"ok": True, "started": False, "reason": reason}
    print(f"[desk] paper autostart: {reason} (PAPER_AUTOSTART=1)")
    result = agent.start(persist_intent=True, intent_source="autostart")
    return {"ok": True, "started": True, "reason": reason, "result": result}
