"""The Analyst seat (desk/brain.py) is a different module from the advisory
second opinion (desk/brain_advisor.py). These tests pin the two things that made
the dashboard read "heuristic (no model configured)" even with a valid Groq key:

  1. a retired default model name (llama-3.3-70b-versatile now answers 404), and
  2. cloud keys deliberately not switching the seat on by themselves.
"""
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk import brain


class TestModelName:
    def test_groq_default_is_not_the_retired_llama(self):
        # Groq retired llama-3.3-70b-versatile; a live call answers 404, so the
        # analyst seat failed on every request even with a good key.
        assert brain.PROVIDERS["groq"].model != "llama-3.3-70b-versatile"

    def test_groq_default_matches_the_advisory_layer(self):
        from desk import brain_advisor
        assert brain.PROVIDERS["groq"].model == brain_advisor.GROQ_MODEL

    def test_braingroqmodel_overrides_both_seats(self):
        with mock.patch.dict(os.environ, {"BRAIN_GROQ_MODEL": "openai/gpt-oss-20b"}):
            import importlib
            import desk.brain_advisor as ba
            import desk.brain as b
            importlib.reload(ba)
            importlib.reload(b)
            assert b.PROVIDERS["groq"].model == "openai/gpt-oss-20b"
            assert ba.GROQ_MODEL == "openai/gpt-oss-20b"
        import importlib
        import desk.brain_advisor as ba
        import desk.brain as b
        importlib.reload(ba)
        importlib.reload(b)


class TestSeatSelection:
    def test_a_cloud_key_alone_does_not_switch_the_seat(self):
        # Deliberate: the analyst's score gates trades, so a key must not change it.
        env = {k: v for k, v in os.environ.items()
               if k not in ("DESK_BRAIN", "DESK_BRAIN_AUTODETECT")}
        env["GROQ_API_KEY"] = "gsk_test_key_1234567890"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(brain, "_ollama_up", return_value=False):
            assert brain.active() == "heuristic"
            assert "no model configured" in brain.describe()

    def test_desk_brain_groq_switches_the_seat(self):
        with mock.patch.dict(os.environ, {"DESK_BRAIN": "groq",
                                          "GROQ_API_KEY": "gsk_test_key_1234567890"}), \
                mock.patch.object(brain, "_ollama_up", return_value=False):
            assert brain.active() == "groq"
            assert brain.describe().startswith("Groq")

    def test_autodetect_opts_in(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DESK_BRAIN",)}
        env["DESK_BRAIN_AUTODETECT"] = "1"
        env["GROQ_API_KEY"] = "gsk_test_key_1234567890"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(brain, "_ollama_up", return_value=False):
            assert brain.active() == "groq"
