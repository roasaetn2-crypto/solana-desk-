"""Shared pytest hooks; also loaded only under pytest, not unittest."""
from pathlib import Path
import sys

# Importing isolate_env drops live API keys for the pytest process.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate_env  # noqa: E402,F401
