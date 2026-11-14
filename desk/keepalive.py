"""Self keep-alive for Render's free plan.

Render spins a free web service down after ~15 min with no inbound HTTP, which
stops the paper loop until something wakes it. The box pinger is unreliable
(the box sleeps), so the service pings its own public URL every 10 minutes.
Uses RENDER_EXTERNAL_URL (set by Render); does nothing when it is absent.
Set SELF_PING=0 to turn it off.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request

INTERVAL_S = 300  # 5 min: well inside Render's ~15 min idle window, so one missed ping cannot sleep the box
_stop = threading.Event()
_thread: threading.Thread | None = None
_last: dict = {"at": None, "ok": None, "error": None, "pings": 0, "fails": 0}


def _url() -> str | None:
    if os.environ.get("SELF_PING", "1") == "0":
        return None
    base = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    return f"{base}/api/health" if base.startswith("http") else None


def _run(url: str) -> None:
    while not _stop.wait(INTERVAL_S):
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "desk-keepalive"}), timeout=30).read(64)
            _last.update(at=time.time(), ok=True, error=None, pings=_last["pings"] + 1)
        except Exception as e:  # never let the pinger die
            _last.update(at=time.time(), ok=False, error=type(e).__name__,
                         pings=_last["pings"] + 1, fails=_last["fails"] + 1)
            print(f"[desk] keepalive: ping failed ({type(e).__name__})", flush=True)


def status() -> dict:
    """What the self-pinger is doing, and why it is off if it is off.

    A self-pinger can only keep a live box awake -- it cannot wake a sleeping
    one, because the thread dies with the process. So this reports the reason
    it is disabled, which is the difference between "you are protected" and
    "you are protected until the first sleep".
    """
    url = _url()
    if url:
        why = None
    elif os.environ.get("SELF_PING", "1") == "0":
        why = "SELF_PING=0"
    else:
        why = "no RENDER_EXTERNAL_URL (not on Render, or the var is missing)"
    return {
        "on": bool(url),
        "off_reason": why,
        "interval_s": INTERVAL_S,
        "url": url,
        "last_at": _last["at"],
        "last_ok": _last["ok"],
        "last_error": _last["error"],
        "pings": _last["pings"],
        "fails": _last["fails"],
    }


def start() -> bool:
    global _thread
    url = _url()
    if not url or (_thread and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(url,), name="keepalive", daemon=True)
    _thread.start()
    print(f"[desk] keepalive: self-ping every {INTERVAL_S // 60} min", flush=True)
    return True


def stop() -> None:
    _stop.set()
