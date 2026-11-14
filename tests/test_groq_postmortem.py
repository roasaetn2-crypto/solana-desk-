"""Tests for the Groq post-mortem: the join, the honesty gate, and the parse.

The point of this module is that it refuses to run when the sample is too small
and never raises when the provider misbehaves. Those two properties are what the
tests are actually about -- the model's output is not testable, but the contract
around it is.
"""
from __future__ import annotations

import json

import pytest

from desk import groq_postmortem as pm


def _row(kind, mint, **kw):
    return {"time": "2026-11-15T00:00:00Z", "kind": kind, "mint": mint, **kw}


def _decision(mint, liq=40000.0, score=7.0, age=300.0):
    return _row("decision", mint, symbol=mint,
                token={"liquidity_usd": liq, "mcap_usd": liq * 8, "volume_h1": liq * 0.6,
                       "buys_h1": 120, "sells_h1": 90, "age_minutes": age},
                thesis={"score": score, "verdict": "valid_setup", "shape": "coil",
                        "red_flags": ["thin"]},
                signal={"direction": "long", "confidence": 62, "win_probability": 0.55},
                regime="neutral")


def _entry(mint, fill=1.0, ticket=3.0):
    return _row("entry", mint, symbol=mint, fill=fill, ticket_usd=ticket, horizon="swing")


def _exit(mint, pnl, fill=1.2, reason="tp1"):
    return _row("exit", mint, symbol=mint, fill=fill, pnl_usd=pnl, reason=reason)


def _write(tmp_path, rows):
    p = tmp_path / "journal.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


class TestBuildCases:
    def test_joins_exit_to_decision_and_entry(self, tmp_path):
        p = _write(tmp_path, [_decision("M1"), _entry("M1", fill=1.0), _exit("M1", 0.5, fill=1.2)])
        cases = pm.build_cases(p)
        assert len(cases) == 1
        c = cases[0]
        assert c["pnl_usd"] == 0.5
        assert c["win"] is True
        assert c["liquidity_usd"] == 40000.0
        assert c["score"] == 7.0
        assert c["ticket_usd"] == 3.0
        assert c["exit_reason"] == "tp1"

    def test_pnl_pct_is_computed_from_fills(self, tmp_path):
        p = _write(tmp_path, [_entry("M1", fill=1.0), _exit("M1", 0.5, fill=1.25)])
        assert pm.build_cases(p)[0]["pnl_pct"] == 25.0

    def test_loss_is_flagged_as_not_win(self, tmp_path):
        p = _write(tmp_path, [_entry("M1"), _exit("M1", -0.4, reason="stop")])
        c = pm.build_cases(p)[0]
        assert c["win"] is False
        assert c["exit_reason"] == "stop"

    def test_exit_without_decision_still_yields_a_case(self, tmp_path):
        """A missing decision row must degrade, not raise. The journal is written
        by several code paths and a partial row is normal."""
        p = _write(tmp_path, [_exit("M1", 0.3)])
        cases = pm.build_cases(p)
        assert len(cases) == 1
        assert cases[0]["pnl_usd"] == 0.3
        assert "liquidity_usd" not in cases[0]

    def test_exit_without_pnl_is_skipped(self, tmp_path):
        """A partial sell (half taken) has no realised P&L and is not a round trip."""
        p = _write(tmp_path, [_entry("M1"), _row("exit", "M1", fill=1.1, reason="tp1")])
        assert pm.build_cases(p) == []

    def test_missing_journal_is_empty_not_an_error(self, tmp_path):
        assert pm.build_cases(tmp_path / "nope.jsonl") == []

    def test_corrupt_line_is_skipped(self, tmp_path):
        p = tmp_path / "journal.jsonl"
        p.write_text('{"kind":"entry","mint":"M1","fill":1.0}\nNOT JSON\n'
                     '{"kind":"exit","mint":"M1","fill":1.2,"pnl_usd":0.4}\n',
                     encoding="utf-8")
        assert len(pm.build_cases(p)) == 1

    def test_null_fields_are_dropped_not_zeroed(self, tmp_path):
        """A null is missing data. Writing 0.0 would make the model think the pool
        had zero liquidity, which is a different and much worse claim."""
        d = _decision("M1")
        d["token"]["liquidity_usd"] = None
        p = _write(tmp_path, [d, _entry("M1"), _exit("M1", 0.2)])
        assert "liquidity_usd" not in pm.build_cases(p)[0]


