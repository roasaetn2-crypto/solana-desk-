"""Persisted Desk AI conversation. Last ~50 messages, Turso + local file.

Survives Render restarts when TURSO is configured (hooked into store.restore).
Fail-open: a store error must never stop paper trading or the chat reply.
desk_chat.py does not import this module (it stays isolated from store/ops).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from desk.ops import data_file

MAX_MESSAGES = 50
STORE_KEY = "desk_chat"
_FILE = "desk_chat.json"

_lock = threading.Lock()
_cache: list[dict] | None = None

_ROLES = {"user", "ai"}
_PROP_KEYS = ("id", "status", "source", "title", "preset", "reason", "changes",
              "dropped", "error", "expires")
_CHANGE_KEYS = ("setting", "label", "current", "proposed", "type", "unit",
                "reason", "note")


def _path():
    return data_file(_FILE)


def reset() -> None:
    """Tests only: drop the in-memory cache so the next read hits the file."""
    global _cache
    with _lock:
        _cache = None


def messages() -> list[dict]:
    with _lock:
        return [dict(m) for m in _loaded()]


def history_for_model(n: int = 8) -> list[dict]:
    """Recent user/ai turns in the shape desk_chat.answer() expects."""
    out = []
    for m in messages()[-max(1, n):]:
        if m.get("role") in _ROLES and m.get("text"):
            out.append({"role": m["role"], "text": m["text"]})
    return out


def append(msg: dict | None) -> list[dict]:
    clean = sanitize(msg)
    if not clean:
        return messages()
    with _lock:
        cur = list(_loaded())
        cur.append(clean)
        _save(cur[-MAX_MESSAGES:])
        return [dict(m) for m in _cache or []]


def extend(msgs) -> list[dict]:
    with _lock:
        cur = list(_loaded())
        for m in msgs or []:
            clean = sanitize(m)
            if clean:
                cur.append(clean)
        _save(cur[-MAX_MESSAGES:])
        return [dict(m) for m in _cache or []]


def seed(msgs) -> list[dict]:
    """Adopt client-side history only when the server has none (first deploy of this feature)."""
    with _lock:
        cur = list(_loaded())
        if cur:
            return [dict(m) for m in cur]
        fresh = []
        for m in msgs or []:
            clean = sanitize(m)
            if clean:
                fresh.append(clean)
        _save(fresh[-MAX_MESSAGES:])
        return [dict(m) for m in _cache or []]


def clear() -> None:
    with _lock:
        _save([])


def sanitize(msg: Any) -> dict | None:
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    if role not in _ROLES:
        return None
    text = _clean_text(msg.get("text") or "", 1200)
    if not text:
        return None
    try:
        t = int(msg.get("t") or time.time() * 1000)
    except (TypeError, ValueError):
        t = int(time.time() * 1000)
    out: dict[str, Any] = {"role": role, "text": text, "t": t}
    agents = [a for a in (msg.get("agents") or []) if isinstance(a, str)]
    if agents:
        out["agents"] = [_clean_text(a, 20) for a in agents[:4] if a]
    if msg.get("provider"):
        out["provider"] = _clean_text(msg.get("provider"), 20)
    if msg.get("err"):
        out["err"] = True
    props = _props(msg.get("proposals"))
    if props:
        out["proposals"] = props
    dropped = msg.get("dropped")
    if isinstance(dropped, list) and dropped:
        out["dropped"] = [_drop(x) for x in dropped[:8] if _drop(x)]
    return out


def _props(raw) -> list[dict]:
    out = []
    if not isinstance(raw, list):
        return out
    for p in raw[:8]:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        row = {"id": _clean_text(p.get("id"), 20)}
        for k in _PROP_KEYS:
            if k == "id" or k not in p or p[k] is None:
                continue
            if k == "changes" and isinstance(p[k], list):
                row[k] = [_change(c) for c in p[k][:12] if _change(c)]
            elif k == "dropped" and isinstance(p[k], list):
                row[k] = [_drop(x) for x in p[k][:8] if _drop(x)]
            elif k in ("reason", "title", "source", "preset", "status", "error", "expires"):
                row[k] = _clean_text(p[k], 300 if k == "reason" else 80)
        out.append(row)
    return out


def _change(c) -> dict | None:
    if not isinstance(c, dict) or not (c.get("setting") or c.get("label")):
        return None
    row = {}
    for k in _CHANGE_KEYS:
        if k not in c or c[k] is None:
            continue
        v = c[k]
        row[k] = v if isinstance(v, (int, float, bool)) else _clean_text(v, 80)
    return row or None


def _drop(x) -> dict | None:
    if not isinstance(x, dict):
        return None
    setting = _clean_text(x.get("setting") or "", 60)
    why = _clean_text(x.get("why") or x.get("error") or "", 80)
    if not setting:
        return None
    return {"setting": setting, "why": why}


def _clean_text(v: Any, n: int) -> str:
    from desk.desk_chat import scrub, _txt
    return scrub(_txt(v, n))


def _loaded() -> list[dict]:
    global _cache
    if _cache is not None:
        return _cache
    rows: list[dict] = []
    path = _path()
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            src = raw.get("messages") if isinstance(raw, dict) else raw
            if isinstance(src, list):
                for m in src:
                    clean = sanitize(m)
                    if clean:
                        rows.append(clean)
    except Exception:
        rows = []
    _cache = rows[-MAX_MESSAGES:]
    return _cache


def _save(rows: list[dict]) -> None:
    global _cache
    _cache = list(rows)
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"messages": _cache}, default=str), encoding="utf-8")
    except Exception as e:
        print(f"[desk] chat memory: local write failed ({type(e).__name__})")
        return
    try:
        from desk.store import sync_kv_file
        sync_kv_file(STORE_KEY, path)
    except Exception as e:
        print(f"[desk] chat memory: store sync failed ({type(e).__name__})")
