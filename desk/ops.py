"""Journal, bench, alerts, kill switch. The boring seats that keep the desk honest."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import BENCH_MINUTES, DEFAULT_BENCH

def data_dir() -> Path:
    """Runtime files. Set DESK_DATA_DIR to a mounted disk so deploys do not wipe the journal."""
    raw = (os.environ.get("DESK_DATA_DIR") or "").strip()
    return Path(raw) if raw else Path(".")


def data_file(name: str) -> Path:
    return data_dir() / name


JOURNAL = Path("journal.jsonl")  # default; journal() uses data_file so DESK_DATA_DIR is honoured
KILL_FILE = Path("KILL")


# ---------- journal: every decision, including every rejection ----------
def journal(kind: str, **fields) -> dict:
    row = {"time": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
    try:
        path = data_file("journal.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        # The journal is an audit trail, not a gate. A read-only volume must not
        # stop the desk from trading.
        print(f"[desk] journal write failed ({e}); decision not persisted")
    try:
        from desk.store import append_journal
        append_journal(row)
    except Exception:
        pass
    return row


# ---------- bench: a rejected token sits out, for as long as its failure deserves ----------
class Bench:
    """Tokens that failed a seat sit out for a while, so the desk does not
    re-litigate the same coin every 45 seconds.

    The bench is an optimisation, never a dependency. If the database cannot be
    written (read-only volume, deleted file, locked by another process) the desk
    must keep trading with an empty bench rather than abort the cycle. A bench
    write used to raise, which killed the whole cycle *before* any token could
    reach the analyst -- the desk looked alive but never traded.
    """

    def __init__(self, path: str = "desk.db"):
        self.path = path
        self.ok = True
        self.memory: dict[str, tuple[str, float]] = {}
        self.db = None
        try:
            self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS bench(mint TEXT PRIMARY KEY, reason TEXT, until REAL)"
            )
            self.db.commit()
        except Exception as e:  # pragma: no cover - depends on the filesystem
            print(f"[desk] bench unavailable ({e}); running with an in-memory bench")
            self.ok = False
            self.db = None

    def _fallback(self, e: Exception) -> None:
        if self.ok:
            print(f"[desk] bench write failed ({e}); falling back to memory")
        self.ok = False

    def benched(self, mint: str) -> bool:
        now = time.time()
        if self.ok and self.db is not None:
            try:
                r = self.db.execute("SELECT until FROM bench WHERE mint=?", (mint,)).fetchone()
                return bool(r and r[0] > now)
            except Exception as e:
                self._fallback(e)
        r = self.memory.get(mint)
        return bool(r and r[1] > now)

    def sit(self, mint: str, reason: str):
        mins = BENCH_MINUTES.get(reason, DEFAULT_BENCH)
        until = time.time() + mins * 60
        if self.ok and self.db is not None:
            try:
                self.db.execute("INSERT OR REPLACE INTO bench VALUES (?,?,?)",
                                (mint, reason, until))
                self.db.commit()
                return
            except Exception as e:
                self._fallback(e)
        self.memory[mint] = (reason, until)

    def clear(self):
        self.memory.clear()
        if self.ok and self.db is not None:
            try:
                self.db.execute("DELETE FROM bench")
                self.db.commit()
            except Exception as e:
                self._fallback(e)


# ---------- alerts: Telegram if configured, stdout otherwise ----------
def alert(text: str):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    print(text)
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text,
                                "disable_web_page_preview": True}, timeout=10)
        except Exception as e:
            print(f"telegram failed: {e}")


# ---------- kill switch: create a file named KILL and the desk stops ----------
def killed() -> bool:
    return KILL_FILE.exists()


def load_env(path: str = ".env"):
    """Tiny .env loader so the desk has no extra dependency. Real env vars win.

    DESK_NO_DOTENV=1 disables it entirely. The test suite sets that flag: its
    isolation fixture strips live keys from os.environ, but app.py calls
    load_env() at import time, which read .env and put the operator's real
    GROQ_API_KEY straight back. Three tests that assert "no keys configured"
    then failed on any machine that actually had a .env -- green in CI, red on
    the owner's phone. The suite must never see the operator's secrets.
    """
    if (os.environ.get("DESK_NO_DOTENV") or "").strip() in ("1", "true", "yes"):
        return
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
