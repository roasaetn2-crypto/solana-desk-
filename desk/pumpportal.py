"""PumpPortal new-token feed. ONE websocket, free, no api-key, no trade streams.

  wss://pumpportal.fun/api/data    {"method": "subscribeNewToken"}

The only subscription is subscribeNewToken. This module never sends any other
method, never uses an api-key or the paid trade streams, and never touches a
wallet. It runs in one daemon thread with exactly one connection at a time.

The thread starts with the web process (and again if the trading loop starts),
reconnects with capped exponential backoff (5 s up to 5 min) and never gives
up, and reconnects if the socket goes stale (no messages for STALE_S seconds).

What it keeps (tiny, in memory): per new mint the launch time, creator wallet and
how much of the supply the creator bought in the launch transaction. Token names,
symbols and URIs are attacker-controlled text and are discarded unread.

What it is used for: flags on a ticket, and a list of freshly launched mints
(recent_mints) that the scanner hydrates with market stats from Jupiter's free
token API, so new launches stay visible when GeckoTerminal/DexScreener 429.
  dev_buy_big      creator bought >= 10% of supply in the launch tx   (HALVE)
  serial_launcher  creator launched >= 3 tokens in the last 6 hours   (HALVE)
Those only ever make a paper ticket smaller. It also gives an exact launch time.
"""
from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict, deque

from desk.netutil import offline

SUPPLY = 1_000_000_000          # pump.fun mints a fixed 1B supply
URL = "wss://pumpportal.fun/api/data"
SUB = {"method": "subscribeNewToken"}      # the ONLY message this module ever sends
MAX_MINTS = 3000
MAX_MSG_BYTES = 65_536
DEV_BUY_BIG = 0.10
SERIAL_N = 3
SERIAL_WINDOW_S = 6 * 3600
BACKOFF_START_S, BACKOFF_MAX_S = 5, 300
STALE_S = 120
RECV_TIMEOUT_S = 30

_lock = threading.Lock()
_mints: OrderedDict = OrderedDict()                 # mint -> {t, creator, dev_buy_pct}
_creators: dict[str, deque] = {}                    # creator -> launch timestamps
_state = {"connected": False, "received": 0, "last_msg": None, "last_activity": None,
          "reconnects": 0, "error": None, "enabled": False}
_thread: threading.Thread | None = None
_ws = None
_stop = threading.Event()


def _log(msg: str) -> None:
    print(f"pumpportal: {msg}", flush=True)


def _ws_mod():
    import websocket
    return websocket


def stale(now: float, last_activity: float | None, connected_at: float,
          window: float = STALE_S) -> bool:
    """True if the socket has been silent for `window` seconds."""
    anchor = last_activity if last_activity is not None else connected_at
    return now - anchor >= window


def status_label(h: dict | None = None) -> str:
    s = h if h is not None else health()
    if s.get("connected"):
        return "on"
    if s.get("enabled"):
        return "connecting"
    return "off"


def reset() -> None:
    with _lock:
        _mints.clear()
        _creators.clear()
        _state.update(connected=False, received=0, last_msg=None, last_activity=None,
                      reconnects=0, error=None)


def health() -> dict:
    with _lock:
        return {**_state, "tracked": len(_mints), "status": status_label(_state)}


def ingest(raw, now: float | None = None) -> bool:
    """Handle one websocket message. Pure apart from the in-memory tables. Never raises."""
    now = time.time() if now is None else now
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str) or len(raw) > MAX_MSG_BYTES:
            return False
        msg = json.loads(raw)
        if not isinstance(msg, dict) or msg.get("txType") != "create":
            return False
        mint, creator = msg.get("mint"), msg.get("traderPublicKey")
        if not isinstance(mint, str) or not isinstance(creator, str) or not (30 <= len(mint) <= 50):
            return False
        try:
            dev_pct = max(0.0, float(msg.get("initialBuy") or 0)) / SUPPLY
        except (TypeError, ValueError):
            dev_pct = 0.0
        with _lock:
            _mints[mint] = {"t": now, "creator": creator, "dev_buy_pct": round(dev_pct, 4)}
            _mints.move_to_end(mint)
            while len(_mints) > MAX_MINTS:
                _mints.popitem(last=False)
            dq = _creators.setdefault(creator, deque(maxlen=50))
            dq.append(now)
            if len(_creators) > MAX_MINTS:           # drop stale creators
                for c in [c for c, d in _creators.items() if not d or now - d[-1] > SERIAL_WINDOW_S]:
                    _creators.pop(c, None)
            _state["received"] += 1
            _state["last_msg"] = now
        return True
    except Exception:
        return False


