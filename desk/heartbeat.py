"""Healthchecks.io heartbeat. Optional: does nothing unless HEALTHCHECKS_URL is set.

Healthchecks.io (free Hobbyist plan) emails you when pings stop, so an outage is
reported even if the desk, the box and the dashboard are all down.

  * The ping URL is a secret. It is read from the environment, never logged,
    never put in the state API, and failures are reported without it.
  * One background daemon thread, one GET per minute, hard timeout, no retries.
  * Loop running and cycling      -> ping the success URL
  * Loop running but stalled      -> ping <url>/fail (an alert, not silence)
  * Loop paused or not started    -> no ping, so a stopped desk is noticed.
    Set HEALTHCHECKS_PING_WHEN_PAUSED=1 to keep pinging while paused.
"""
from __future__ import annotations

import os
import threading
import time

import requests

INTERVAL_S = 60
TIMEOUT_S = 8
STALE_AFTER_S = 300          # running, but no cycle finished in 5 minutes

_state = {"configured": False, "last_ok": None, "last_status": None, "fails": 0}
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()


def url() -> str:
    u = os.environ.get("HEALTHCHECKS_URL", "").strip()
    return u if u.startswith("https://") else ""


def configured() -> bool:
    return bool(url())


def health() -> dict:
    """Safe to show: never contains the URL."""
    with _lock:
        return {**_state, "configured": configured()}


def decide(running: bool, last_cycle_age_s: float | None, ping_when_paused: bool = False) -> str:
    """'ok' | 'fail' | 'skip'. Pure, so it can be tested."""
    if not running:
        return "ok" if ping_when_paused else "skip"
    if last_cycle_age_s is not None and last_cycle_age_s > STALE_AFTER_S:
        return "fail"
    return "ok"


def ping_once(kind: str, session: requests.Session | None = None) -> bool:
    base = url()
    if not base or kind == "skip":
        return False
    target = base.rstrip("/") + ("/fail" if kind == "fail" else "")
    try:
        r = (session or requests).get(target, timeout=TIMEOUT_S,
                                      headers={"User-Agent": "solana-desk-heartbeat/1.0"})
        ok = 200 <= r.status_code < 300
        status = r.status_code
    except Exception as e:      # message may embed the URL: keep only the type
        ok, status = False, type(e).__name__
    with _lock:
        _state["last_status"] = status
        if ok:
            _state["last_ok"] = time.time()
        else:
            _state["fails"] += 1
    return ok


def _run(status_fn) -> None:
    while not _stop.wait(INTERVAL_S):
        try:
            running, age = status_fn()
            kind = decide(running, age, os.environ.get("HEALTHCHECKS_PING_WHEN_PAUSED") == "1")
            ping_once(kind)
        except Exception:
            pass


def start(status_fn) -> bool:
    """Start the thread once. status_fn() -> (running: bool, seconds_since_last_cycle|None)."""
    global _thread
    if not configured():
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_run, args=(status_fn,), name="healthchecks",
                                   daemon=True)
        _thread.start()
    return True
