"""The dashboard must never serve a stale shell.

The original bug: index.html carried hand-edited `?v=` strings, so a deploy
shipped new HTML pointing at an OLD app.js. The browser served the cached
script and new panels silently never appeared. These tests pin the fix.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import app as app_mod


@pytest.fixture()
def client():
    return TestClient(app_mod.app)


def test_index_is_not_cached(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")


def test_asset_version_is_derived_from_the_files(client):
    """The version in the HTML must equal the one the server computed."""
    r = client.get("/")
    v = r.headers.get("x-desk-assets")
    assert v and len(v) == 10
    assert f"app.js?v={v}" in r.text
    assert f"styles.css?v={v}" in r.text


def test_hand_edited_versions_are_overwritten(client):
    """A stale literal in index.html must not survive to the browser."""
    r = client.get("/")
    v = r.headers["x-desk-assets"]
    found = re.findall(r"/static/(?:app\.js|styles\.css)\?v=([^\"']*)", r.text)
    assert found, "no versioned asset tags in the shell"
    assert set(found) == {v}, f"stale version leaked: {found}"


def test_version_changes_when_an_asset_changes(tmp_path, monkeypatch):
    """The whole point: touch app.js and the URL must change."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "app.js").write_text("// v1", encoding="utf-8")
    (web / "styles.css").write_text("/* v1 */", encoding="utf-8")
    monkeypatch.setattr(app_mod, "WEB", web)

    v1 = app_mod._asset_version()
    (web / "app.js").write_text("// v2 changed", encoding="utf-8")
    v2 = app_mod._asset_version()
    assert v1 != v2


def test_version_survives_a_missing_asset(tmp_path, monkeypatch):
    """A half-deployed web/ dir must not 500 the dashboard."""
    web = tmp_path / "web"
    web.mkdir()
    monkeypatch.setattr(app_mod, "WEB", web)
    assert len(app_mod._asset_version()) == 10


def test_state_is_not_cached(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")