def lookup(mint: str, now: float | None = None) -> dict:
    """Flags for a mint we saw launch. Unknown (available False) if we did not."""
    now = time.time() if now is None else now
    with _lock:
        row = _mints.get(mint)
        if not row:
            return {"available": False, "source": "pumpportal", "flags": [], "block": [],
                    "halve": [], "launched_at": None}
        recent = [t for t in _creators.get(row["creator"], ()) if now - t <= SERIAL_WINDOW_S]
    halve = []
    if row["dev_buy_pct"] >= DEV_BUY_BIG:
        halve.append("dev_buy_big")
    if len(recent) >= SERIAL_N:
        halve.append("serial_launcher")
    return {"available": True, "source": "pumpportal", "flags": halve, "block": [],
            "halve": halve, "launched_at": row["t"], "dev_buy_pct": row["dev_buy_pct"],
            "creator_launches_6h": len(recent)}


def recent_mints(now: float | None = None, min_age_s: float = 120.0,
                 max_age_s: float = 6 * 3600.0, limit: int = 100) -> list[str]:
    """Mints seen launching between min_age_s and max_age_s ago, newest first."""
    now = time.time() if now is None else now
    out: list[str] = []
    with _lock:
        for mint, row in reversed(_mints.items()):
            age = now - row["t"]
            if age < min_age_s:
                continue
            if age > max_age_s:
                break                     # OrderedDict is in arrival order
            out.append(mint)
            if len(out) >= limit:
                break
    return out


def _run() -> None:
    """Connect, subscribe, read. Never returns until stop() is called."""
    global _ws
    backoff = BACKOFF_START_S
    while not _stop.is_set():
        try:
            websocket = _ws_mod()
        except Exception as e:
            with _lock:
                _state["error"] = f"websocket-client missing ({type(e).__name__})"
            _log(f"websocket-client missing ({type(e).__name__}); retry in {backoff}s")
            _stop.wait(backoff)
            backoff = min(BACKOFF_MAX_S, backoff * 2)
            continue
        ws = None
        try:
            _log("connecting")
            ws = websocket.create_connection(URL, timeout=20)
            ws.settimeout(RECV_TIMEOUT_S)
            ws.send(json.dumps(SUB))
            connected_at = time.time()
            with _lock:
                _ws = ws
                _state.update(connected=True, error=None)
            _log("connected, subscribed subscribeNewToken")
            backoff = BACKOFF_START_S
            while not _stop.is_set():
                try:
                    raw = ws.recv()
                    now = time.time()
                    with _lock:
                        _state["last_activity"] = now
                    ingest(raw)
                except websocket.WebSocketTimeoutException:
                    now = time.time()
                    with _lock:
                        last = _state.get("last_activity")
                    if stale(now, last, connected_at):
                        _log(f"stale ({STALE_S}s with no messages), reconnecting")
                        with _lock:
                            _state["error"] = "stale"
                        break
                    try:
                        ws.ping()                      # quiet period: keepalive, not a new message type
                    except Exception as e:
                        _log(f"ping failed ({type(e).__name__})")
                        with _lock:
                            _state["error"] = type(e).__name__
                        break
        except Exception as e:
            with _lock:
                _state["error"] = type(e).__name__
            _log(f"disconnected ({type(e).__name__})")
        finally:
            with _lock:
                _state["connected"] = False
                if _ws is ws:
                    _ws = None
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
        if _stop.is_set():
            break
        with _lock:
            _state["reconnects"] += 1
        _log(f"reconnect in {backoff}s")
        _stop.wait(backoff)
        backoff = min(BACKOFF_MAX_S, backoff * 2)


def start() -> bool:
    """Start the single feed thread (idempotent). No-op when DESK_OFFLINE=1."""
    global _thread
    if offline():
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _state["enabled"] = True
        _thread = threading.Thread(target=_run, name="pumpportal", daemon=True)
        _thread.start()
    _log("thread started")
    return True


def stop() -> None:
    global _ws
    _stop.set()
    with _lock:
        _state["enabled"] = False
        ws = _ws
        _ws = None
    try:
        if ws:
            ws.close()
    except Exception:
        pass
