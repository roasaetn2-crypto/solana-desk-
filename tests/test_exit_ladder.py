"""The exit ladder is selectable, and the default must not move on its own.

scripts/ladder_replay.py showed the ladder choice barely moves win rate but moves
expectancy by ~3x. That makes the ladder worth switching -- but only deliberately,
so these tests pin the resolution order and the default.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def mt(monkeypatch):
    monkeypatch.delenv("BRAIN_EXIT_LADDER", raising=False)
    import desk.meme_tactics as mod
    importlib.reload(mod)
    return mod


def test_default_ladder_is_fast(mt):
    """The shipped default must stay `fast` so this feature is inert until chosen."""
    assert mt.DEFAULT_LADDER == "fast"
    assert mt.active_ladder()["tp1_at"] == 1.25
    assert mt.active_ladder()["tp2_at"] == 1.6


def test_every_ladder_has_the_same_shape(mt):
    """A ladder missing a key would blow up deep inside the exit planner."""
    required = {"stop_pct", "tp1_at", "tp1_frac", "tp2_at", "tp2_frac", "trail_pct"}
    for name, L in mt.LADDERS.items():
        assert set(L) == required, f"{name} has {set(L) ^ required}"


def test_patient_ladder_is_reachable(mt, monkeypatch):
    monkeypatch.setenv("BRAIN_EXIT_LADDER", "patient")
    L = mt.active_ladder()
    assert L["tp1_at"] == 2.0 and L["tp2_at"] == 3.0


def test_unknown_ladder_falls_back_to_default(mt, monkeypatch):
    """A typo in an env var must not disable exits -- fall back, never raise."""
    monkeypatch.setenv("BRAIN_EXIT_LADDER", "typo")
    assert mt.active_ladder() == mt.LADDERS[mt.DEFAULT_LADDER]


def test_env_is_case_and_space_insensitive(mt, monkeypatch):
    monkeypatch.setenv("BRAIN_EXIT_LADDER", "  PATIENT  ")
    assert mt.active_ladder()["tp1_at"] == 2.0


def test_exit_overrides_is_empty_when_pullback_is_off(mt, monkeypatch):
    monkeypatch.setitem(mt.MEME, "pullback_entry", False)
    assert mt.exit_overrides() == {}


def test_exit_overrides_follows_the_selected_ladder(mt, monkeypatch):
    monkeypatch.setitem(mt.MEME, "pullback_entry", True)
    monkeypatch.setenv("BRAIN_EXIT_LADDER", "single")
    assert mt.exit_overrides()["tp1_at"] == 1.35
