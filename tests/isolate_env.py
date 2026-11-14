"""Tests never pick up live API keys from the box or Render env, and never write to
the real runtime files.

Without DESK_DATA_DIR the desk writes journal.jsonl, desk.db, paper_ledger.json and
the operator/settings state into the working directory. Running the suite from the
repo root therefore appended hundreds of fixture rows ("REALMINT1...", symbol GOOD)
to the same journal the live desk and the backtest read, which silently poisoned
every backtest with fake 0% win-rate trades. Point the whole suite at a throwaway
directory instead.
"""
import atexit
import os
import shutil
import tempfile

os.environ.setdefault("DESK_OFFLINE", "1")

# app.py calls ops.load_env() at import time. Popping the keys below is not
# enough: load_env() would read the operator's .env and put them back, so the
# "no keys configured" tests pass in a bare sandbox and fail on a real phone.
# This flag makes load_env() a no-op for the whole test process.
os.environ["DESK_NO_DOTENV"] = "1"

# CoinGecko Demo, Gemini, Groq and OpenRouter keys must never be used by tests;
# they mock HTTP and, when needed, set fake keys themselves.
for _name in ("COINGECKO_DEMO_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(_name, None)

# Never write into the repo: the journal and ledgers must stay the operator's.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="desk-test-data-")
os.environ["DESK_DATA_DIR"] = _TEST_DATA_DIR
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)
