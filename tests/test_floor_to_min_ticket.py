"""PAPER opt-in setting paper.floor_to_min_ticket (default OFF).

A cut ticket (RISK_OFF regime, breaker half size, size model...) that lands under the
paper min ticket is placed at the min ticket instead of vetoed, but only when the
operator turned it on, only in paper, never for a 0 size, and never when cash or the
open-risk budget cannot take the min ticket.
"""
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import config  # noqa: E402
from desk import guard, risk, settings  # noqa: E402
from test_guard_integration import Base, TOK  # noqa: E402
from legacy_book import legacy_book  # noqa: E402

KEY = "paper.floor_to_min_ticket"
RISK_OFF = {"tag": "RISK_OFF", "halve": True}


def _two_losses():
    now = time.time()
    return [{"mint": f"M{i}", "pnl_usd": -0.1, "closed": now - 60 * (2 - i)} for i in range(2)]


# ---------------------------------------------------------------- default + wiring

def test_defaults_to_off_in_config_and_settings():
    fresh = {}
    assert config.PAPER["floor_to_min_ticket"] is False
    assert settings.REGISTRY[KEY].get() is False
    fresh.update(settings.current_values())
    assert fresh[KEY] is False


def test_setting_is_a_confirmable_paper_bool():
    item = settings.REGISTRY[KEY]
    assert item.kind == "bool"
    assert not getattr(item, "owner_only", False)
    changes, dropped = settings.validate_changes([{"setting": KEY, "proposed": True}])
    assert not dropped and changes[0]["proposed"] is True and changes[0]["current"] is False
    assert ["paper.floor_to_min_ticket", False, "bool"] in settings.compact_for_ai()
    # validating must not apply anything
    assert config.PAPER["floor_to_min_ticket"] is False


@pytest.fixture
def isolated_settings(tmp_path):
    saved = settings.current_values()
    with mock.patch.dict("os.environ", {"DESK_DATA_DIR": str(tmp_path)}):
        settings.reset_runtime()
        yield
        for k, v in saved.items():
            settings.REGISTRY[k].set(v)
        settings.reset_runtime()


def test_proposal_does_not_apply_until_confirm_then_turns_on(isolated_settings):
    made = settings.create_values({KEY: True}, source="desk_ai")
    assert made["ok"], made
    assert config.PAPER["floor_to_min_ticket"] is False      # pending only
    pid = made["proposal"]["id"]
    assert settings.confirm(pid, "owner")["ok"]
    assert config.PAPER["floor_to_min_ticket"] is True
    pid = settings.create_values({KEY: False}, source="desk_ai")["proposal"]["id"]
    assert settings.confirm(pid, "owner")["ok"]
    assert config.PAPER["floor_to_min_ticket"] is False


# ---------------------------------------------------------------- helper rules

@pytest.fixture
def on():
    with mock.patch.dict(config.PAPER, {"floor_to_min_ticket": True, "min_ticket_usd": 2.0}):
        yield


def test_helper_off_never_floors():
    with mock.patch.dict(config.PAPER, {"floor_to_min_ticket": False}):
        assert risk.floor_cut_ticket(0.62, 2.5, 2.0, 40.0, "paper") == (0.62, False)


def test_helper_on_floors_062_to_2(on):
    assert risk.floor_cut_ticket(0.62, 2.5, 2.0, 40.0, "paper") == (2.0, True)


def test_helper_on_refuses_low_cash(on):
    assert risk.floor_cut_ticket(0.62, 2.5, 2.0, 1.5, "paper") == (0.62, False)


def test_helper_never_floors_zero_or_a_ticket_already_at_floor(on):
    assert risk.floor_cut_ticket(0.0, 2.5, 2.0, 40.0, "paper") == (0.0, False)
    assert risk.floor_cut_ticket(2.0, 2.5, 2.0, 40.0, "paper") == (2.0, False)
    assert risk.floor_cut_ticket(3.0, 5.0, 2.0, 40.0, "paper") == (3.0, False)


def test_helper_needs_a_precut_ticket_that_cleared_the_floor(on):
    # the sizing caps (risk, share, pool, cash) already said less than the min
    assert risk.floor_cut_ticket(0.4, 1.2, 2.0, 40.0, "paper") == (0.4, False)


def test_helper_live_is_never_affected(on):
    assert risk.floor_cut_ticket(0.62, 25.0, 10.0, 400.0, "live") == (0.62, False)
    assert risk.floor_cut_ticket(0.62, 2.5, 2.0, 40.0, "LIVE") == (0.62, False)


def test_zero_multiplier_is_a_zero_size_and_stays_blocked(on):
    mult, names = guard.size_multiplier([("breaker", 0.0)])
    assert mult == 0.0
    new = round(2.5 * mult, 2)
    assert risk.floor_cut_ticket(new, 2.5, 2.0, 40.0, "paper") == (0.0, False)


