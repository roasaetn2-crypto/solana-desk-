"""Optional remote persistence for journal, paper ledger, operator intent and settings.

When TURSO_DATABASE_URL + TURSO_AUTH_TOKEN (or a turso DATABASE_URL) are set,
this module syncs those files to a free Turso/libSQL database over HTTP and
restores them on boot. With no URL it is a no-op and the desk keeps using
local files. Fail-open: a store error must never stop paper trading.

Secrets never go in logs, the journal, or raised messages.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from desk.ops import data_file

_lock = threading.Lock()
_ready = False
# What the last boot-time restore did, and when. Reported by status() so
# "persistence is on" is a measurement from the live box rather than a guess
# made from the Environment tab. Cheap: no network, just the last result.
_last: dict = {"at": None, "pulled": [], "pushed": [], "error": None}
# probe() hits Turso, so it is cached: an unauthenticated endpoint that does a
# round trip per call is a free way to burn someone else's quota.
_probe_cache: dict = {"at": 0.0, "body": None}
PROBE_TTL_S = 30.0
# Injected in tests. Signature: (pipeline_url, token, payload) -> (status_code, body_dict)
_poster: Callable[[str, str, dict], tuple[int, dict]] | None = None

_KV = (
    ("paper_ledger", lambda: data_file("paper_ledger.json")),
    ("operator_intent", lambda: data_file("operator_intent.json")),
    ("operator_settings", lambda: data_file("operator_settings.json")),
    ("proposals", lambda: data_file("proposals.json")),
    ("desk_chat", lambda: data_file("desk_chat.json")),
)
_JOURNAL = "journal"


def _clean(url: str) -> str:
    return (url or "").strip().strip('"').strip("'")


def database_url() -> str:
    raw = _clean(os.environ.get("TURSO_DATABASE_URL") or "")
    if not raw:
        alt = _clean(os.environ.get("DATABASE_URL") or "")
        if "turso.io" in alt or alt.startswith("libsql://"):
            raw = alt
    return raw


def auth_token() -> str:
    return _clean(
        os.environ.get("TURSO_AUTH_TOKEN")
        or os.environ.get("DATABASE_AUTH_TOKEN")
        or ""
    )


def configured() -> bool:
    return bool(database_url() and auth_token())


def pipeline_url(raw: str | None = None) -> str:
    u = _clean(raw or database_url())
    if u.startswith("libsql://"):
        u = "https://" + u[len("libsql://"):]
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    u = u.rstrip("/")
    if u.endswith("/v2/pipeline"):
        return u
    return u + "/v2/pipeline"


def _redact(msg: str) -> str:
    token = auth_token()
    url = database_url()
    if token:
        msg = msg.replace(token, "[redacted]")
    if url:
        # never print the full URL (org slug + db name are identifying)
        try:
            host = urlparse(pipeline_url(url)).hostname or "turso"
        except Exception:
            host = "turso"
        msg = msg.replace(url, host)
        msg = msg.replace(pipeline_url(url), host)
    return msg


def _arg(v: Any) -> dict:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int) and not isinstance(v, bool):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": str(v)}
    if isinstance(v, (bytes, bytearray)):
        import base64
        return {"type": "blob", "value": base64.b64encode(v).decode("ascii")}
    return {"type": "text", "value": str(v)}


def _cell(c: Any) -> Any:
    if not isinstance(c, dict):
        return c
    typ, val = c.get("type"), c.get("value")
    if typ == "null" or val is None:
        return None
    if typ == "integer":
        try:
            return int(val)
        except (TypeError, ValueError):
            return val
    if typ == "float":
        try:
            return float(val)
        except (TypeError, ValueError):
            return val
    return val


def _post(payload: dict) -> dict:
    if not configured() and _poster is None:
        return {"ok": False, "error": "not configured"}
    url, token = pipeline_url(), auth_token()
    if _poster is not None:
        status, body = _poster(url, token, payload)
    else:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=8,
        )
        try:
            body = r.json()
        except ValueError:
            body = {"error": "non-json response"}
        status = r.status_code
    if status >= 400 or (isinstance(body, dict) and body.get("error")):
        err = (body or {}).get("error") or f"http {status}"
        raise RuntimeError(_redact(str(err)))
    return body if isinstance(body, dict) else {"raw": body}


def execute(sql: str, args: list | None = None) -> dict:
    stmt: dict[str, Any] = {"sql": sql}
    if args:
        stmt["args"] = [_arg(a) for a in args]
    body = _post({"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]})
    results = body.get("results") or []
    if not results:
        raise RuntimeError("empty store response")
    first = results[0]
    if first.get("type") != "ok":
        err = first.get("error") or first
        raise RuntimeError(_redact(str(err)))
    result = ((first.get("response") or {}).get("result")) or {}
    cols = [c.get("name") if isinstance(c, dict) else c for c in (result.get("cols") or [])]
    rows = []
    for raw in result.get("rows") or []:
        cells = [_cell(c) for c in raw]
        rows.append(dict(zip(cols, cells)) if cols else cells)
    return {"cols": cols, "rows": rows, "affected": result.get("affected_row_count", 0)}


def _ensure() -> bool:
    global _ready
    if not configured() and _poster is None:
        return False
    if _ready:
        return True
    with _lock:
        if _ready:
            return True
        execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        execute(
            "CREATE TABLE IF NOT EXISTS journal ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, kind TEXT, mint TEXT, body TEXT NOT NULL)"
        )
        _ready = True
        return True


def reset_runtime() -> None:
    """Tests only: drop the cached schema flag and the cached probe."""
    global _ready
    _ready = False
    _probe_cache.update(at=0.0, body=None)
    _last.update(at=None, pulled=[], pushed=[], error=None)


def put_kv(key: str, value: str) -> None:
    if not configured() and _poster is None:
        return
    try:
        _ensure()
        now = datetime.now(timezone.utc).isoformat()
        execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [key, value, now],
        )
    except Exception as e:
        print(f"[desk] store: put {key} failed ({_redact(str(e))})")


def get_kv(key: str) -> str | None:
    if not configured() and _poster is None:
        return None
    _ensure()
    out = execute("SELECT value FROM kv WHERE key=?", [key])
    rows = out.get("rows") or []
    if not rows:
        return None
    row = rows[0]
    return row.get("value") if isinstance(row, dict) else (row[0] if row else None)


def put_json(key: str, obj: Any) -> None:
    put_kv(key, json.dumps(obj, default=str))


def get_json(key: str) -> Any | None:
    raw = get_kv(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def append_journal(row: dict) -> None:
    if not configured() and _poster is None:
        return
    try:
        _ensure()
        execute(
            "INSERT INTO journal(time, kind, mint, body) VALUES(?,?,?,?)",
            [row.get("time"), row.get("kind"), row.get("mint"), json.dumps(row, default=str)],
        )
    except Exception as e:
        print(f"[desk] store: journal append failed ({_redact(str(e))})")


def load_journal_rows() -> list[dict]:
    _ensure()
    out = execute("SELECT body FROM journal ORDER BY id")
    rows = []
    for r in out.get("rows") or []:
        raw = r.get("body") if isinstance(r, dict) else (r[0] if r else None)
        if not raw:
            continue
        try:
            rows.append(json.loads(raw) if isinstance(raw, str) else raw)
        except json.JSONDecodeError:
            continue
    return rows


def journal_count() -> int:
    _ensure()
    out = execute("SELECT COUNT(*) AS n FROM journal")
    rows = out.get("rows") or []
    if not rows:
        return 0
    r = rows[0]
    return int(r.get("n") if isinstance(r, dict) else r[0] or 0)


def push_file(key: str, path: Path) -> None:
    if not path.exists():
        return
    put_kv(key, path.read_text(encoding="utf-8"))


def pull_file(key: str, path: Path) -> bool:
    raw = get_kv(key)
    if raw is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return True


def push_local_journal(path: Path | None = None) -> int:
    """Upload local journal lines the remote does not yet have (first-time hookup)."""
    p = path or data_file("journal.jsonl")
    if not p.exists():
        return 0
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    remote_n = journal_count()
    if remote_n >= len(lines):
        return 0
    added = 0
    for ln in lines[remote_n:]:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        append_journal(row)
        added += 1
    return added


def restore() -> dict:
    """Pull remote state into local files. No-op if the store is not configured.

    If the remote journal is empty and a local journal already exists (first
    time the URL is set on a box that has history), the local file is pushed.
    """
    out = {"configured": configured() or _poster is not None, "pulled": [], "pushed": []}
    if not out["configured"]:
        _last.update(at=time.time(), pulled=[], pushed=[], error="not configured")
        return out
    try:
        _ensure()
    except Exception as e:
        print(f"[desk] store: restore skipped ({_redact(str(e))})")
        out["error"] = "unavailable"
        _last.update(at=time.time(), pulled=[], pushed=[], error="unavailable")
        return out

    # journal
    try:
        remote = load_journal_rows()
        local_path = data_file("journal.jsonl")
        if remote:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with local_path.open("w", encoding="utf-8") as f:
                for row in remote:
                    f.write(json.dumps(row, default=str) + "\n")
            out["pulled"].append("journal")
        elif local_path.exists():
            n = push_local_journal(local_path)
            if n:
                out["pushed"].append(f"journal:{n}")
    except Exception as e:
        print(f"[desk] store: journal restore failed ({_redact(str(e))})")

    for key, path_fn in _KV:
        path = path_fn()
        try:
            if pull_file(key, path):
                out["pulled"].append(key)
            elif path.exists():
                push_file(key, path)
                out["pushed"].append(key)
        except Exception as e:
            print(f"[desk] store: {key} restore failed ({_redact(str(e))})")

    if out["pulled"] or out["pushed"]:
        print(f"[desk] store: restored {', '.join(out['pulled']) or 'nothing'} "
              f"(pushed {', '.join(out['pushed']) or 'nothing'})")
    else:
        print("[desk] store: connected, no history to restore yet")
    _last.update(at=time.time(), pulled=list(out["pulled"]),
                 pushed=list(out["pushed"]), error=out.get("error"))
    return out


def _host() -> str | None:
    """The database host, never the full URL: the org slug is identifying."""
    raw = database_url()
    if not raw:
        return None
    try:
        return urlparse(pipeline_url(raw)).hostname
    except Exception:
        return None


def status() -> dict:
    """Whether persistence is wired up, and what the boot restore managed.

    No network. This answers the question the Environment tab cannot: the vars
    are set, but did this *process* see them and did the schema get created?
    `tables_ready` is the load-bearing field -- an empty database that was
    never touched looks identical to a working one from the outside.
    """
    cfg = configured()
    return {
        "configured": cfg,
        "host": _host(),
        "token_present": bool(auth_token()),
        "tables_ready": bool(_ready),
        "last_restore_at": _last["at"],
        "last_restore_error": _last["error"],
        "pulled": list(_last["pulled"]),
        "pushed": list(_last["pushed"]),
        "journal_rows_local": _local_journal_rows(),
    }


def _local_journal_rows() -> int:
    try:
        p = data_file("journal.jsonl")
        if not p.exists():
            return 0
        with p.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def probe() -> dict:
    """One real round trip: which tables exist and how many journal rows.

    This is the only call that proves the token is accepted *and* the schema is
    there. Cached for PROBE_TTL_S because it is reachable without a token.
    """
    if not configured() and _poster is None:
        return {"configured": False, "ok": False, "error": "not configured"}
    now = time.time()
    if _probe_cache["body"] is not None and now - _probe_cache["at"] < PROBE_TTL_S:
        return {**_probe_cache["body"], "cached": True}
    body: dict = {"configured": True, "host": _host(), "ok": False}
    try:
        names = [r["name"] for r in execute(
            "select name from sqlite_master where type='table' order by name")["rows"]]
        rows = None
        try:
            got = execute("select count(*) as n from journal")["rows"]
            rows = got[0]["n"] if got else 0
        except Exception:
            rows = None  # table missing: schema not created yet
        body.update(ok=True, tables=names, journal_rows_remote=rows,
                    schema_ready=("journal" in names and "kv" in names))
    except Exception as e:
        body["error"] = _redact(str(e))
    _probe_cache.update(at=now, body=body)
    return {**body, "cached": False}


def sync_kv_file(key: str, path: Path | None = None) -> None:
    """Push one local snapshot file. Used by ledger / intent / settings hooks."""
    if not configured() and _poster is None:
        return
    mapping = {k: fn for k, fn in _KV}
    fn = mapping.get(key)
    target = path or (fn() if fn else None)
    if target is None:
        return
    try:
        push_file(key, target)
    except Exception as e:
        print(f"[desk] store: sync {key} failed ({_redact(str(e))})")
