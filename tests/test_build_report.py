"""The desk must be able to say which code it is actually serving.

The phone kept showing an older build and there was no way to tell from the
screen whether a pull had landed. `/api/build` answers that in one request:
the asset version plus a presence check for every feature added in this round.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_mod


@pytest.fixture()
def client():
    return TestClient(app_mod.app)


def test_build_endpoint_reports_the_live_version(client):
    r = client.get("/api/build")
    assert r.status_code == 200
    d = r.json()
    assert d["version"] == app_mod._asset_version()
    assert d["version"] and len(d["version"]) >= 8


def test_every_feature_is_present_in_this_tree(client):
    """A false here means a file the dashboard depends on never shipped."""
    d = client.get("/api/build").json()
    missing = [k for k, ok in d["features"].items() if not ok]
    assert not missing, f"features missing from the tree: {missing}"
    assert d["complete"] is True


def test_all_six_presets_are_registered(client):
    d = client.get("/api/build").json()
    for key in ("scalper", "swing", "balanced", "wr60", "wr65", "pf11"):
        assert key in d["presets"], f"preset {key} not registered"


def test_header_shows_the_build_chip(client):
    """The chip is how the operator sees the version without opening a menu."""
    html = client.get("/").text
    assert 'id="buildPill"' in html
    assert 'id="buildText"' in html
