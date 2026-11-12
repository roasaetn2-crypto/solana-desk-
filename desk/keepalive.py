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
import urllib.request

INTERVAL_S = 600
_stop = threading.Event()
_thread: threading.Thread | None = None


def _url() -> str | None:
    if os.environ.get("SELF_PING", "1") == "0":
        return None
    base = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    return f"{base}/api/health" if base.startswith("http") else None


def _run(url: str) -> None:
    while not _stop.wait(INTERVAL_S):
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "desk-keepalive"}), timeout=30).read(64)
        except Exception as e:  # never let the pinger die
            print(f"[desk] keepalive: ping failed ({type(e).__name__})", flush=True)


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
