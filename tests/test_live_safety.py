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


# ---------------------------------------------------------------------------
# Mid-cycle seat flips. `self.broker` is a property: it returns self.live when
# mode == "live" and self.paper otherwise. The operator can arm live from the
# dashboard at any moment, including while a cycle is mid-flight. The old
# _manage_positions snapshotted the paper book's mints and then re-read the
# property for every lookup, so the flip orphaned the iteration:
#   error: position management failed (KeyError: 'ANPik...pump')
# These tests pin the fix: bind the seat once, tolerate a vanished key.
# ---------------------------------------------------------------------------

def _manage_agent(monkeypatch, tmp_path):
    ag, live = _agent(monkeypatch, tmp_path)
    from desk import solana_agent as sa
    ag.session = None
    ag.tokens_seen = {}
    ag.stats = {"errors": 0, "exits": 0, "entries": 0, "cycles": 0}
    ag.cycle_no = 1
    ag._advise_exit = lambda *a, **k: None
    ag._mark_trade = lambda *a, **k: None
    ag._mcap_of = lambda *a, **k: None
    ag._review = lambda *a, **k: {"note": "", "exit_flags": []}
    ag.bench = type("B", (), {"sit": lambda *a, **k: None,
                              "benched": lambda *a, **k: False})()
    monkeypatch.setattr(sa.scanner, "quote_many", lambda mints, sess: {})
    monkeypatch.setattr(sa.candle_chart, "prefetch", lambda *a, **k: None)
    return ag, live, sa


def _seed_paper(ag, mint="PAPERmint111", symbol="PMT"):
    ag.paper.s["positions"][mint] = {
        "symbol": symbol, "entry": 1.0, "last": 1.0, "peak": 1.0,
        "qty": 10.0, "opened": 0.0, "horizon": "m5", "meta": {},
    }
    return mint


def test_manage_positions_survives_a_mid_cycle_flip_to_live(monkeypatch, tmp_path):
    """Arming live while the paper book is being managed must not raise."""
    ag, live, sa = _manage_agent(monkeypatch, tmp_path)
    mint = _seed_paper(ag)

    def flip_and_hold(m, q):
        ag.mode = "live"          # operator arms live mid-iteration
        return None               # no exit signal; loop keeps going

    monkeypatch.setattr(ag.paper, "exit_signal", flip_and_hold)
    monkeypatch.setattr(sa.signal_engine, "evaluate", lambda *a, **k: {"direction": "HOLD"})
    monkeypatch.setattr(sa.signal_engine, "should_sell", lambda *a, **k: (False, ""))

    ag._manage_positions()        # must not raise KeyError
    assert ag.mode == "live"
    assert mint in ag.paper.s["positions"]


def test_manage_positions_tolerates_a_vanished_position(monkeypatch, tmp_path):
    """A position deleted between the snapshot and the lookup is skipped, not fatal."""
    ag, live, sa = _manage_agent(monkeypatch, tmp_path)
    mint = _seed_paper(ag)

    def vanish(m, q):
        ag.paper.s["positions"].pop(mint, None)   # book changes under us
        return (1.0, "test vanish")

    monkeypatch.setattr(ag.paper, "exit_signal", vanish)
    monkeypatch.setattr(sa.signal_engine, "evaluate", lambda *a, **k: {"direction": "HOLD"})
    monkeypatch.setattr(sa.signal_engine, "should_sell", lambda *a, **k: (False, ""))

    ag._manage_positions()        # must not raise
    assert mint not in ag.paper.s["positions"]


def test_liquidate_survives_a_mid_cycle_flip(monkeypatch, tmp_path):
    ag, live, sa = _manage_agent(monkeypatch, tmp_path)
    mint = _seed_paper(ag)

    def flip_and_sell(m, fraction, price, reason):
        ag.mode = "live"
        ag.paper.s["positions"].pop(m, None)
        return {"symbol": "PMT", "mint": m, "fill": price, "pnl_usd": 0.0,
                "qty": 10.0 * fraction, "reason": reason}

    monkeypatch.setattr(ag.paper, "sell", flip_and_sell)
    ag._liquidate("test")
    assert ag.mode == "live"


# ---------------------------------------------------------------------------
# The $5-cap / $10-floor deadlock. The live lane reported, on every cycle:
#   Neil: risk engine said no (ticket $5.0 below fee floor $10)
# The per-trade cap was $5 and the floor was $10, so `min(max_trade, ...)`
# could never clear the floor. Fully funded, fully armed, zero fills forever.
# ---------------------------------------------------------------------------

def test_live_caps_are_coherent_by_default():
    from desk.risk import live_cap_problems
    assert live_cap_problems() == []


