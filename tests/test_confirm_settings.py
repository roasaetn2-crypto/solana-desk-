"""The entry confirmation checks are switchable settings (the trend check vetoes dip entries)."""
import pytest

import config
from desk import confirm, settings

KEYS = ("confirm.enabled", "confirm.check_trend", "confirm.check_buy", "confirm.check_turnover",
        "confirm.check_knife", "confirm.check_extended")


@pytest.fixture(autouse=True)
def _restore():
    snap = (dict(config.CONFIRM), dict(config.CONFIRM["checks"]))
    yield
    config.CONFIRM.clear(); config.CONFIRM.update(snap[0])
    config.CONFIRM["checks"] = dict(snap[1])


def test_all_checks_exist_default_on():
    for k in KEYS:
        assert settings.resolve(k).get() is True, k


def test_toggling_the_setting_changes_the_confirm_config():
    settings.resolve("confirm.check_trend").set(False)
    assert config.CONFIRM["checks"]["trend"] is False
    settings.resolve("confirm.check_trend").set(True)
    assert config.CONFIRM["checks"]["trend"] is True


def test_a_failing_trend_vetoes_only_while_the_check_is_on(monkeypatch):
    monkeypatch.setattr(confirm, "trend_state", lambda *a, **k: {"ok": False, "why": "EMA9 < EMA21"})
    t = {"symbol": "T"}
    assert confirm.checklist(t, None, [], [])["ok"] is False
    settings.resolve("confirm.check_trend").set(False)
    assert confirm.checklist(t, None, [], [])["ok"] is True


def test_settings_can_be_proposed_and_validated():
    out = settings.create_values({"confirm.check_trend": False}, "t", owner=True)
    assert out["ok"] is True
    ch = out["proposal"]["changes"]
    assert [c["setting"] for c in ch] == ["confirm.check_trend"] and ch[0]["proposed"] is False
