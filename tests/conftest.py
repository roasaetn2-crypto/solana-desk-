"""Shared pytest hooks; also loaded only under pytest, not unittest.

Two jobs:

1. Importing isolate_env drops live API keys for the pytest process, so a real
   .env on the developer's machine cannot change what the tests see.
2. Turn OFF the local-Ollama probe. The advisor probes localhost:11434 to decide
   whether a local model is available, and that probe changes the provider order
   (a free local model goes first). On a machine that actually runs `ollama serve`
   that would flip 20+ unrelated assertions, so the suite must not depend on it.
   Tests that want local mode set OLLAMA_BASE_URL, which short-circuits the probe,
   or pop DESK_NO_LOCAL_OLLAMA to exercise the probe itself.
"""
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate_env  # noqa: E402,F401

os.environ["DESK_NO_LOCAL_OLLAMA"] = "1"


def pytest_configure(config):  # noqa: ARG001
    """Clear any probe result cached before the env above was set."""
    try:
        from desk import brain_advisor as ba
    except Exception:      # never fail collection over this
        return
    ba._local_probe.update(at=0.0, up=False, host="")
