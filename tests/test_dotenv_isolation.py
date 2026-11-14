"""The test suite must never read the operator's real .env.

This exists because it already went wrong. `tests/isolate_env.py` strips live
API keys from os.environ, but `app.py` calls `ops.load_env()` at import time,
which reads `.env` and puts them back. The result: three tests asserting "no
keys configured" passed in a bare sandbox and failed on the owner's phone,
where a .env with a real GROQ_API_KEY exists. A suite that is green only where
there are no secrets is a suite that lies.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import ops  # noqa: E402


class DotenvIsolation(unittest.TestCase):
    def test_suite_runs_with_dotenv_disabled(self):
        """conftest must have set the flag before any test body runs."""
        self.assertEqual(os.environ.get("DESK_NO_DOTENV"), "1")
        # And the real keys must be absent, not merely ignored by load_env.
        for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
            self.assertFalse(os.environ.get(k), f"{k} leaked into the test process")

    def test_load_env_is_a_noop_when_flagged(self):
        """A .env on disk must not be able to inject a key while flagged."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("GROQ_API_KEY=gsk_shouldneverappear\nOTHER_VAR=1\n")
            with mock.patch.dict(os.environ, {"DESK_NO_DOTENV": "1"}, clear=False):
                os.environ.pop("GROQ_API_KEY", None)
                os.environ.pop("OTHER_VAR", None)
                ops.load_env(str(p))
                self.assertIsNone(os.environ.get("GROQ_API_KEY"))
                self.assertIsNone(os.environ.get("OTHER_VAR"))

    def test_load_env_still_works_in_production(self):
        """The flag must be the only thing that disables it. The desk itself
        depends on load_env() reading .env at boot."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('GROQ_API_KEY="gsk_realkey123"\n')
            env = {k: v for k, v in os.environ.items() if k != "DESK_NO_DOTENV"}
            with mock.patch.dict(os.environ, env, clear=True):
                ops.load_env(str(p))
                self.assertEqual(os.environ.get("GROQ_API_KEY"), "gsk_realkey123")

    def test_real_env_wins_over_dotenv(self):
        """setdefault semantics: an exported var must not be overwritten."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("GROQ_API_KEY=gsk_fromfile\n")
            env = {k: v for k, v in os.environ.items() if k != "DESK_NO_DOTENV"}
            env["GROQ_API_KEY"] = "gsk_fromenv"
            with mock.patch.dict(os.environ, env, clear=True):
                ops.load_env(str(p))
                self.assertEqual(os.environ["GROQ_API_KEY"], "gsk_fromenv")


if __name__ == "__main__":
    unittest.main()