def test_live_cap_guard_catches_a_cap_under_the_floor(monkeypatch):
    from desk import risk
    monkeypatch.setitem(risk.EXECUTION, "live_max_trade_usd", 5.0)
    monkeypatch.setitem(risk.EXECUTION, "live_min_ticket_usd", 10.0)
    problems = risk.live_cap_problems()
    assert any("below" in p and "fee floor" in p for p in problems), problems


def test_live_cap_guard_catches_a_notional_under_the_floor(monkeypatch):
    from desk import risk
    monkeypatch.setitem(risk.EXECUTION, "live_max_trade_usd", 15.0)
    monkeypatch.setitem(risk.EXECUTION, "live_min_ticket_usd", 10.0)
    monkeypatch.setitem(risk.EXECUTION, "live_max_notional_usd", 5.0)
    problems = risk.live_cap_problems()
    assert any("notional" in p for p in problems), problems


def test_check_live_trade_uses_the_live_floor_not_the_paper_one(monkeypatch):
    """A $12 ticket must pass with a $10 live floor even though RISK says 10 and
    the old code compared against RISK while capping at 5."""
    from desk import risk, analyst
    monkeypatch.setitem(risk.EXECUTION, "live_min_ticket_usd", 10.0)
    caps = {"max_trade": 15.0, "min_ticket": 10.0, "max_notional": 20.0,
            "daily_loss_usd": 0.3, "deployed": 0.0}
    th = analyst.TokenThesis(verdict="valid_setup", score=99, red_flags=[],
                             green_flags=["crowd"], shape="crowd",
                             invalidation="buyers stall", sources=["buyers_h1"])
    ok, ticket, reason = risk.check_live_trade(th, 0.0, 0, 1_000_000.0, caps)
    assert ok is True, reason
    assert ticket >= 10.0


def test_check_live_trade_still_refuses_a_ticket_under_the_live_floor(monkeypatch):
    from desk import risk, analyst
    caps = {"max_trade": 15.0, "min_ticket": 10.0, "max_notional": 20.0,
            "daily_loss_usd": 0.3, "deployed": 0.0}
    th = analyst.TokenThesis(verdict="valid_setup", score=99, red_flags=[],
                             green_flags=["crowd"], shape="crowd",
                             invalidation="buyers stall", sources=["buyers_h1"])
    ok, ticket, reason = risk.check_live_trade(th, 0.0, 0, 100.0, caps)  # pool too thin
    assert ok is False
    assert "fee floor" in reason


def test_arming_is_refused_when_caps_deadlock(monkeypatch, tmp_path):
    ag, live = _agent(monkeypatch, tmp_path)
    monkeypatch.setitem(live.EXECUTION, "live_max_trade_usd", 5.0)
    monkeypatch.setitem(live.EXECUTION, "live_min_ticket_usd", 10.0)
    out = ag.set_mode("live", confirm=live.EXECUTION["live_confirm_phrase"])
    assert out["ok"] is False
    assert "contradictory" in out["error"]
    assert ag.mode == "paper"


# ---------- fee model (Jupiter charges 0%; Solana does not) ----------

def test_fee_model_charges_no_jupiter_platform_fee(monkeypatch):
    """Jupiter's protocol fee is 0 and we never send platformFeeBps. Everything
    in the model must be Solana cost: base + capped priority + rent + spread."""
    from desk import risk
    monkeypatch.setitem(risk.EXECUTION, "live_sol_price_usd", 100.0)
    m = risk.live_fee_model(10.0)
    # 2 x 5,000 base + 2 x 200,000 priority + 2,039,280 rent = 2,449,280 lamports
    assert m["fixed_usd"] == round(2_449_280 / 1e9 * 100.0, 4)
    assert m["spread_usd"] == round(10.0 * 0.015, 4)   # half the 300bps cap each way
    assert m["total_usd"] == round(m["fixed_usd"] + m["spread_usd"], 4)


def test_fee_model_pct_falls_as_the_ticket_grows(monkeypatch):
    """Rent is fixed, so it is 7% of a $2 ticket and 0.7% of a $20 one. This is
    the entire reason a live floor exists."""
    from desk import risk
    monkeypatch.setitem(risk.EXECUTION, "live_sol_price_usd", 100.0)
    small = risk.live_fee_model(2.0)["pct"]
    large = risk.live_fee_model(20.0)["pct"]
    assert small > large * 3, (small, large)


def test_fee_model_pins_the_sol_price_when_configured(monkeypatch):
    from desk import risk
    monkeypatch.setitem(risk.EXECUTION, "live_sol_price_usd", 71.23)
    assert risk.sol_price_usd() == 71.23
    monkeypatch.setitem(risk.EXECUTION, "live_sol_price_usd", 0.0)
    monkeypatch.setitem(risk.EXECUTION, "live_sol_price_fallback_usd", 150.0)
    assert risk.sol_price_usd() == 150.0


