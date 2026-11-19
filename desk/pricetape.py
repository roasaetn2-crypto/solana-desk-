"""Price tape: a small, bounded, in-memory record of recent prices per mint (PAPER gates only).

Fed for free by the Jupiter rows the scanner already fetches (discovery, the PumpPortal hydrate,
finalist refreshes), so a coin has a price history even while GeckoTerminal is rate-limited. It
holds prices only, no volume. The entry trigger and the dip reader turn it into 1m bars when
GeckoTerminal candles are missing. Nothing here reads or writes anything outside memory.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

MAX_MINTS = 800
PER_MINT = 240                 # about 2 hours at one hydrate a minute
MIN_GAP_S = 5.0                # ignore a repeat inside this gap

_lock = threading.Lock()
_tape: "OrderedDict[str, deque]" = OrderedDict()


def reset() -> None:
    with _lock:
        _tape.clear()


def record(mint, price, ts: float | None = None) -> bool:
    if not mint or price is None:
        return False
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    if not (px > 0) or px != px or px == float("inf"):
        return False
    ts = time.time() if ts is None else float(ts)
    with _lock:
        dq = _tape.get(mint)
        if dq is None:
            dq = _tape[mint] = deque(maxlen=PER_MINT)
        else:
            _tape.move_to_end(mint)
            if dq and ts - dq[-1][0] < MIN_GAP_S:
                return False
        dq.append((ts, px))
        while len(_tape) > MAX_MINTS:
            _tape.popitem(last=False)
    return True


def ticks(mint: str) -> list[tuple[float, float, float]]:
    """(ts, price, 0.0) rows, oldest first. Volume is always 0: the tape is price only."""
    with _lock:
        return [(t, p, 0.0) for t, p in (_tape.get(mint) or ())]


def size() -> int:
    with _lock:
        return len(_tape)


def merged_ticks(mint: str) -> list[tuple[float, float, float]]:
    """The desk's own quote ticks (chart) plus the Jupiter tape, oldest first. Volume is not
    meaningful on either (quote ticks hold a rolling 5m figure), so callers must ignore it."""
    from desk import chart
    rows = list(chart.ticks(mint)) + ticks(mint)
    rows.sort(key=lambda r: r[0])
    return rows


def candles_1m(mint: str) -> list[dict]:
    """Price-only 1m candles (v is 0.0) from the merged ticks. Silent minutes are NOT filled here."""
    from desk import chart
    bars = chart.bucket_ticks([(t, p, 0.0) for t, p, _ in merged_ticks(mint)], 60)
    for b in bars:
        b["v"] = 0.0
    return bars