class TestHonestyGate:
    def test_refuses_below_min_trades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 30)
        rows = []
        for i in range(9):
            rows += [_entry(f"M{i}"), _exit(f"M{i}", 0.1)]
        out = pm.analyse(pm.build_cases(_write(tmp_path, rows)))
        assert out["ok"] is False
        assert "need 30" in out["reason"]
        assert out["n"] == 9

    def test_does_not_call_the_model_below_min_trades(self, tmp_path, monkeypatch):
        """The gate must short-circuit before any network call, not after."""
        monkeypatch.setattr(pm, "MIN_TRADES", 30)
        called = []
        monkeypatch.setattr(pm, "_provider", lambda: called.append(1))
        rows = [_entry("M1"), _exit("M1", 0.1)]
        pm.analyse(pm.build_cases(_write(tmp_path, rows)))
        assert called == []

    def test_runs_at_exactly_min_trades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        monkeypatch.setenv("GROQ_API_KEY", "x" * 20)

        class P:
            model = "m"
            last_model = "m"
            def available(self): return True
            def call(self, s, u, t=700):
                return json.dumps({"verdict": "no separating feature", "loser_traits": [],
                                   "winner_traits": [], "next_test": "none"})
        monkeypatch.setattr(pm, "_provider", lambda: P())
        rows = [_entry("M1"), _exit("M1", 0.1), _entry("M2"), _exit("M2", -0.1)]
        out = pm.analyse(pm.build_cases(_write(tmp_path, rows)))
        assert out["ok"] is True
        assert out["n"] == 2


class TestFailureModes:
    def _enough(self, tmp_path):
        rows = []
        for i in range(32):
            rows += [_entry(f"M{i}"), _exit(f"M{i}", 0.1 if i % 2 else -0.1)]
        return pm.build_cases(_write(tmp_path, rows))

    def test_disabled_returns_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        monkeypatch.setenv("BRAIN_POSTMORTEM_ON", "0")
        out = pm.analyse(self._enough(tmp_path))
        assert out["ok"] is False
        assert "disabled" in out["reason"]

    def test_no_provider_returns_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        monkeypatch.setenv("GROQ_API_KEY", "x" * 20)
        monkeypatch.setattr(pm, "_provider", lambda: None)
        out = pm.analyse(self._enough(tmp_path))
        assert out["ok"] is False
        assert "unavailable" in out["reason"]

    def test_provider_exception_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        monkeypatch.setenv("GROQ_API_KEY", "x" * 20)

        class P:
            model = "m"
            def available(self): return True
            def call(self, s, u, t=700): raise RuntimeError("boom")
        monkeypatch.setattr(pm, "_provider", lambda: P())
        out = pm.analyse(self._enough(tmp_path))
        assert out["ok"] is False
        assert "RuntimeError" in out["reason"]

    def test_unparseable_output_returns_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        monkeypatch.setenv("GROQ_API_KEY", "x" * 20)

        class P:
            model = "m"
            def available(self): return True
            def call(self, s, u, t=700): return "I think the losers were bad, actually."
        monkeypatch.setattr(pm, "_provider", lambda: P())
        out = pm.analyse(self._enough(tmp_path))
        assert out["ok"] is False
        assert "unparseable" in out["reason"]


class TestParsing:
    def test_fenced_json_is_parsed(self):
        raw = '```json\n{"verdict":"x","loser_traits":[]}\n```'
        assert pm._parse(raw)["verdict"] == "x"

    def test_json_with_prose_around_it_is_parsed(self):
        raw = 'Here you go: {"verdict":"y"} hope that helps'
        assert pm._parse(raw)["verdict"] == "y"

    def test_garbage_is_none(self):
        assert pm._parse("no json here") is None
        assert pm._parse("") is None

    def test_traits_are_clamped_and_trimmed(self):
        out = pm._clean_list([{"trait": "thin liq", "evidence": "e", "confidence": "HIGH"},
                              {"no_trait": 1}, "bare string"])
        assert out[0]["trait"] == "thin liq"
        assert out[0]["confidence"] == "high"
        assert out[1]["trait"] == "bare string"
        assert len(out) == 2

    def test_non_list_becomes_empty(self):
        assert pm._clean_list("nope") == []
        assert pm._clean_list(None) == []


