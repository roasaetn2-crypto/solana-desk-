"""FastAPI server: the dashboard's window into the Solana Agent.

    ./run.sh                 # locked, random token printed
    ./run.sh mypassword      # locked with your token
    ./run.sh --open          # no lock at all

Access control
    Two ways in:

    1. A PAIRED DEVICE. Pair once with the key; that browser then holds a
       long-lived HttpOnly cookie and opens the desk with no key. This is
       how "only my phone gets in" works.
    2. THE KEY. Any other device can still get in by typing DESK_TOKEN.

    With no DESK_TOKEN the desk is open to anyone who can reach the port.

Endpoints
    GET  /                     the dashboard
    GET  /login                key form + "pair this device"
    POST /login                sets the master session cookie
    POST /pair                 pairs this browser as a device (needs the key)
    GET  /logout               clears both cookies
    GET  /devices              list paired devices, revoke them
    GET  /api/state            full agent + broker + event snapshot
    GET  /api/chart            OHLC for one ticket (query: mint, tf)
    GET  /api/backtest         read-only gate replay over the journal (never changes settings)
    (boot)                    restores journal/ledger/intent from Turso when configured;
                              auto-starts the PAPER loop unless the operator paused it
    GET  /api/devices          paired device list (JSON)
    DELETE /api/devices/{id}   revoke one device
    POST /api/agent/start      start the background loop
    POST /api/agent/stop       pause the background loop
    POST /api/risk/continue    PAPER only: resume entries today after the 3% daily stop
    GET  /api/ai/status        Desk AI chat: keys present? limits (never key values)
    GET  /api/ai/history       Desk AI: persisted conversation (last ~50 messages)
    POST /api/ai/history       seed history if the server has none (from the phone cache)
    POST /api/ai/chat          Desk AI: advisory answer; may return PENDING setting proposals
    POST /api/ai/chat/clear    wipe the persisted Desk AI conversation
    POST /api/ai/recommend     Desk AI: recommend paper settings from recent performance (pending only)
    GET  /api/settings         every proposable PAPER setting: value, type, bounds; exclusions
    GET  /api/proposals        pending + recent setting proposals
    POST /api/proposals        operator preset (scalper / swing / balanced) -> pending proposal
    POST /api/proposals/{id}/confirm   apply a proposal (re-validated, persisted, journaled)
    POST /api/proposals/{id}/reject    drop a proposal (journaled)
    POST /api/agent/step       run exactly one cycle (query: ?demo=1)
    POST /api/agent/reset      reset paper equity + bench + log
    GET  /api/mode             paper or live, caps, wallet, arming state
    POST /api/mode             switch mode (live needs the confirm phrase)
    POST /api/kill             trip the kill switch (close all, stop)
    DELETE /api/kill           clear the kill switch
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import hmac
import os
import time
from pathlib import Path

from fastapi import Body, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from desk import devices, keepalive, pumpportal
from desk.ops import KILL_FILE, load_env
from desk.solana_agent import SolanaAgent
from desk.store import restore as restore_store
from desk.intent import maybe_autostart

load_env()
restore_store()

@asynccontextmanager
async def _lifespan(app):
    # Feed is independent of the trading loop so a deploy is not "off" until Start.
    pumpportal.start()
    # PAPER only, and only if the operator did not pause the desk.
    maybe_autostart(agent)
    # Free plan sleeps after ~15 min idle; ping our own public URL.
    keepalive.start()
    yield
    keepalive.stop()
    pumpportal.stop()


app = FastAPI(title="AI Trading Desk \u2014 Solana Agent", version="1.3.0", lifespan=_lifespan)
agent = SolanaAgent()


WEB = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")

SESSION_COOKIE = "desk_session"
DEVICE_COOKIE = "desk_device"

DESK_TOKEN = os.environ.get("DESK_TOKEN", "").strip()
_SESSION = hashlib.sha256(("desk::" + DESK_TOKEN).encode()).hexdigest() if DESK_TOKEN else ""

PUBLIC_PATHS = {"/login", "/logout", "/pair", "/api/health"}


def _master(request: Request) -> bool:
    if not DESK_TOKEN:
        return True
    return hmac.compare_digest(request.cookies.get(SESSION_COOKIE, ""), _SESSION)


def _device(request: Request) -> dict | None:
    return devices.verify(request.cookies.get(DEVICE_COOKIE, ""))


def _authed(request: Request) -> bool:
    if not DESK_TOKEN:
        return True
    return _master(request) or _device(request) is not None


LOGIN_HTML = (WEB / "login.html").read_text(encoding="utf-8")


DEVICES_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="theme-color" content="#080c12" />
<title>Devices \u2014 Solana Desk</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;background:#080c12;color:#e6edf5;padding:22px 16px 60px;
       font-family:Inter,system-ui,-apple-system,sans-serif}
  .wrap{max-width:640px;margin:0 auto}
  h1{font-size:17px;margin:0 0 4px}
  p.s{font-size:12px;color:#7d8ea3;margin:0 0 22px;line-height:1.5}
  .row{display:flex;align-items:center;gap:12px;background:#0d131b;border:1px solid #1c2735;
       border-radius:11px;padding:13px 15px;margin-bottom:9px}
  .row .n{font-weight:700;font-size:13.5px}
  .row .m{font-size:11px;color:#6b7c91;margin-top:2px}
  .row .sp{flex:1}
  .row form{margin:0}
  .rev{background:#1a1116;color:#ff4d6d;border:1px solid rgba(255,77,109,.3);
       border-radius:8px;padding:7px 12px;font-size:11.5px;font-weight:600;cursor:pointer;
       font-family:inherit}
  .empty{color:#5a6a7d;font-size:12.5px;padding:18px 0}
  a{color:#2ee59d;font-size:12.5px;text-decoration:none}
</style></head><body><div class="wrap">
  <h1>Paired devices</h1>
  <p class="s">These browsers open the desk without a key. Revoke any you do not recognise.</p>
  __ROWS__
  <p style="margin-top:26px"><a href="/">\u2190 back to the desk</a></p>
</div></body></html>"""


