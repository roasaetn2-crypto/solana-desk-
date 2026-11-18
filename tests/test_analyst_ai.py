"""PAPER analyst on a real model: provider choice, budgets, and fallback to the heuristic."""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from desk import analyst, analyst_ai, brain

TOKEN = {"symbol": "T", "liquidity_usd": 30_000, "mcap_usd": 100_000, "volume_h1": 60_000,
         "buys_h1": 200, "sells_h1": 100, "buyers_h1": 120, "change_h1": 20, "change_m5": 3,
         "age_minutes": 90}
GOOD = ('{"verdict":"valid_setup","score":8,"red_flags":[],"green_flags":["ok"],"shape":"crowd",'
        '"invalidation":"loses bid","sources":["liquidity_usd"]}')


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    for k in ("DESK_BRAIN", "DESK_BRAIN_AUTODETECT", "XAI_API_KEY",
              "GROQ_API_KEY", "BRAIN_ANALYST_AI"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BRAIN_ANALYST_AI_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(brain, "_ollama_up", lambda: False)
    analyst_ai._cooldown_until.clear()
    analyst_ai._last_error.clear()
    analyst_ai.new_cycle()
    yield


def _ai(monkeypatch, replies, calls=None):
    seq = list(replies)

    def fake(name, system, user):
        if calls is not None:
            calls.append(name)
        r = seq.pop(0) if seq else seq_last
        if isinstance(r, Exception):
            raise r
        return r
    seq_last = GOOD
    monkeypatch.setattr(analyst_ai, "_call", fake)


class TestSelection:
    def test_groq_when_only_groq_key(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        assert analyst_ai.providers() == ["groq"]
        assert "Groq" in analyst_ai.describe("paper")

    def test_xai_preferred_when_present(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        monkeypatch.setenv("XAI_API_KEY", "xai-test-key-1234567890")
        assert analyst_ai.providers() == ["xai", "groq"]
        assert "Grok" in analyst_ai.describe("paper")

    def test_off_without_a_key(self):
        assert not analyst_ai.enabled("paper")
        assert analyst.analyst_name("paper").startswith("heuristic")

    def test_never_in_live_or_unknown_mode(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        assert not analyst_ai.enabled("live")
        assert not analyst_ai.enabled(None)

    def test_config_and_env_switch_off(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        monkeypatch.setenv("BRAIN_ANALYST_AI", "0")
        assert not analyst_ai.enabled("paper")
        monkeypatch.delenv("BRAIN_ANALYST_AI")
        monkeypatch.setitem(config.ANALYST_AI, "enabled", False)
        assert not analyst_ai.enabled("paper")


class TestAnalyze:
    def test_paper_uses_the_model(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        calls = []
        _ai(monkeypatch, [GOOD], calls)
        th = analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert calls == ["groq"] and th.score == 8 and th.verdict == "valid_setup"

    def test_live_mode_stays_heuristic(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        calls = []
        _ai(monkeypatch, [GOOD], calls)
        th = analyst.analyze_token(TOKEN, {}, {}, mode="live")
        assert calls == []
        assert th == analyst.heuristic_thesis(TOKEN, {}, {})

    def test_code_downgrades_a_valid_setup_below_the_bar(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        _ai(monkeypatch, [GOOD.replace('"score":8', '"score":3')])
        th = analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert th.verdict == "watch"

    def test_explicit_desk_brain_still_wins(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        monkeypatch.setenv("DESK_BRAIN", "groq")
        calls = []
        _ai(monkeypatch, [GOOD], calls)
        with mock.patch.object(brain, "structured", return_value=None) as st:
            th = analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert st.called and calls == []
        assert th == analyst.heuristic_thesis(TOKEN, {}, {})


class TestFallback:
    @pytest.mark.parametrize("err", [RuntimeError("HTTP 429"), TimeoutError("t"), "not json"])
    def test_error_falls_back_to_heuristic_and_rests(self, monkeypatch, err):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        calls = []
        _ai(monkeypatch, [err], calls)
        th = analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert th == analyst.heuristic_thesis(TOKEN, {}, {})
        assert analyst_ai.providers() == []           # resting, not hammered
        analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert len(calls) == 1

    def test_xai_failure_falls_through_to_groq(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        monkeypatch.setenv("XAI_API_KEY", "xai-test-key-1234567890")
        calls = []
        _ai(monkeypatch, [RuntimeError("HTTP 400"), GOOD], calls)
        th = analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert calls == ["xai", "groq"] and th.score == 8


class TestBudget:
    def test_per_cycle_cap_then_new_cycle(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        monkeypatch.setitem(config.ANALYST_AI, "per_cycle", 2)
        calls = []
        _ai(monkeypatch, [], calls)
        for _ in range(5):
            analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert len(calls) == 2
        analyst_ai.new_cycle()
        analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert len(calls) == 3

    def test_daily_cap_persists_and_stops_calls(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        monkeypatch.setitem(config.ANALYST_AI, "daily_cap", 3)
        monkeypatch.setitem(config.ANALYST_AI, "per_cycle", 99)
        calls = []
        _ai(monkeypatch, [], calls)
        for _ in range(6):
            th = analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert len(calls) == 3 and analyst_ai.budget_left() == 0
        assert th == analyst.heuristic_thesis(TOKEN, {}, {})

    def test_status_has_no_key_material(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
        assert "gsk_test" not in str(analyst_ai.status())


def test_default_config_is_paper_cheap():
    c = config.ANALYST_AI
    assert c["prefer"][0] == "xai" and "groq" in c["prefer"]
    assert 0 < c["per_cycle"] <= 3 and 0 < c["daily_cap"] <= 200 and c["timeout_s"] <= 10
