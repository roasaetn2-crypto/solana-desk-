"""xAI Grok: first for Desk AI chat when XAI_API_KEY is set, free providers as the fallback."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from desk import analyst_ai, brain_advisor as ba, desk_chat


class Resp:
    def __init__(self, code, body=None):
        self.status_code, self._b, self.text = code, body or {}, "x"
        self.headers = {}

    def json(self):
        return self._b


OK = Resp(200, {"choices": [{"message": {"content": '{"reply": "hi from grok", "agents": [], "proposals": []}'}}]})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in ("XAI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
              "CEREBRAS_API_KEY", "CLOUDFLARE_API_TOKEN", "OLLAMA_API_KEY", "BRAIN_XAI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    x = desk_chat.ADVISOR.xai
    x.used, x.cooldown_until, x._cooldowns, x._per_key = 0, 0.0, {}, {}
    x._hits, x._chat_hits, x._credit_at, x._credit, x.last_error = [], [], 0.0, 0.0, None
    x.day = ba._day()
    yield


def test_model_is_a_config_constant_and_cheap():
    assert config.XAI["base_url"] == "https://api.x.ai/v1"
    assert desk_chat.ADVISOR.xai.model == config.XAI["model"] == "grok-4.20-0309-non-reasoning"


def test_status_shows_provider_model_and_key_presence_never_the_key(monkeypatch):
    s = desk_chat.status()
    assert s["grok"]["key_present"] is False and s["grok"]["model"] == config.XAI["model"]
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-key-1234567890")
    s = desk_chat.status()
    assert s["grok"]["key_present"] is True and s["providers"][0] == "xai"
    assert "xai-secret" not in str(s) and "xai-secret" not in str(desk_chat.health())


def test_grok_not_in_advisory_panel(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-key-1234567890")
    assert desk_chat.ADVISOR.xai not in desk_chat.ADVISOR.providers()
    assert desk_chat.ADVISOR.xai not in desk_chat.ADVISOR.pair()
    assert desk_chat.ADVISOR.chat_providers()[0] is desk_chat.ADVISOR.xai


def test_chat_uses_grok_first(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-key-1234567890")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
    seen = []

    def post(url, **kw):
        seen.append((url, kw["json"]["model"], kw["headers"]["Authorization"]))
        return OK
    monkeypatch.setattr(ba.requests, "post", post)
    out = desk_chat.answer("how are we doing?", {}, None)
    assert out["provider"] == "xai" and out["source"] == "ai"
    assert seen[0][0] == "https://api.x.ai/v1/chat/completions"
    assert seen[0][1] == config.XAI["model"]


def test_chat_falls_back_on_429(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-key-1234567890")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
    calls = []

    def post(url, **kw):
        calls.append(url)
        if "x.ai" in url:
            return Resp(429)
        return Resp(200, {"choices": [{"message": {"content":
                          '{"reply": "from groq", "agents": [], "proposals": []}'}}]})
    monkeypatch.setattr(ba.requests, "post", post)
    out = desk_chat.answer("how are we doing?", {}, None)
    assert out["provider"] == "groq"
    desk_chat.answer("again", {}, None)
    assert sum("x.ai" in c for c in calls) == 1        # cooling after the 429, not hammered


def test_chat_daily_cap(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-key-1234567890")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
    desk_chat.ADVISOR.xai.budget = 2
    try:
        calls = []

        def post(url, **kw):
            calls.append(url)
            return OK if "x.ai" in url else Resp(200, {"choices": [{"message": {"content":
                   '{"reply": "from groq", "agents": [], "proposals": []}'}}]})
        monkeypatch.setattr(ba.requests, "post", post)
        for _ in range(4):
            desk_chat.answer("q", {}, None)
        assert sum("x.ai" in c for c in calls) == 2
    finally:
        desk_chat.ADVISOR.xai.budget = config.XAI["chat_daily_cap"]


def test_no_key_means_no_grok_call(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_1234567890")
    calls = []
    monkeypatch.setattr(ba.requests, "post", lambda url, **kw: (calls.append(url), OK)[1])
    desk_chat.answer("q", {}, None)
    assert not any("x.ai" in c for c in calls)


def test_redact_hides_the_xai_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-key-1234567890")
    assert "xai-secret" not in ba.redact("boom xai-secret-key-1234567890")


def test_analyst_uses_same_model_constant(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-key-1234567890")
    assert analyst_ai.model_for("xai") == config.XAI["model"]
    assert analyst_ai.providers()[0] == "xai"