@app.middleware("http")
async def gate(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if _authed(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(error: int = 0):
    if not DESK_TOKEN:
        return RedirectResponse("/", status_code=303)
    body = LOGIN_HTML.replace(
        "__ERROR__", '<div class="err">Wrong key. Try again.</div>' if error else ""
    )
    return body


@app.post("/login", response_class=HTMLResponse)
def login_submit(token: str = Form("")):
    if not DESK_TOKEN:
        return RedirectResponse("/", status_code=303)
    if not hmac.compare_digest(token.strip(), DESK_TOKEN):
        return RedirectResponse("/login?error=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, _SESSION, httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 7)
    return resp


@app.post("/pair", response_class=HTMLResponse)
def pair_device(name: str = Form(""), token: str = Form("")):
    """Bind this browser to the desk. Requires the key once."""
    if not DESK_TOKEN:
        return RedirectResponse("/", status_code=303)
    if not hmac.compare_digest(token.strip(), DESK_TOKEN):
        return RedirectResponse("/login?error=1", status_code=303)
    raw, rec = devices.pair(name or "unnamed device")
    resp = RedirectResponse("/", status_code=303)
    # One year, HttpOnly: the token never touches JavaScript.
    resp.set_cookie(DEVICE_COOKIE, raw, httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 365)
    resp.set_cookie(SESSION_COOKIE, _SESSION, httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 7)
    print(f"[desk] paired device {rec['id']} \u2014 {rec['name']}")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(DEVICE_COOKIE)
    return resp


@app.get("/devices", response_class=HTMLResponse)
def devices_page():
    rows = devices.listing()
    if not rows:
        body = '<div class="empty">No devices paired yet. Use \u201cPair this device\u201d on the login screen.</div>'
    else:
        body = "".join(
            f'<div class="row"><div><div class="n">{r["name"]}</div>'
            f'<div class="m">id {r["id"]} \u00b7 paired {r["created"][:10]} \u00b7 last seen {r["last_seen"][:16].replace("T", " ")}</div></div>'
            f'<div class="sp"></div>'
            f'<form method="post" action="/devices/{r["id"]}/revoke">'
            f'<button class="rev" type="submit">Revoke</button></form></div>'
            for r in rows
        )
    return DEVICES_HTML.replace("__ROWS__", body)


@app.post("/devices/{device_id}/revoke")
def revoke_device(device_id: str):
    devices.revoke(device_id)
    return RedirectResponse("/devices", status_code=303)


@app.get("/api/devices")
def api_devices():
    return {"devices": devices.listing()}


@app.delete("/api/devices/{device_id}")
def api_revoke(device_id: str):
    return {"ok": devices.revoke(device_id), "id": device_id}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/state")
def state(request: Request):
    snap = agent.status()
    dev = _device(request)
    snap["access"] = {
        "locked": bool(DESK_TOKEN),
        "mode": "device" if dev else ("key" if _master(request) else "open"),
        "device": dev["name"] if dev else None,
        "paired_devices": devices.count(),
    }
    return JSONResponse(snap)


@app.get("/api/chart")
def chart(mint: str = "", tf: str = ""):
    return agent.chart(mint, tf or None)


_BACKTEST_CACHE: dict = {"t": 0.0, "data": None}


@app.get("/api/backtest")
def backtest():
    """Replay recorded tickets against other gate settings. Read-only; never applies a change."""
    now = time.time()
    cached = _BACKTEST_CACHE.get("data")
    if cached is not None and now - _BACKTEST_CACHE["t"] < 30:
        return cached
    from desk.backtest import report
    data = report()
    _BACKTEST_CACHE["t"] = now
    _BACKTEST_CACHE["data"] = data
    return data


@app.get("/api/health")
def health():
    pp = pumpportal.health()
    return {
        "ok": True,
        "running": agent.running,
        "cycle": agent.cycle_no,
        "pumpportal": {
            "status": pp["status"],
            "connected": pp["connected"],
            "enabled": pp["enabled"],
            "received": pp["received"],
            "tracked": pp["tracked"],
            "reconnects": pp["reconnects"],
            "error": pp.get("error"),
        },
        "ai": _ai_health(),
        "desk": _desk_health(),
    }


def _desk_health() -> dict:
    """Where pools come from and the PAPER gate values actually live (after saved overrides).
    Numbers only: no keys, no wallet, nothing that changes state."""
    try:
        import config as c
        S, R, E, M = c.SCAN, c.RISK, c.EXITS, c.MEME
        st = agent.stats
        return {
            "data_source": agent.data_source,
            "last_cycle_at": agent.last_cycle_at,
            "mode": agent.mode,
            "gates": {
                "style": E.get("style", "both"),
                "min_age_minutes": S["min_age_minutes"], "max_age_hours": S["max_age_hours"],
                "min_liquidity_usd": S["min_liquidity_usd"], "min_volume_h1_usd": S["min_volume_h1_usd"],
                "min_mcap_usd": S["min_mcap_usd"], "min_trades_h1": S["min_trades_h1"], "pages": S["pages"],
                "max_open_positions": R["max_open_positions"], "max_risk_per_trade": R["max_risk_per_trade"],
                "max_daily_loss_pct": R["max_daily_loss_pct"],
                "max_daily_loss_usd": R.get("max_daily_loss_usd", 0.0),
                "min_paper_ticket_usd": c.PAPER["min_ticket_usd"],
                "fallback_stop_pct": E["stop_pct"], "fallback_max_hold_hours": E["max_hold_hours"],
                "scalp_stop_pct": E["profiles"]["scalp"]["stop_pct"],
                "scalp_max_hold_hours": E["profiles"]["scalp"]["max_hold_hours"],
                "swing_stop_pct": E["profiles"]["swing"]["stop_pct"],
                "swing_max_hold_hours": E["profiles"]["swing"]["max_hold_hours"],
                "scalp_timeout_hours": E["profiles"]["scalp"]["timeout_hours"],
                "swing_timeout_hours": E["profiles"]["swing"]["timeout_hours"],
                "breakeven_after_half": E.get("breakeven_after_half"),
                "runner_trail_pct": E.get("runner_trail_pct"), "runner_max_minutes": E.get("runner_max_minutes"),
                "min_community_score": M["min_community_score"], "analyst_min_score": c.ANALYST_VALID_SCORE,
            },
            "stats": {k: st.get(k, 0) for k in ("cycles", "seen", "free_rejects", "chain_rejects", "dev_rejects",
                                                "sec_rejects", "meme_rejects", "risk_rejects", "guard_rejects",
                                                "entries", "exits", "errors")},
            "open_positions": agent.broker.open_count(),
        }
    except Exception as e:  # health must never fail
        return {"error": type(e).__name__}


def _ai_health() -> dict:
    try:
        from desk import desk_chat
        return desk_chat.health()
    except Exception as e:  # health must never fail
        return {"error": type(e).__name__}


@app.get("/api/ai/status")
def ai_status():
    from desk import desk_chat
    return desk_chat.status()


@app.get("/api/ai/history")
def ai_history():
    """Persisted Desk AI conversation. Behind the same key / paired-device gate as chat."""
    from desk import chat_memory
    return {"ok": True, "messages": chat_memory.messages()}


@app.post("/api/ai/history")
def ai_history_seed(payload: dict = Body(None)):
    """Adopt the phone's cached chat only when the server has none (first run of this feature)."""
    from desk import chat_memory
    msgs = (payload or {}).get("messages") if isinstance(payload, dict) else None
    return {"ok": True, "messages": chat_memory.seed(msgs if isinstance(msgs, list) else [])}


@app.post("/api/ai/chat/clear")
def ai_chat_clear():
    """Wipe the persisted Desk AI conversation. Same auth as chat. Does not touch trades."""
    from desk import chat_memory
    chat_memory.clear()
    return {"ok": True, "messages": []}


@app.post("/api/ai/chat")
def ai_chat(payload: dict = Body(...)):
    """Desk AI. Advisory and read-only: it gets a whitelisted copy of the state, never the
    agent, so it cannot start, stop, trade, reset, kill or switch mode. Behind the same
    key / paired-device gate as Start and Pause; rate-limited per minute and per day."""
    from desk import desk_chat
    from desk import chat_memory
    q = str((payload or {}).get("message", ""))[:2000]
    if not q.strip():
        return JSONResponse({"ok": False, "error": "empty message"}, status_code=400)
    why = desk_chat.LIMITER.take()
    if why:
        return JSONResponse({"ok": False, "error": why}, status_code=429)
    client_hist = (payload or {}).get("history")
    client_hist = client_hist if isinstance(client_hist, list) else None
    server_hist = chat_memory.history_for_model()
    hist = server_hist or client_hist
    if client_hist and (not server_hist or len(client_hist) > len(server_hist)):
        hist = client_hist
    from desk import settings as desk_settings
    out = desk_chat.answer(q, agent.status(), hist,
                           settings=desk_settings.compact_for_ai())
    out = _with_proposals(out, "desk_ai")
    _remember_chat("user", q)
    if out.get("reply"):
        _remember_chat("ai", out.get("reply"), out)
    return out


def _remember_chat(role: str, text: str, out: dict | None = None) -> None:
    """Best-effort persist. Never raises into the chat response."""
    try:
        from desk import chat_memory
        msg = {"role": role, "text": text, "t": int(time.time() * 1000)}
        if out:
            if out.get("agents"):
                msg["agents"] = out["agents"]
            if out.get("provider"):
                msg["provider"] = out["provider"]
            if out.get("proposals"):
                msg["proposals"] = out["proposals"]
            if out.get("dropped"):
                msg["dropped"] = out["dropped"]
        chat_memory.append(msg)
    except Exception:
        pass


def _with_proposals(out: dict, source: str) -> dict:
    """Validate the model's raw suggestions and store them as ONE pending proposal. Never applies."""
    from desk import settings as desk_settings
    raw = out.pop("proposals", None) or []
    out["proposals"], out["dropped"] = [], []
    if raw:
        reason = " · ".join(x.get("reason", "") for x in raw if x.get("reason"))[:300]
        raw, preset = desk_settings.expand_style(raw)
        title = desk_settings.PRESETS[preset]["label"] if preset else "Desk AI suggestion"
        made = desk_settings.create(raw, reason or "Desk AI suggestion", source, title, preset=preset)
        if made.get("ok"):
            p = made["proposal"]
            out["proposals"] = [p]
            agent.emit("info", f"Desk AI proposed {len(p['changes'])} paper setting change(s); "
                               "waiting for Confirm", stage="settings")
        out["dropped"] = made.get("dropped") or (made.get("proposal") or {}).get("dropped") or []
    return out


@app.post("/api/ai/recommend")
def ai_recommend():
    """Desk AI recommends PAPER settings from recent paper results + backtest. Pending only."""
    from desk import desk_chat
    from desk import settings as desk_settings
    why = desk_chat.LIMITER.take()
    if why:
        return JSONResponse({"ok": False, "error": why}, status_code=429)
    st = agent.status()
    try:
        bt = backtest()
    except Exception:
        bt = None
    out = desk_chat.answer(desk_chat.RECOMMEND_Q, st, None, settings=desk_settings.compact_for_ai(),
                           perf=desk_chat.performance(st, bt))
    out = _with_proposals(out, "recommend")
    _remember_chat("user", "Recommend settings")
    if out.get("reply"):
        _remember_chat("ai", out.get("reply"), out)
    return out


def _who(request: Request) -> str:
    dev = _device(request)
    if dev:
        return f"device:{dev['name']}"[:40]
    return "operator key" if DESK_TOKEN else "open desk"


@app.get("/api/settings")
def get_settings():
    from desk import settings as desk_settings
    return desk_settings.snapshot()


@app.get("/api/proposals")
def get_proposals():
    from desk import settings as desk_settings
    return desk_settings.listing()


@app.post("/api/proposals")
def new_proposal(payload: dict = Body(...)):
    """Operator-made proposal from a preset button. Still needs Confirm."""
    from desk import settings as desk_settings
    payload = payload or {}
    values = payload.get("values")
    if isinstance(values, dict) and values:
        out = desk_settings.create_values(values, str(payload.get("title") or "Operator change")[:80])
    else:
        preset = str(payload.get("preset", ""))[:20]
        out = desk_settings.create_preset(preset, "operator")
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


@app.post("/api/proposals/{pid}/confirm")
def confirm_proposal(pid: str, request: Request):
    from desk import settings as desk_settings
    who = _who(request)
    out = desk_settings.confirm(pid[:20], who)
    if out.get("ok"):
        agent.emit("warn", "settings confirmed by " + who + ": " + ", ".join(
            f"{a['label']} {a['from']} -> {a['to']}" for a in out["applied"]), stage="settings")
    return JSONResponse(out, status_code=200 if out.get("ok") else out.pop("code", 400))


@app.post("/api/proposals/{pid}/reject")
def reject_proposal(pid: str, request: Request):
    from desk import settings as desk_settings
    who = _who(request)
    out = desk_settings.reject(pid[:20], who)
    if out.get("ok"):
        agent.emit("info", f"settings proposal {out['proposal']['id']} rejected by {who}", stage="settings")
    return JSONResponse(out, status_code=200 if out.get("ok") else out.pop("code", 400))


@app.get("/api/mode")
def get_mode():
    return agent.mode_info()


@app.post("/api/mode")
def set_mode(payload: dict = Body(...)):
    return agent.set_mode(str(payload.get("mode", "")), str(payload.get("confirm", "")))


@app.post("/api/agent/start")
def start():
    return agent.start()


@app.post("/api/agent/stop")
def stop():
    return agent.stop()


@app.post("/api/risk/continue")
def risk_continue():
    """Operator Continue after the 3% daily stop: PAPER only, today only, logged.
    Behind the same key / paired-device gate as Start and Pause."""
    r = agent.continue_today()
    return JSONResponse(r, status_code=200 if r.get("ok") else 409)


@app.post("/api/agent/step")
def step(demo: int = 0):
    return agent.run_once(force_demo=bool(demo))


@app.post("/api/agent/reset")
def reset():
    return agent.reset()


@app.post("/api/kill")
def kill():
    KILL_FILE.write_text("stop")
    agent.stop()
    return {"ok": True, "message": "kill switch tripped, agent stopped"}


@app.delete("/api/kill")
def clear_kill():
    if KILL_FILE.exists():
        KILL_FILE.unlink()
    return {"ok": True, "message": "kill switch cleared"}


if not DESK_TOKEN:
    print("[desk] WARNING: DESK_TOKEN is not set \u2014 this desk is OPEN to anyone who can reach the port.")
else:
    print(f"[desk] locked: key required, {devices.count()} device(s) paired.")
from desk import brain as _brain
from desk import live as _live
_ok, _why = _live.ready()
print(f"[desk] execution: paper by default. live signing: "
      f"{'available' if _ok else 'unavailable'} ({_why})")
print(f"[desk] brain: {_brain.describe()}" + (" (free)" if _brain.health()["free"] else ""))