class TestPersistence:
    def test_save_then_load_roundtrips(self, tmp_path):
        p = tmp_path / "pm.json"
        pm.save({"ok": True, "n": 5, "verdict": "v"}, p)
        assert pm.load(p)["verdict"] == "v"

    def test_load_missing_is_empty_dict(self, tmp_path):
        assert pm.load(tmp_path / "nope.json") == {}

    def test_corrupt_state_is_empty_dict(self, tmp_path):
        p = tmp_path / "pm.json"
        p.write_text("{not json", encoding="utf-8")
        assert pm.load(p) == {}

    def test_run_reuses_cache_when_count_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        monkeypatch.setenv("GROQ_API_KEY", "x" * 20)
        state = tmp_path / "pm.json"
        monkeypatch.setattr(pm, "_STATE_PATH", str(state))
        calls = []

        class P:
            model = "m"
            last_model = "m"
            def available(self): return True
            def call(self, s, u, t=700):
                calls.append(1)
                return json.dumps({"verdict": "v", "loser_traits": [], "winner_traits": [],
                                   "next_test": "none"})
        monkeypatch.setattr(pm, "_provider", lambda: P())
        j = _write(tmp_path, [_entry("M1"), _exit("M1", 0.1), _entry("M2"), _exit("M2", -0.1)])
        first = pm.run(path=j)
        second = pm.run(path=j)
        assert first["ok"] and second.get("cached") is True
        assert len(calls) == 1

    def test_force_reruns(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        monkeypatch.setenv("GROQ_API_KEY", "x" * 20)
        monkeypatch.setattr(pm, "_STATE_PATH", str(tmp_path / "pm.json"))
        calls = []

        class P:
            model = "m"
            last_model = "m"
            def available(self): return True
            def call(self, s, u, t=700):
                calls.append(1)
                return json.dumps({"verdict": "v"})
        monkeypatch.setattr(pm, "_provider", lambda: P())
        j = _write(tmp_path, [_entry("M1"), _exit("M1", 0.1), _entry("M2"), _exit("M2", -0.1)])
        pm.run(path=j)
        pm.run(force=True, path=j)
        assert len(calls) == 2


class TestStatus:
    def test_status_never_calls_the_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "_STATE_PATH", str(tmp_path / "pm.json"))
        monkeypatch.setattr(pm, "_provider", lambda: pytest.fail("status must not build a provider"))
        s = pm.status(_write(tmp_path, [_entry("M1"), _exit("M1", 0.1)]))
        assert s["cases"] == 1
        assert s["ready"] is False

    def test_status_reports_ready_above_the_bar(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        monkeypatch.setattr(pm, "_STATE_PATH", str(tmp_path / "pm.json"))
        rows = [_entry("M1"), _exit("M1", 0.1), _entry("M2"), _exit("M2", -0.1)]
        assert pm.status(_write(tmp_path, rows))["ready"] is True

    def test_status_carries_the_last_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        state = tmp_path / "pm.json"
        monkeypatch.setattr(pm, "_STATE_PATH", str(state))
        pm.save({"ok": True, "n": 2, "wins": 1, "losses": 1, "verdict": "no signal",
                 "loser_traits": [{"trait": "thin liq", "evidence": "e", "confidence": "low"}]}, state)
        s = pm.status(_write(tmp_path, [_entry("M1"), _exit("M1", 0.1),
                                        _entry("M2"), _exit("M2", -0.1)]))
        assert s["ok"] is True
        assert s["win_rate"] == 0.5
        assert s["verdict"] == "no signal"
        assert s["loser_traits"][0]["trait"] == "thin liq"

    def test_status_survives_a_broken_journal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "_STATE_PATH", str(tmp_path / "pm.json"))
        bad = tmp_path / "j.jsonl"
        bad.mkdir()
        assert pm.status(bad)["cases"] == 0


class TestSampling:
    def test_sample_is_balanced_so_a_win_streak_cannot_dominate(self, tmp_path, monkeypatch):
        """60 winners and 5 losers must not be sent as 65 rows skewed 12:1."""
        monkeypatch.setattr(pm, "MIN_TRADES", 2)
        monkeypatch.setattr(pm, "BATCH", 10)
        monkeypatch.setenv("GROQ_API_KEY", "x" * 20)
        seen = {}

        class P:
            model = "m"
            last_model = "m"
            def available(self): return True
            def call(self, s, u, t=700):
                seen["payload"] = json.loads(u)
                return json.dumps({"verdict": "v"})
        monkeypatch.setattr(pm, "_provider", lambda: P())
        rows = []
        for i in range(60):
            rows += [_entry(f"W{i}"), _exit(f"W{i}", 0.2)]
        for i in range(5):
            rows += [_entry(f"L{i}"), _exit(f"L{i}", -0.2)]
        pm.analyse(pm.build_cases(_write(tmp_path, rows)))
        trades = seen["payload"]["trades"]
        assert sum(1 for t in trades if t["win"]) == 10
        assert sum(1 for t in trades if not t["win"]) == 5
