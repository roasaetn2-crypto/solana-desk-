"""/api/health 'ai' block and Desk AI status: presence flags only, accurate busy vs no-keys."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import desk_chat as dc  # noqa: E402
from desk import brain_advisor as ba  # noqa: E402
from desk.brain_advisor import ADVISOR  # noqa: E402

GKEY = "AIzaHEALTHkeyABCDEFGHIJ1234567"
OKEY = "sk-or-v1-HEALTHkeyABCDEFGHIJ1234567"
NO_ENV = {"GEMINI_API_KEY": "", "GROQ_API_KEY": "", "OPENROUTER_API_KEY": ""}


class _Resp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._d, self.text, self.headers = code, data or {}, text, {}

    def json(self):
        return self._d


def _reset():
    for p in ADVISOR.providers():
        p.cooldown_until, p.used, p.errors, p.last_error, p.last_error_at = 0.0, 0, 0, None, 0.0


class Health(unittest.TestCase):
    def setUp(self):
        _reset()

    tearDown = setUp

    def test_no_secret_material(self):
        with mock.patch.dict(os.environ, {**NO_ENV, "GEMINI_API_KEY": GKEY, "OPENROUTER_API_KEY": OKEY}):
            h = dc.health()
        blob = repr(h)
        for bad in (GKEY, OKEY, GKEY[:6], OKEY[:8], str(len(GKEY)), str(len(OKEY))):
            self.assertNotIn(bad, blob)
        self.assertTrue(h["gemini"]["key_present"] and h["openrouter"]["key_present"])
        self.assertFalse(h["groq"]["key_present"])
        self.assertTrue(h["chat_enabled"])
        for k in ("key_present", "available", "cooling_s", "last_error"):
            self.assertIn(k, h["gemini"])

    def test_no_keys(self):
        with mock.patch.dict(os.environ, NO_ENV):
            h, s = dc.health(), dc.status()
        self.assertFalse(h["chat_enabled"])
        self.assertEqual(s["state"], "no_keys")

    def test_keys_but_limited_says_busy_not_unset(self):
        with mock.patch.dict(os.environ, {**NO_ENV, "GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ba.requests, "post", return_value=_Resp(429)):
            out = dc.answer("how am I doing?", {})
            s = dc.status()
        self.assertEqual(out["source"], "unavailable")
        self.assertIn("AI busy / limited", out["reply"])
        self.assertIn("rate_limited", out["reply"])
        self.assertNotIn("not set", out["reply"])
        self.assertEqual(s["state"], "busy")
        self.assertEqual(s["errors"]["gemini"], "rate_limited")

    def test_bad_key_is_auth_class(self):
        with mock.patch.dict(os.environ, {**NO_ENV, "GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ba.requests, "post", return_value=_Resp(403, text="API key not valid")):
            dc.answer("hi", {})
            self.assertEqual(dc.health()["gemini"]["last_error"], "auth")

    def test_gemini_falls_back_on_gone_model(self):
        seen = []
        ok = _Resp(200, {"candidates": [{"content": {"parts": [{"text": '{"reply":"fine"}'}]}}]})

        def post(url, **kw):
            seen.append(url)
            return _Resp(404, text="no longer available") if len(seen) == 1 else ok

        with mock.patch.dict(os.environ, {**NO_ENV, "GEMINI_API_KEY": GKEY}), \
                mock.patch.object(ba.requests, "post", side_effect=post):
            out = dc.answer("hi", {})
        self.assertEqual(out["source"], "ai")
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(out["model"], ADVISOR.gemini.models[1])

    def test_health_endpoint_has_ai_block(self):
        os.environ.pop("DESK_TOKEN", None)
        os.environ["DESK_TOKEN"] = "s3cret"
        sys.modules.pop("app", None)
        try:
            from fastapi.testclient import TestClient
            import app as appmod
            with mock.patch.dict(os.environ, {**NO_ENV, "OPENROUTER_API_KEY": OKEY}):
                r = TestClient(appmod.app).get("/api/health")
            self.assertEqual(r.status_code, 200)
            ai = r.json()["ai"]
            self.assertTrue(ai["openrouter"]["key_present"])
            self.assertNotIn(OKEY[:10], r.text)
        finally:
            os.environ.pop("DESK_TOKEN", None)
            sys.modules.pop("app", None)


if __name__ == "__main__":
    unittest.main()