def test_fee_share_guard_catches_a_floor_under_the_fee(monkeypatch):
    """A $2 live floor pays ~20% round trip. The guard must refuse to arm."""
    from desk import risk
    monkeypatch.setitem(risk.EXECUTION, "live_min_ticket_usd", 2.0)
    monkeypatch.setitem(risk.EXECUTION, "live_max_trade_usd", 15.0)
    monkeypatch.setitem(risk.EXECUTION, "live_max_fee_share_pct", 0.08)
    problems = risk.live_cap_problems()
    assert any("fee-share" in p for p in problems), problems


def test_fee_share_guard_passes_the_shipped_floor(monkeypatch):
    from desk import risk
    monkeypatch.setitem(risk.EXECUTION, "live_min_ticket_usd", 10.0)
    monkeypatch.setitem(risk.EXECUTION, "live_max_trade_usd", 15.0)
    monkeypatch.setitem(risk.EXECUTION, "live_max_fee_share_pct", 0.08)
    assert risk.live_cap_problems() == []


def test_priority_fee_is_capped_not_auto(monkeypatch, tmp_path):
    """'auto' let Jupiter bid unbounded. The swap build must send a capped
    object so a hot launch cannot charge more in priority than the edge."""
    from desk import live as livemod
    monkeypatch.setitem(livemod.EXECUTION, "live_priority_fee_max_lamports", 200000)
    monkeypatch.setitem(livemod.EXECUTION, "live_priority_level", "high")
    sent = {}

    class Resp:
        status_code = 200
        text = ""
        def json(self): return {"swapTransaction": "AAAA"}

    class Http:
        def post(self, url, json=None, timeout=None):
            sent.update(json or {})
            return Resp()

    class KP:
        def pubkey(self): return "PUBKEY"
        def sign_message(self, m): raise RuntimeError("stop here")

    b = livemod.LiveBroker(tmp_path / "l.json")
    b.http = Http()
    monkeypatch.setattr(livemod, "_keypair", lambda: KP())
    monkeypatch.setattr(livemod, "dry_run", lambda: False)
    monkeypatch.setattr(livemod, "LIVE_DISABLED", "")
    try:
        b._sign_and_send({"outAmount": "1"})
    except Exception:
        pass
    # Jupiter's schema is NESTED. A flat {maxLamports, priorityLevel} returns
    # 422 and no transaction, so this shape is load-bearing, not cosmetic.
    assert sent["prioritizationFeeLamports"] == {
        "priorityLevelWithMaxLamports": {"maxLamports": 200000,
                                         "priorityLevel": "high"}}


def test_loss_limit_covers_one_full_stop_out(monkeypatch, tmp_path):
    """$0.30 was below a single $15 stop-out, so the first loss ended live for
    the day. The stop must always cover at least one ordinary stop-out."""
    ag, live = _agent(monkeypatch, tmp_path)
    monkeypatch.setitem(live.EXECUTION, "live_max_trade_usd", 15.0)
    monkeypatch.setitem(live.EXECUTION, "live_max_notional_usd", 20.0)
    monkeypatch.setitem(live.EXECUTION, "live_daily_loss_pct", 0.015)
    limit = ag.live.loss_limit_usd()
    one_stop = 15.0 * 0.20          # scalp profile stop
    assert limit >= one_stop, (limit, one_stop)
    assert limit > 0.30


def test_loss_limit_scales_with_the_book(monkeypatch, tmp_path):
    ag, live = _agent(monkeypatch, tmp_path)
    ag.live.s["cash"] = 500.0
    monkeypatch.setitem(live.EXECUTION, "live_daily_loss_pct", 0.02)
    monkeypatch.setitem(live.EXECUTION, "live_max_trade_usd", 15.0)
    assert ag.live.loss_limit_usd() >= 500.0 * 0.02


def test_fills_record_the_measured_fee_not_zero(monkeypatch, tmp_path):
    """The fee column was hardcoded 0.0, so realised P&L absorbed every fee."""
    from desk import live as livemod
    b = livemod.LiveBroker(tmp_path / "l.json")
    monkeypatch.setattr(b, "_rpc", lambda m, p: {"meta": {"fee": 1_000_000}})
    monkeypatch.setitem(livemod.EXECUTION, "live_sol_price_usd", 100.0)
    assert b._fee_usd("sig") == 0.1


def test_fee_lookup_failure_never_fails_a_fill(monkeypatch, tmp_path):
    from desk import live as livemod
    b = livemod.LiveBroker(tmp_path / "l.json")
    def boom(m, p): raise RuntimeError("rpc down")
    monkeypatch.setattr(b, "_rpc", boom)
    assert b._fee_usd("sig") == 0.0
