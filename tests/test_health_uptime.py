"""Health must answer "did the box sleep?" with a number, not a guess.

Render's free plan stops the container after ~15 min without inbound traffic.
Before this, the only way to tell a spin-down from a slow request was to
reason about it. /api/health now reports uptime and the self-pinger's state.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_mod  # noqa: E402
from desk import keepalive  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _client():
    return TestClient(app_mod.app)


def test_health_reports_uptime():
    d = _client().get("/api/health").json()
    assert isinstance(d["uptime_s"], int)
    assert d["uptime_s"] >= 0
    assert d["booted_at"].endswith("Z")


def test_health_reports_keepalive_block():
    k = _client().get("/api/health").json()["keepalive"]
    for key in ("on", "off_reason", "interval_s", "url", "pings", "fails"):
        assert key in k


def test_keepalive_off_reason_names_the_cause(monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setenv("SELF_PING", "1")
    s = keepalive.status()
    assert s["on"] is False
    assert "RENDER_EXTERNAL_URL" in s["off_reason"]


def test_keepalive_reports_self_ping_disabled(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
    monkeypatch.setenv("SELF_PING", "0")
    s = keepalive.status()
    assert s["on"] is False
    assert s["off_reason"] == "SELF_PING=0"


def test_keepalive_on_when_render_url_present(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com/")
    monkeypatch.setenv("SELF_PING", "1")
    s = keepalive.status()
    assert s["on"] is True
    assert s["url"] == "https://x.onrender.com/api/health"
    assert s["off_reason"] is None


def test_health_exposes_the_ladder_without_a_token():
    """The token is exactly what you lack when checking whether a push landed."""
    d = _client().get("/api/health").json()
    x = d["exits"]
    assert x["name"] in ("fast", "patient", "slow", "single")
    assert x["source"] in ("env", "config", "default")
    assert set(x["ladder"]) == {"stop_pct", "tp1_at", "tp1_frac",
                                "tp2_at", "tp2_frac", "trail_pct"}


def test_health_exits_never_leaks_a_secret():
    """Unauthenticated endpoint: geometry only, no keys, no positions."""
    body = _client().get("/api/health").text.lower()
    for forbidden in ("desk_token", "api_key", "private", "secret", "wallet"):
        assert forbidden not in body
