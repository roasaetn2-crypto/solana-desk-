"""Live trade stream for OPEN POSITIONS: a price tick the moment a held coin trades.

REST polling marks a position every 15-60 seconds at best, and goes blind while DexScreener or
GeckoTerminal rate-limit the host. PumpPortal's Data API streams every trade on a pump.fun /
PumpSwap coin over one websocket (free, no api key, subject to rate limits):

    wss://pumpportal.fun/api/data
        {"method": "subscribeTokenTrade",   "keys": [mint, ...]}
        {"method": "unsubscribeTokenTrade", "keys": [mint, ...]}

This module subscribes ONLY to the mints the desk currently holds (<= MAX_MINTS) and does three
things with each trade: remembers the latest price (scanner.quote_many overlays it on the REST
quote when it is fresh), records a chart tick (so the 15s/30s/1m candles fill without any HTTP
call), and counts it for /api/health. It never trades, never sends any other method, never uses an
api key and never touches a wallet. Token names and URIs in messages are discarded unread.

It is OFF by default (FEED["trade_stream"]). While it is off, or while nothing is held, no
connection is open. It runs in one daemon thread, reconnects with capped exponential backoff,
re-subscribes on every reconnect, keeps the socket alive with protocol pings, and syncs its
subscriptions with the open positions every RECV_TIMEOUT_S seconds.

Coverage is limited on purpose: only coins pump.fun's program trades. A held coin that is not one
simply never produces a tick and keeps being priced by the REST path, exactly as before.
"""
from __future__ import annotations

import json
import threading
import time

from config import FEED

URL = "wss://pumpportal.fun/api/data"
SUPPLY = 1_000_000_000              # pump.fun mints a fixed 1B supply (same constant as pumpportal.py)
MAX_MINTS = 30
MAX_MSG_BYTES = 65_536
RECV_TIMEOUT_S = 2.0                # also how often subscriptions are re-synced
PING_EVERY_S = 25.0
DEAD_S = 600.0                      # no message at all while holding coins -> reconnect
BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 300.0
CHUNK = 20                          # keys per subscribe message

_lock = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_wanted: set[str] = set()
_last: dict[str, tuple[float, float]] = {}      # mint -> (price_usd, unix time)
_state = {"connected": False, "received": 0, "ticks": 0, "reconnects": 0, "error": None,
          "last_msg": None, "subscribed": 0}


def _log(msg: str) -> None:
    print(f"live_feed: {msg}", flush=True)


def enabled() -> bool:
    return bool(FEED.get("trade_stream", False))


def reset() -> None:
    with _lock:
        _wanted.clear()
        _last.clear()
        _state.update(connected=False, received=0, ticks=0, reconnects=0, error=None,
                      last_msg=None, subscribed=0)


def health() -> dict:
    with _lock:
        return dict(_state, enabled=enabled(), wanted=len(_wanted),
                    alive=bool(_thread and _thread.is_alive()))


def set_mints(mints) -> None:
    """The desk's open positions. Called every cycle; cheap."""
    keep = []
    for m in mints or []:
        if isinstance(m, str) and 30 <= len(m) <= 50 and not m.startswith("DEMO") and m not in keep:
            keep.append(m)
    with _lock:
        _wanted.clear()
        _wanted.update(keep[:MAX_MINTS])
        for m in [m for m in _last if m not in _wanted]:
            _last.pop(m, None)


def last_price(mint: str, now: float | None = None, max_age_s: float | None = None) -> float | None:
    """Latest streamed price (USD) if it is fresh enough, else None. Never raises."""
    if not enabled():
        return None
    now = time.time() if now is None else now
    age = float(FEED.get("price_max_age_s", 15)) if max_age_s is None else max_age_s
    with _lock:
        row = _last.get(mint)
    if not row or now - row[1] > age:
        return None
    return row[0]


def _price_sol(msg: dict) -> float | None:
    """SOL per token from one trade message: market cap / supply, else the trade's own ratio."""
    try:
        cap = float(msg.get("marketCapSol") or 0)
        if cap > 0:
            return cap / SUPPLY
        sol, tok = float(msg.get("solAmount") or 0), float(msg.get("tokenAmount") or 0)
        if sol > 0 and tok > 0:
            return sol / tok
        vs, vt = float(msg.get("vSolInBondingCurve") or 0), float(msg.get("vTokensInBondingCurve") or 0)
        if vs > 0 and vt > 0:
            return vs / vt
    except (TypeError, ValueError):
        pass
    return None