def test_budget_gate_refuses_a_floored_ticket_that_does_not_fit(on):
    """After flooring, the open-risk budget still decides (fit_risk_budget)."""
    acct = risk.Account(equity=49.0, day_pnl=0.0, open_positions=1, realized_equity=49.0,
                        day_start_equity=49.0, cash=40.0)
    with mock.patch.dict(config.RISK, {"risk_budget_usd": 1.0, "enforce_risk_budget": True}):
        ok, _, why = risk.fit_risk_budget(0.95, 2.0, 0.30, acct, 2.0)
        assert not ok and "below" in why
        ok, ticket, _ = risk.fit_risk_budget(0.0, 2.0, 0.30, acct, 2.0)
        assert ok and ticket == 2.0


# ---------------------------------------------------------------- through the agent loop

@legacy_book
class AgentLoop(Base):
    """$20 book, 5% risk: full ticket ~$3.3; RISK_OFF x breaker half = ~$0.83 < $2 min."""

    def _run(self, floor_on, **kw):
        kw.setdefault("regime_info", RISK_OFF)
        kw.setdefault("closed", _two_losses())
        with mock.patch.dict(config.PAPER, {"floor_to_min_ticket": floor_on, "min_ticket_usd": 2.0}):
            return self.run_agent(**kw)

    def test_off_keeps_the_old_veto(self):
        a = self._run(False)
        assert a.stats["entries"] == 0
        assert a.stats["floor_rejects"] == 1
        assert "below fee floor" in a.decisions[0]["reason"]
        assert not a.decisions[0]["approved"]

    def test_on_floors_the_cut_ticket_to_the_min(self):
        a = self._run(True)
        assert a.stats["entries"] == 1, [e["message"] for e in list(a.events)[:8]]
        assert self.entry_cost(a) == 2.0
        d = a.decisions[0]
        assert d["approved"] and d["ticket_usd"] == 2.0
        assert d["reason"].startswith("floored_to_min_ticket")
        g = d["sizes"]["guard"]
        assert g["floored_to_min_ticket"] is True
        assert g["ticket_after_cuts"] < 2.0
        assert "floored_to_min_ticket" in [c["t"] for c in d["flags"]]
        assert any("floored_to_min_ticket" in e["message"] for e in a.events)

    def test_on_but_a_full_size_ticket_is_untouched(self):
        a = self._run(True, regime_info=None, closed=None)
        assert a.stats["entries"] == 1
        assert self.entry_cost(a) > 3
        assert "floored_to_min_ticket" not in a.decisions[0]["reason"]

    def test_on_still_refuses_on_low_cash(self):
        with mock.patch.dict(config.PAPER, {"floor_to_min_ticket": True, "min_ticket_usd": 2.0}):
            a = self._run_low_cash()
        assert a.stats["entries"] == 0
        assert "below fee floor" in a.decisions[0]["reason"]

    def _run_low_cash(self):
        orig = risk.check_token_trade

        def lowcash(th, acct, *args, **kw):
            # sizing caps saw plenty; free cash is then just under the min ticket
            ok, ticket, why = orig(th, acct, *args, **kw)
            acct.cash = 1.5
            return ok, ticket, why

        with mock.patch("desk.solana_agent.check_token_trade", side_effect=lowcash):
            return self.run_agent(regime_info=RISK_OFF, closed=_two_losses())

    def test_on_still_refuses_when_the_breaker_pauses(self):
        now = time.time()
        closed = [{"mint": f"M{i}", "pnl_usd": -0.3, "closed": now - 60 * (3 - i)} for i in range(3)]
        a = self._run(True, closed=closed)
        assert a.stats["entries"] == 0
        assert a.breaker["state"] == "cooldown"

    def test_on_still_refuses_a_security_block(self):
        from test_guard_integration import sec
        a = self._run(True, security=sec(block=["closable"]))
        assert a.stats["entries"] == 0

    def test_on_still_refuses_when_the_open_risk_budget_cannot_fit_the_min_ticket(self):
        with mock.patch.dict(config.PAPER, {"floor_to_min_ticket": True, "min_ticket_usd": 2.0}), \
                mock.patch.dict(config.RISK, {"risk_budget_usd": 1.0, "enforce_risk_budget": True}), \
                mock.patch("desk.solana_agent.open_risk_usd", return_value=0.95):
            a = self.run_agent(regime_info=RISK_OFF, closed=_two_losses())
        assert a.stats["entries"] == 0
        assert "risk budget left" in a.decisions[0]["reason"], a.decisions[0]["reason"]

    def test_live_mode_is_never_floored(self):
        orig = self.run_agent
        with mock.patch.dict(config.PAPER, {"floor_to_min_ticket": True, "min_ticket_usd": 2.0}):
            from desk.solana_agent import SolanaAgent
            import tempfile
            d = Path(tempfile.mkdtemp())
            a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")
            a.mode = "live"
            ok, ticket, reason, info, _chips = a._caution(
                dict(TOK), 20.0, {"chips": [], "halve": ["x", "y"], "block": []},
                {"mult": 0.5, "state": "reduce"}, 0.30,
                risk.Account(equity=100.0, day_pnl=0.0, open_positions=0), max_ticket=25.0)
        assert "floored_to_min_ticket" not in info
        assert "floored_to_min_ticket" not in reason
        assert orig is not None
