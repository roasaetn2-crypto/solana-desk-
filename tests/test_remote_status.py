"""remote_status.py must classify a host correctly, because every failure mode
looks identical in a browser and they have completely different fixes.

No network in these tests: _get is monkeypatched.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    spec = importlib.util.spec_from_file_location(
        "remote_status", ROOT / "scripts" / "remote_status.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HEALTH = {
    "ok": True, "uptime_s": 4000, "booted_at": "2026-11-14T00:00:00Z",
    "cycle": 88, "running": True,
    "keepalive": {"on": True, "pings": 12, "fails": 0},
    "exits": {"name": "patient", "source": "config"},
}


def _probe(monkeypatch, routes):
    mod = _load()

    def fake_get(url, timeout=60.0):
        for suffix, (code, body) in routes.items():
            if url.endswith(suffix):
                return code, body
        return 404, "not found"

    monkeypatch.setattr(mod, "_get", fake_get)
    return mod.probe("https://desk.example")


def test_live(monkeypatch):
    r = _probe(monkeypatch, {"/api/health": (200, json.dumps(HEALTH))})
    assert r["state"] == "live"
    assert r["ladder"] == "patient"
    assert r["ladder_source"] == "config"


def test_old_build_has_no_uptime(monkeypatch):
    r = _probe(monkeypatch, {"/api/health": (200, json.dumps({"ok": True}))})
    assert r["state"] == "old-build"


def test_503_is_suspended(monkeypatch):
    r = _probe(monkeypatch, {"/api/health": (503, "unavailable")})
    assert r["state"] == "suspended"


def test_502_is_crashing(monkeypatch):
    r = _probe(monkeypatch, {"/api/health": (502, "bad gateway")})
    assert r["state"] == "crashing"


def test_404_with_a_desk_root_is_a_failed_build(monkeypatch):
    r = _probe(monkeypatch, {
        "/api/health": (404, "not found"),
        "/": (200, "<title>Unlock \u2014 Solana Desk</title>"),
    })
    assert r["state"] == "not-deployed"
    assert "failed build" in r["detail"]


def test_404_without_a_desk_root_is_the_wrong_url(monkeypatch):
    r = _probe(monkeypatch, {
        "/api/health": (404, "not found"),
        "/": (200, "<h1>Example Domain</h1>"),
    })
    assert r["state"] == "not-deployed"
    assert "wrong URL" in r["detail"]


def test_unreachable(monkeypatch):
    r = _probe(monkeypatch, {"/api/health": (0, "URLError: nodename nor servname")})
    assert r["state"] == "unreachable"


def test_non_json_200_is_not_the_desk(monkeypatch):
    r = _probe(monkeypatch, {"/api/health": (200, "<html>hello</html>")})
    assert r["state"] == "not-the-desk"


def test_fresh_boot_after_a_slow_answer_is_asleep(monkeypatch):
    mod = _load()
    fresh = {**HEALTH, "uptime_s": 30}
    monkeypatch.setattr(mod, "_get", lambda url, timeout=60.0: (200, json.dumps(fresh)))
    ticks = iter([0.0, 40.0])
    monkeypatch.setattr(mod.time, "time", lambda: next(ticks))
    r = mod.probe("https://desk.example")
    assert r["state"] == "asleep"
    assert "woke it" in r["detail"]


def test_every_state_has_advice():
    mod = _load()
    for state in ("live", "asleep", "just-restarted", "suspended", "unreachable",
                  "not-deployed", "crashing", "old-build", "not-the-desk"):
        assert mod.HINT.get(state), state
