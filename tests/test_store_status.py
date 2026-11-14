"""store.status() / store.probe() and the /api/store endpoint.

The point of these two functions is a specific failure mode: you set
TURSO_DATABASE_URL in the Render Environment tab, the deploy goes green, and
the desk still persists nothing -- because the var was set on the wrong
service, or with a trailing newline, or the token was rotated. From outside,
"empty database" and "working database" look identical. status() reports what
the *process* saw; probe() does one real round trip and names the tables.
"""
from __future__ import annotations

import json

import pytest

from desk import store


def _fake_poster(tables=("journal", "kv"), journal_rows=0, fail=None):
    """A libSQL /v2/pipeline stand-in with the same envelope as the real thing."""
    calls = []

    def poster(url, token, payload):
        calls.append(payload)
        if fail:
            return 500, {"error": fail}
        stmts = [r["stmt"]["sql"] for r in payload["requests"] if r.get("type") == "execute"]
        results = []
        for sql in stmts:
            low = sql.lower()
            if "sqlite_master" in low:
                rows = [[{"type": "text", "value": t}] for t in tables]
                cols = [{"name": "name"}]
            elif "count(*)" in low:
                if "journal" not in tables:
                    # Real libSQL errors here; the desk must survive that.
                    results.append({"type": "error", "error": {
                        "message": "no such table: journal"}})
                    continue
                rows = [[{"type": "integer", "value": str(journal_rows)}]]
                cols = [{"name": "n"}]
            else:
                rows, cols = [], []
            results.append({"type": "ok", "response": {
                "type": "execute", "result": {"cols": cols, "rows": rows}}})
        results.append({"type": "ok", "response": {"type": "close"}})
        return 200, {"results": results}

    poster.calls = calls
    return poster


@pytest.fixture(autouse=True)
def _clean():
    store.reset_runtime()
    yield
    store.reset_runtime()


def test_status_is_cheap_and_reports_not_configured(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("DATABASE_AUTH_TOKEN", raising=False)
    s = store.status()
    assert s["configured"] is False
    assert s["host"] is None
    assert s["token_present"] is False
    assert s["tables_ready"] is False


def test_status_does_not_touch_the_network(monkeypatch):
    """status() goes on /api/health, which is polled. It must not do I/O."""
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    boom = _fake_poster(fail="should not be called")
    store._poster = boom
    s = store.status()
    assert s["configured"] is True
    assert boom.calls == []
    assert s["host"] == "db.turso.io"


def test_status_never_leaks_the_token_or_full_url(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://solana-desk-roms09.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "super-secret-token")
    blob = json.dumps(store.status())
    assert "super-secret-token" not in blob
    assert "libsql://" not in blob
    assert "solana-desk-roms09.turso.io" in blob  # host is fine, URL is not


def test_probe_before_schema_says_not_ready(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    store._poster = _fake_poster(tables=())
    p = store.probe()
    assert p["ok"] is True
    assert p["tables"] == []
    assert p["schema_ready"] is False
    assert p["journal_rows_remote"] is None


def test_probe_after_schema_reports_tables_and_rows(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    store._poster = _fake_poster(journal_rows=132)
    p = store.probe()
    assert p["schema_ready"] is True
    assert p["journal_rows_remote"] == 132


def test_probe_reports_a_rejected_token_as_an_error(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    store._poster = _fake_poster(fail="Unauthorized")
    p = store.probe()
    assert p["ok"] is False
    assert "Unauthorized" in p["error"]


def test_probe_is_cached_so_a_loop_cannot_burn_quota(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    poster = _fake_poster()
    store._poster = poster
    first = store.probe()
    n = len(poster.calls)
    second = store.probe()
    assert len(poster.calls) == n, "second probe must not hit the network"
    assert first["cached"] is False
    assert second["cached"] is True


def test_restore_records_what_it_did(monkeypatch, tmp_path):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    monkeypatch.setattr(store, "data_file", lambda name: tmp_path / name)
    store._poster = _fake_poster()
    store.restore()
    s = store.status()
    assert s["tables_ready"] is True
    assert s["last_restore_at"] is not None
    assert s["last_restore_error"] is None


def test_restore_failure_is_reported_not_raised(monkeypatch, tmp_path):
    """Fail-open: a dead store must never stop paper trading."""
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    monkeypatch.setattr(store, "data_file", lambda name: tmp_path / name)
    store._poster = _fake_poster(fail="boom")
    out = store.restore()
    assert out["error"] == "unavailable"
    assert store.status()["last_restore_error"] == "unavailable"


def test_health_exposes_store_and_api_store_returns_a_probe(monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    store._poster = _fake_poster(journal_rows=7)

    import app as appmod
    client = fastapi_testclient.TestClient(appmod.app)

    h = client.get("/api/health").json()
    assert "store" in h
    assert h["store"]["configured"] is True
    assert "super-secret" not in json.dumps(h)

    p = client.get("/api/store").json()
    assert p["schema_ready"] is True
    assert p["journal_rows_remote"] == 7