def ingest(raw, now: float | None = None) -> bool:
    """Handle one websocket message. Never raises. True when it produced a price tick."""
    now = time.time() if now is None else now
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str) or len(raw) > MAX_MSG_BYTES:
            return False
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            return False
        with _lock:
            _state["received"] += 1
            _state["last_msg"] = now
        mint = msg.get("mint")
        if msg.get("txType") not in ("buy", "sell") or not isinstance(mint, str):
            return False
        with _lock:
            if mint not in _wanted:
                return False                      # only coins we hold
        psol = _price_sol(msg)
        if psol is None or not (0 < psol < 1e6):
            return False
        from desk import chart, fees
        sol_usd = float(fees.sol_price_usd())
        if sol_usd <= 0:
            return False
        price = psol * sol_usd
        try:
            vol_usd = max(0.0, float(msg.get("solAmount") or 0)) * sol_usd
        except (TypeError, ValueError):
            vol_usd = 0.0
        chart.record_tick(mint, price, vol_usd, ts=now)
        with _lock:
            _last[mint] = (price, now)
            _state["ticks"] += 1
        return True
    except Exception:
        return False


def _ws_mod():
    import websocket                                   # the package is `websocket-client`
    return websocket


def _send(ws, method: str, keys: list[str]) -> None:
    for i in range(0, len(keys), CHUNK):
        ws.send(json.dumps({"method": method, "keys": keys[i:i + CHUNK]}))


def _sync(ws, subscribed: set[str]) -> None:
    """Make this connection's subscriptions match the open positions."""
    with _lock:
        want = set(_wanted)
    add, drop = sorted(want - subscribed), sorted(subscribed - want)
    if add:
        _send(ws, "subscribeTokenTrade", add)
        subscribed.update(add)
    if drop:
        _send(ws, "unsubscribeTokenTrade", drop)
        subscribed.difference_update(drop)
    with _lock:
        _state["subscribed"] = len(subscribed)


def _should_connect() -> bool:
    with _lock:
        return enabled() and bool(_wanted)


def _run() -> None:
    backoff = BACKOFF_START_S
    while not _stop.is_set():
        if not _should_connect():
            _stop.wait(2.0)                            # idle: no connection while off / nothing held
            continue
        try:
            websocket = _ws_mod()
        except Exception as e:
            with _lock:
                _state["error"] = f"websocket-client missing ({type(e).__name__})"
            _log("websocket-client missing: pip install websocket-client")
            _stop.wait(backoff)
            backoff = min(BACKOFF_MAX_S, backoff * 2)
            continue
        ws = None
        subscribed: set[str] = set()
        try:
            _log("connecting")
            ws = websocket.create_connection(URL, timeout=20)
            ws.settimeout(RECV_TIMEOUT_S)
            now = time.time()
            last_activity, last_ping = now, now
            with _lock:
                _state.update(connected=True, error=None)
            _sync(ws, subscribed)
            _log(f"connected, watching {len(subscribed)} held coin(s)")
            backoff = BACKOFF_START_S
            while not _stop.is_set():
                if not _should_connect():
                    break                              # switched off, or the book is empty: hang up
                try:
                    raw = ws.recv()
                    last_activity = time.time()
                    ingest(raw, last_activity)
                except websocket.WebSocketTimeoutException:
                    pass
                now = time.time()
                _sync(ws, subscribed)                  # picks up new / closed positions
                if now - last_ping >= PING_EVERY_S:
                    ws.ping()                          # protocol keepalive, not a new message type
                    last_ping = now
                if subscribed and now - last_activity > DEAD_S:
                    _log(f"no messages for {DEAD_S:.0f}s, reconnecting")
                    with _lock:
                        _state["error"] = "stale"
                    break
        except Exception as e:
            with _lock:
                _state["error"] = type(e).__name__
            _log(f"disconnected ({type(e).__name__})")
        finally:
            with _lock:
                _state["connected"] = False
                _state["subscribed"] = 0
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
        if _stop.is_set():
            break
        if not _should_connect():
            continue                                   # went idle on purpose: no backoff, no counter
        with _lock:
            _state["reconnects"] += 1
        _log(f"reconnect in {backoff:.0f}s")
        _stop.wait(backoff)
        backoff = min(BACKOFF_MAX_S, backoff * 2)


def start() -> bool:
    """Start the daemon thread once. Safe to call repeatedly (it restarts a dead thread)."""
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return False
        _stop.clear()
        _thread = threading.Thread(target=_run, name="live-feed", daemon=True)
        _thread.start()
        return True


def start_in_background() -> threading.Thread | None:
    start()
    return _thread


def stop() -> None:
    _stop.set()
