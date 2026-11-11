"""Small shared helpers for the optional free data sources.

Every external lookup in the signal modules goes through the same three rules:
a hard timeout, a cache (including a short negative cache), and a rate limiter
that says "skip" instead of sleeping. A skipped or failed lookup becomes an
"unknown" answer and the desk carries on. Nothing here raises into the loop.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

TIMEOUT_S = 8


class SlidingLimiter:
    """At most `limit` calls in any `window_s` seconds. allow() never blocks."""

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = int(limit)
        self.window_s = float(window_s)
        self._hits: deque = deque()
        self._lock = threading.Lock()

    def allow(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            while self._hits and now - self._hits[0] >= self.window_s:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                return False
            self._hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class TTLCache:
    """Bounded cache with a per-entry expiry. Thread-safe."""

    def __init__(self, max_items: int = 512):
        self.max_items = max_items
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, now: float | None = None):
        """Return (hit, value)."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._d.get(key)
            if row is None:
                return False, None
            if row[0] <= now:
                del self._d[key]
                return False, None
            self._d.move_to_end(key)
            return True, row[1]

    def set(self, key, value, ttl_s: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._d[key] = (now + ttl_s, value)
            self._d.move_to_end(key)
            while len(self._d) > self.max_items:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


def unknown(source: str, why: str) -> dict:
    """The fail-open answer: no flags, no cuts, marked unavailable."""
    return {"available": False, "source": source, "why": why,
            "flags": [], "block": [], "halve": []}


def offline() -> bool:
    """DESK_OFFLINE=1 turns every optional lookup into 'unknown' (tests, air-gapped runs)."""
    import os
    return os.environ.get("DESK_OFFLINE", "").strip() == "1"
