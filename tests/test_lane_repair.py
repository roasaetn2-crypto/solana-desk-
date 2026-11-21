"""Both lanes off means nothing can trade (every coin is cut as 'age'); boot repairs it."""
import pytest

import config
from desk import scanner, settings


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    snap = (dict(config.SWING), dict(config.SCALP))
    monkeypatch.delenv("DESK_NO_LANE_REPAIR", raising=False)
    yield
    for d, s in zip((config.SWING, config.SCALP), snap):
        d.clear(); d.update(s)


def _saved(monkeypatch, values):
    monkeypatch.setattr(settings, "_read", lambda path, default=None: {"values": values})


def test_both_lanes_off_after_boot_is_repaired(monkeypatch, capsys):
    _saved(monkeypatch, {"swing.enabled": False, "scalp.enabled": False})
    settings.apply_saved()
    assert config.SWING["enabled"] is True and config.SCALP["enabled"] is False
    assert "REPAIR" in capsys.readouterr().out


def test_a_desk_with_one_lane_on_is_left_alone(monkeypatch):
    _saved(monkeypatch, {"swing.enabled": False, "scalp.enabled": True})
    settings.apply_saved()
    assert config.SWING["enabled"] is False and config.SCALP["enabled"] is True
    _saved(monkeypatch, {"swing.enabled": True, "scalp.enabled": False})
    settings.apply_saved()
    assert config.SWING["enabled"] is True


def test_opt_out_keeps_both_lanes_off(monkeypatch):
    monkeypatch.setenv("DESK_NO_LANE_REPAIR", "1")
    _saved(monkeypatch, {"swing.enabled": False, "scalp.enabled": False})
    settings.apply_saved()
    assert config.SWING["enabled"] is False


def test_repaired_desk_no_longer_cuts_every_coin_as_swing_lane_off(monkeypatch):
    coin = dict(age_minutes=600, liquidity_usd=80_000, volume_h1=40_000, volume_m5=800, mcap_usd=300_000,
                buys_h1=90, sells_h1=60, change_h1=5.0, symbol="T")
    config.SWING.update(enabled=False); config.SCALP.update(enabled=False)
    assert scanner.free_kill(coin) == "age"
    _saved(monkeypatch, {"swing.enabled": False, "scalp.enabled": False})
    settings.apply_saved()
    assert scanner.free_kill(coin) != "age"
