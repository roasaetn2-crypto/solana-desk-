"""'cut at scanner (age)' must say whether the coin was too young, too old, or the swing lane is off."""
import pytest

import config
from desk import scanner


@pytest.fixture(autouse=True)
def _restore():
    snap = (dict(config.SWING), dict(config.SCALP), dict(config.ESTABLISHED))
    yield
    for d, s in zip((config.SWING, config.SCALP, config.ESTABLISHED), snap):
        d.clear(); d.update(s)


def _coin(age_minutes, **kw):
    t = dict(age_minutes=age_minutes, liquidity_usd=12_000, volume_h1=9_000, buys_h1=60, sells_h1=40)
    t.update(kw)
    return t


def test_only_age_gets_a_detail():
    assert scanner.kill_detail("liquidity", _coin(500)) == ""
    assert scanner.kill_detail(None, _coin(500)) == ""


def test_too_young_names_the_swing_minimum():
    config.SWING.update(enabled=True, min_age_minutes=120)
    d = scanner.kill_detail("age", _coin(30))
    assert "too young" in d and "30m" in d and "2.0h" in d


def test_too_old_names_the_cap_and_the_established_floors():
    config.SWING.update(enabled=True, max_age_hours=48)
    config.ESTABLISHED.update(enabled=True, max_age_hours=168)
    d = scanner.kill_detail("age", _coin(60 * 24 * 4))          # 4 days
    assert "established lane needs" in d and "12,000" in d       # shows what the coin actually has
    d2 = scanner.kill_detail("age", _coin(60 * 24 * 9))          # 9 days: past even the established cap
    assert d2.startswith("too old: 9.0d")


def test_swing_off_is_called_out():
    config.SWING.update(enabled=False)
    config.SCALP.update(enabled=False)
    assert "swing lane is OFF" in scanner.kill_detail("age", _coin(500))


def test_kill_reason_itself_is_unchanged():
    config.SWING.update(enabled=False)
    config.SCALP.update(enabled=False)
    assert scanner.free_kill(_coin(500)) == "age"                # stats and the bench still key on "age"
