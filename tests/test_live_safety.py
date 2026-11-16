"""Safety rails for live execution.

These tests exist to prove three things that matter more than any strategy:

  1. A deploy with no LIVE_TRADING_ENABLED is inert. Live can never arm.
  2. LIVE_DRY_RUN runs the guards and quotes but can never sign or send.
  3. The KILL file blocks a live buy even when everything else is armed.

No key, no network, no money.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _reload(monkeypatch, **env):
    """Reload desk.live with a controlled environment."""
    for k in ("LIVE_TRADING_ENABLED", "LIVE_DRY_RUN", "SOLANA_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import desk.live as live
    return importlib.reload(live)


def test_default_is_disabled(monkeypatch):
    live = _reload(monkeypatch)
    assert live.LIVE_DISABLED is True
    ok, why = live.ready()
    assert ok is False
    assert "DISABLED" in why


def test_opt_in_without_key_cannot_arm(monkeypatch):
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED="1")
    assert live.LIVE_DISABLED is False
    ok, why = live.ready()
    assert ok is False
    assert "SOLANA_PRIVATE_KEY" in why


@pytest.mark.parametrize("val", ["1", "true", "YES", "on"])
def test_opt_in_truthy(monkeypatch, val):
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED=val)
    assert live.LIVE_DISABLED is False


@pytest.mark.parametrize("val", ["0", "false", "no", "", "off"])
def test_opt_in_falsey(monkeypatch, val):
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED=val)
    assert live.LIVE_DISABLED is True


def test_dry_run_ready_without_key(monkeypatch):
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED="1", LIVE_DRY_RUN="1")
    assert live.dry_run() is True
    ok, why = live.ready()
    assert ok is True
    assert "DRY RUN" in why


def test_dry_run_can_never_sign(monkeypatch, tmp_path):
    """The last line of defence: _sign_and_send refuses under dry run."""
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED="1", LIVE_DRY_RUN="1")
    broker = live.LiveBroker(path=tmp_path / "live_ledger.json")
    with pytest.raises(live.LiveError) as e:
        broker._sign_and_send({"outAmount": "1"})
    assert "DRY RUN" in str(e.value)


def test_kill_switch_blocks_buy(monkeypatch, tmp_path):
    """KILL tripped -> a live buy is refused before any quote is built."""
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED="1", LIVE_DRY_RUN="1")
    monkeypatch.setattr(live, "killed", lambda: True)
    broker = live.LiveBroker(path=tmp_path / "live_ledger.json")
    with pytest.raises(live.LiveError) as e:
        broker.buy("So11111111111111111111111111111111111111112", "SOL", 5.0, 1.0)
    assert "kill switch" in str(e.value).lower()


def test_per_trade_cap_enforced(monkeypatch, tmp_path):
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED="1", LIVE_DRY_RUN="1")
    monkeypatch.setattr(live, "killed", lambda: False)
    broker = live.LiveBroker(path=tmp_path / "live_ledger.json")
    cap = live.EXECUTION["live_max_trade_usd"]
    with pytest.raises(live.LiveError) as e:
        broker.buy("So11111111111111111111111111111111111111112", "SOL", cap + 1, 1.0)
    assert "per-trade cap" in str(e.value)


def test_notional_cap_enforced(monkeypatch, tmp_path):
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED="1", LIVE_DRY_RUN="1")
    monkeypatch.setattr(live, "killed", lambda: False)
    broker = live.LiveBroker(path=tmp_path / "live_ledger.json")
    # Pretend the book is already at the notional ceiling.
    broker.s["positions"] = {"fake": {"cost": live.EXECUTION["live_max_notional_usd"]}}
    with pytest.raises(live.LiveError) as e:
        broker.buy("So11111111111111111111111111111111111111112", "SOL", 1.0, 1.0)
    assert "notional cap" in str(e.value)


def test_demo_token_refused_live(monkeypatch, tmp_path):
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED="1", LIVE_DRY_RUN="1")
    monkeypatch.setattr(live, "killed", lambda: False)
    broker = live.LiveBroker(path=tmp_path / "live_ledger.json")
    with pytest.raises(live.LiveError) as e:
        broker.buy("DEMO123", "DEMO", 1.0, 1.0)
    assert "demo" in str(e.value).lower()


# ---------------------------------------------------------------------------
# Switching seats. Paper <-> live is a real operator action, so it needs the
# same proof as arming: paper must always be reachable, live must never be
# reachable without the phrase, and leaving live must not orphan real money.
# ---------------------------------------------------------------------------

def _agent(monkeypatch, tmp_path):
    """A minimal SolanaAgent with both books pointed at tmp files."""
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED="1", LIVE_DRY_RUN="1")
    monkeypatch.setattr(live, "killed", lambda: False)
    from desk import solana_agent as sa
    importlib.reload(sa)
    ag = sa.SolanaAgent.__new__(sa.SolanaAgent)
    ag.mode = "paper"
    ag.paper = live.PaperBroker(path=tmp_path / "paper.json")
    ag.live = live.LiveBroker(path=tmp_path / "live_ledger.json")
    ag.events = __import__("collections").deque(maxlen=10)
    ag.lock = __import__("threading").RLock()
    ag.emit = lambda kind, msg, **kw: None
    return ag, live


def test_switch_to_paper_is_always_allowed(monkeypatch, tmp_path):
    ag, _ = _agent(monkeypatch, tmp_path)
    ag.mode = "live"
    out = ag.set_mode("paper")
    assert out["ok"] is True
    assert out["mode"] == "paper"
    assert ag.mode == "paper"


def test_switch_to_live_needs_the_phrase(monkeypatch, tmp_path):
    ag, _ = _agent(monkeypatch, tmp_path)
    out = ag.set_mode("live", confirm="yes")
    assert out["ok"] is False
    assert "phrase" in out["error"]
    assert ag.mode == "paper"


def test_switch_to_live_needs_a_key_when_not_dry_run(monkeypatch, tmp_path):
    """With dry run OFF, arming requires a real key. (In dry run, arming without
    a key is deliberate: the guards and quotes are what is being exercised.)"""
    ag, live = _agent(monkeypatch, tmp_path)
    live = _reload(monkeypatch, LIVE_TRADING_ENABLED="1")   # dry run off
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    ag.live = live.LiveBroker(path=tmp_path / "live2.json")
    out = ag.set_mode("live", confirm=live.EXECUTION["live_confirm_phrase"])
    assert out["ok"] is False
    assert "SOLANA_PRIVATE_KEY" in out["error"]
    assert ag.mode == "paper"


def test_dry_run_can_arm_without_a_key(monkeypatch, tmp_path):
    ag, live = _agent(monkeypatch, tmp_path)          # LIVE_DRY_RUN=1
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    out = ag.set_mode("live", confirm=live.EXECUTION["live_confirm_phrase"])
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert ag.mode == "live"


def test_leaving_live_with_open_position_is_refused(monkeypatch, tmp_path):
    """The orphan guard: paper and live keep separate books, so flipping to
    paper while a live position is open leaves it with no stop loss at all."""
    ag, _ = _agent(monkeypatch, tmp_path)
    ag.mode = "live"
    ag.live.s["positions"]["So11111111111111111111111111111111111111112"] = {
        "symbol": "SOL", "qty": 0.1, "entry": 100.0, "last": 100.0,
        "opened": 0.0, "cost": 10.0, "half_taken": False, "live": True,
    }
    out = ag.set_mode("paper")
    assert out["ok"] is False
    assert "orphan" in out["error"]
    assert ag.mode == "live"                      # still live, nothing abandoned
    assert len(out["open_live"]) == 1


def test_leaving_live_with_no_positions_is_allowed(monkeypatch, tmp_path):
    ag, _ = _agent(monkeypatch, tmp_path)
    ag.mode = "live"
    ag.live.s["positions"] = {}
    out = ag.set_mode("paper")
    assert out["ok"] is True
    assert ag.mode == "paper"


def test_force_abandons_open_positions_on_purpose(monkeypatch, tmp_path):
    ag, _ = _agent(monkeypatch, tmp_path)
    ag.mode = "live"
    ag.live.s["positions"]["So11111111111111111111111111111111111111112"] = {
        "symbol": "SOL", "qty": 0.1, "entry": 100.0, "last": 100.0,
        "opened": 0.0, "cost": 10.0, "half_taken": False, "live": True,
    }
    out = ag.set_mode("paper", force=True)
    assert out["ok"] is True
    assert ag.mode == "paper"


def test_demo_positions_do_not_block_the_switch(monkeypatch, tmp_path):
    """A DEMO token is not real money, so it must not trap the operator in live."""
    ag, _ = _agent(monkeypatch, tmp_path)
    ag.mode = "live"
    ag.live.s["positions"]["DEMOabc"] = {"symbol": "DEMO", "qty": 1.0,
                                         "entry": 1.0, "last": 1.0}
    out = ag.set_mode("paper")
    assert out["ok"] is True
