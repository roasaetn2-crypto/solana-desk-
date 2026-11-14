"""The ladder probe must never lie about where the setting came from.

The whole point of scripts/which_ladder.py is to answer "why is my ladder not
the one I set in config.py", so the precedence it reports has to match what
meme_tactics.active_ladder() actually does. These tests pin the two together.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import meme_tactics  # noqa: E402


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "which_ladder", ROOT / "scripts" / "which_ladder.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def probe():
    return _load_probe()


def test_env_beats_config(probe, monkeypatch):
    monkeypatch.setenv("BRAIN_EXIT_LADDER", "slow")
    r = probe.resolve()
    assert r["source"].startswith("env")
    assert r["requested"] == "slow"
    assert r["ladder"] == meme_tactics.LADDERS["slow"]
    assert r["ladder"] == meme_tactics.active_ladder()


def test_config_used_when_env_is_blank(probe, monkeypatch):
    monkeypatch.delenv("BRAIN_EXIT_LADDER", raising=False)
    r = probe.resolve()
    assert r["source"].startswith("config.py")
    assert r["ladder"] == meme_tactics.active_ladder()


def test_unknown_name_is_reported_not_hidden(probe, monkeypatch):
    monkeypatch.setenv("BRAIN_EXIT_LADDER", "turbo")
    r = probe.resolve()
    assert r["valid"] is False
    assert r["fallback_used"] is True
    assert r["name"] == meme_tactics.DEFAULT_LADDER
    assert r["ladder"] == meme_tactics.LADDERS[meme_tactics.DEFAULT_LADDER]


def test_probe_matches_resolver_for_every_known_ladder(probe, monkeypatch):
    for name in meme_tactics.LADDERS:
        monkeypatch.setenv("BRAIN_EXIT_LADDER", name)
        r = probe.resolve()
        assert r["valid"] is True
        assert r["ladder"] == meme_tactics.active_ladder(), name


def test_ladder_info_reports_env_source(monkeypatch):
    monkeypatch.setenv("BRAIN_EXIT_LADDER", "single")
    info = meme_tactics.ladder_info()
    assert info["source"] == "env"
    assert info["name"] == "single"
    assert info["fallback_used"] is False


def test_ladder_info_falls_back_to_default_source(monkeypatch):
    monkeypatch.delenv("BRAIN_EXIT_LADDER", raising=False)
    monkeypatch.setitem(meme_tactics.MEME, "exit_ladder", "")
    info = meme_tactics.ladder_info()
    assert info["source"] == "default"
    assert info["name"] == meme_tactics.DEFAULT_LADDER


def test_ladder_info_never_returns_a_mutable_reference(monkeypatch):
    """Callers mutate the dict (exits.py copies it into overrides)."""
    monkeypatch.setenv("BRAIN_EXIT_LADDER", "patient")
    a = meme_tactics.ladder_info()["ladder"]
    a["tp1_at"] = 99.0
    assert meme_tactics.LADDERS["patient"]["tp1_at"] == 2.0
