"""Analyst AI: the prompt names the JSON shape (verdict included), the parser tolerates a missing
or odd verdict by choosing "skip", and the Groq request only uses params Groq accepts.
All HTTP is mocked; no key is read."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from desk import analyst, analyst_ai, brain
from desk.analyst import TOKEN_PROMPT, TokenThesis

TOKEN = {"symbol": "T", "liquidity_usd": 30_000, "mcap_usd": 100_000, "volume_h1": 60_000,
         "buys_h1": 200, "sells_h1": 100, "buyers_h1": 120, "change_h1": 20, "change_m5": 3,
         "age_minutes": 90}
FAKE_GROQ = "gsk_fake_key_for_tests_123456"
FAKE_XAI = "xai-fake-key-for-tests-123456"


class Resp:
    def __init__(self, status, content=None, text=""):
        self.status_code = status
        self._content = content
        self.text = text

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class FakeSession:
    """Behaves like the two providers as seen in production."""

    def __init__(self, xai_reply, groq_reply=None):
        self.posts = []
        self.xai_reply = xai_reply
        self.groq_reply = groq_reply

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        text = " ".join(m["content"] for m in json["messages"]).lower()
        if "groq" in url:
            # Groq rejects JSON mode when no message mentions the word "json"
            if json["response_format"]["type"] == "json_object" and "json" not in text:
                return Resp(400, text='{"error":{"message":"\'messages\' must contain the word '
                                 f'\'json\' in some form (key {FAKE_GROQ})"}}}}')
            for bad in ("max_tokens",):
                if bad in json:
                    return Resp(400, text=f"unsupported param {bad}")
            return Resp(200, self.groq_reply)
        return Resp(200, self.xai_reply)


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    for k in ("DESK_BRAIN", "DESK_BRAIN_AUTODETECT", "BRAIN_ANALYST_AI", "BRAIN_XAI_MODEL",
              "BRAIN_ANALYST_GROQ_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XAI_API_KEY", FAKE_XAI)
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ)
    monkeypatch.setenv("BRAIN_ANALYST_AI_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(brain, "_ollama_up", lambda: False)
    analyst_ai._cooldown_until.clear()
    analyst_ai._last_error.clear()
    analyst_ai.new_cycle()
    monkeypatch.setattr(analyst_ai, "_session", None)
    yield
    analyst_ai._cooldown_until.clear()      # module state must not leak into other tests
    analyst_ai._last_error.clear()


def _session(monkeypatch, xai, groq=None):
    s = FakeSession(xai, groq)
    monkeypatch.setattr(analyst_ai, "_session", s)
    return s


class TestPrompt:
    def test_prompt_names_json_and_every_key_and_the_verdict_values(self):
        low = TOKEN_PROMPT.lower()
        assert "json" in low
        for k in ("verdict", "score", "red_flags", "green_flags", "shape", "invalidation", "sources"):
            assert f'"{k}"' in TOKEN_PROMPT
        for v in ("skip", "watch", "valid_setup"):
            assert v in TOKEN_PROMPT
        assert "required" in low


class TestParserTolerance:
    FULL = {"verdict": "watch", "score": 5, "red_flags": ["a"], "green_flags": [],
            "shape": "crowd", "invalidation": "x", "sources": ["liquidity_usd"]}

    def test_valid_reply_unchanged(self):
        th = TokenThesis.model_validate(self.FULL)
        assert (th.verdict, th.score, th.shape) == ("watch", 5, "crowd")

    def test_missing_verdict_defaults_to_skip_with_a_flag(self):
        d = {k: v for k, v in self.FULL.items() if k != "verdict"}
        th = TokenThesis.model_validate(d)
        assert th.verdict == "skip"
        assert any("no usable verdict" in r for r in th.red_flags)

    @pytest.mark.parametrize("raw", ["buy", "BUY NOW", "", 7, "moon"])
    def test_invalid_verdict_defaults_to_skip(self, raw):
        th = TokenThesis.model_validate({**self.FULL, "verdict": raw})
        assert th.verdict == "skip"

    def test_case_and_alias_are_accepted(self):
        assert TokenThesis.model_validate({**self.FULL, "verdict": " VALID_SETUP "}).verdict == "valid_setup"
        assert TokenThesis.model_validate({**self.FULL, "verdict": "valid setup"}).verdict == "valid_setup"

    def test_missing_optional_fields_get_safe_defaults(self):
        th = TokenThesis.model_validate({"verdict": "watch", "score": "6.4"})
        assert th.score == 6 and th.red_flags == [] and th.sources == []
        assert th.shape == "too_early" and th.invalidation

    def test_score_clamped_and_junk_is_zero(self):
        assert TokenThesis.model_validate({"verdict": "skip", "score": 99}).score == 10
        assert TokenThesis.model_validate({"verdict": "skip", "score": "n/a"}).score == 0

    def test_wrapped_reply_is_unwrapped(self):
        th = TokenThesis.model_validate({"thesis": self.FULL})
        assert th.verdict == "watch"

    def test_reply_with_no_verdict_and_no_score_is_rejected(self):
        with pytest.raises(Exception):
            TokenThesis.model_validate({"summary": "looks fine"})

    def test_ai_can_never_upgrade_from_a_missing_verdict(self):
        d = {k: v for k, v in self.FULL.items() if k != "verdict"} | {"score": 10}
        th = analyst._guard_ai_thesis(TokenThesis.model_validate(d))
        assert th.verdict == "skip"


class TestProviders:
    def test_xai_reply_missing_verdict_still_gives_a_thesis(self, monkeypatch):
        xai = json.dumps({"score": 6, "red_flags": ["thin"], "green_flags": [], "shape": "crowd",
                          "invalidation": "x", "sources": ["liquidity_usd"]})
        s = _session(monkeypatch, xai)
        th = analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert th.verdict == "skip" and th.score == 6
        assert len(s.posts) == 1 and not analyst_ai._last_error     # no failure, no fallback

    def test_groq_json_mode_gets_the_word_json_so_no_400(self, monkeypatch):
        good = json.dumps({"verdict": "watch", "score": 5, "red_flags": [], "green_flags": [],
                           "shape": "crowd", "invalidation": "x", "sources": ["a"]})
        monkeypatch.delenv("XAI_API_KEY")
        s = _session(monkeypatch, None, good)
        th = analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert th.verdict == "watch"
        assert not analyst_ai._last_error

    def test_groq_request_params(self):
        body = analyst_ai._body("groq", "sys json", "usr")
        assert "max_tokens" not in body and body["max_completion_tokens"] >= 800
        assert body["reasoning_effort"] == "low"
        assert body["response_format"] == {"type": "json_object"}
        assert body["model"] == "openai/gpt-oss-20b"

    def test_xai_request_params_unchanged(self):
        body = analyst_ai._body("xai", "sys json", "usr")
        assert body["max_tokens"] == config.ANALYST_AI["max_tokens"]
        assert "reasoning_effort" not in body and "max_completion_tokens" not in body

    def test_old_prompt_without_json_would_400_on_groq(self, monkeypatch):
        """Regression: what production did before (no 'json' anywhere in the messages)."""
        monkeypatch.delenv("XAI_API_KEY")
        _session(monkeypatch, None, "{}")
        with pytest.raises(RuntimeError, match="HTTP 400"):
            analyst_ai._call("groq", "You are the analyst.", "Produce the thesis.")

    def test_error_body_is_logged_without_the_key(self, monkeypatch, capsys):
        monkeypatch.delenv("XAI_API_KEY")
        _session(monkeypatch, None, "{}")
        out = analyst_ai.thesis("You are the analyst.", "Produce the thesis.", TokenThesis)
        assert out is None
        err = analyst_ai._last_error["groq"]
        assert err.startswith("RuntimeError: HTTP 400") and "json" in err
        shown = err + capsys.readouterr().out
        assert FAKE_GROQ not in shown and "gsk_fake" not in shown

    def test_both_fail_falls_back_to_heuristic(self, monkeypatch):
        _session(monkeypatch, "not json at all", "{}")
        th = analyst.analyze_token(TOKEN, {}, {}, mode="paper")
        assert th.verdict in ("skip", "watch", "valid_setup")
        assert set(analyst_ai._last_error) == {"xai", "groq"}
