"""The sizer's safety properties, pinned. If any of these break, the layer is unsafe."""
from __future__ import annotations

import json

import pytest

from desk import groq_sizer as g


class TestClamp:
    """Clamp is the entire safety story: nothing above rule size may survive."""

    @pytest.mark.parametrize("raw", [1.01, 1.5, 2.0, 10.0, 1e9])
    def test_never_grows(self, raw):
        assert g.clamp(raw) == 1.0

    @pytest.mark.parametrize("raw", [0.25, 0.5, 0.75, 1.0])
    def test_allowed_values_pass_through(self, raw):
        assert g.clamp(raw) == raw

    @pytest.mark.parametrize("raw", [0.0, 0.1, -1.0, -1e9])
    def test_never_blocks(self, raw):
        """A zero would be a veto. This layer may not veto; the floor is 0.25."""
        assert g.clamp(raw) == g.MIN_MULT

    @pytest.mark.parametrize("raw", [float("nan"), float("inf"), "x", None, [], {}])
    def test_garbage_fails_open(self, raw):
        assert g.clamp(raw) == 1.0


class TestParse:
    def test_plain_json(self):
        assert g._parse('{"mult":0.5,"confidence":70,"reasons":["thin liq"]}')["mult"] == 0.5

    def test_json_embedded_in_prose(self):
        out = g._parse('sure: {"mult": 0.25, "reasons": ["rug"]} done')
        assert out["mult"] == 0.25 and out["reasons"] == ["rug"]

    def test_string_number(self):
        assert g._parse('{"mult":"0.75"}')["mult"] == 0.75

    @pytest.mark.parametrize("txt", ["", "garbage", '{"confidence":50}', "{}"])
    def test_missing_mult_is_none(self, txt):
        assert g._parse(txt) is None

    def test_reasons_capped_at_two(self):
        out = g._parse('{"mult":0.5,"reasons":["a","b","c","d"]}')
        assert len(out["reasons"]) == 2


class TestDisabled:
    def test_no_key_means_one(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert g.enabled() is False
        mult, info = g.size_mult({"symbol": "X"}, mint="m")
        assert mult == 1.0 and info["source"] == "off"

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_123456")
        monkeypatch.setenv("BRAIN_SIZER_ON", "0")
        assert g.enabled() is False

    def test_short_key_rejected(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "short")
        assert g.enabled() is False


class TestBudget:
    def test_exhausted_budget_returns_one(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_123456")
        monkeypatch.setattr(g, "DAILY_BUDGET", 1)   # read at import, so patch the module
        monkeypatch.setattr(g, "_STATE_PATH", str(tmp_path / "state.json"))
        assert g.budget_left() == 1
        assert g._spend() is True
        assert g._spend() is False
        mult, info = g.size_mult({"symbol": "Y"}, mint="fresh_mint")
        assert mult == 1.0 and info["source"] == "budget"


class TestCompact:
    def test_drops_unknown_fields(self):
        out = g._compact({"symbol": "A", "liquidity_usd": 5, "secret": "nope"})
        assert "secret" not in out and out["symbol"] == "A"

    def test_keeps_dev_and_scam(self):
        out = g._compact({"symbol": "A", "dev": {"hold_pct": 3.0, "junk": 1},
                          "scam_checks": {"blocked": True}})
        assert out["dev"] == {"hold_pct": 3.0} and out["scam"] == {"blocked": True}


class TestBuckets:
    def _write(self, path, rows):
        path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    def test_joins_decisions_to_closes(self, tmp_path):
        p = tmp_path / "j.jsonl"
        self._write(p, [
            {"kind": "groq_sizer", "status": "ok", "mint": "a", "mult": 1.0},
            {"kind": "groq_sizer_pnl", "mint": "a", "mult": 1.0, "pnl_usd": 2.0},
            {"kind": "groq_sizer", "status": "ok", "mint": "b", "mult": 0.25},
            {"kind": "groq_sizer_pnl", "mint": "b", "mult": 0.25, "pnl_usd": -2.0},
        ])
        out = g.buckets(str(p))
        assert out["matched_closes"] == 2
        assert out["bands"]["1.0"]["avg_pnl_usd"] == 2.0
        assert out["bands"]["0.25"]["avg_pnl_usd"] == -2.0

    def test_monotonic_verdict(self, tmp_path):
        p = tmp_path / "j.jsonl"
        rows = []
        for mint, mult, pnl in [("a", 1.0, 3.0), ("b", 0.75, 1.0),
                                ("c", 0.5, -1.0), ("d", 0.25, -3.0)]:
            rows.append({"kind": "groq_sizer", "status": "ok", "mint": mint, "mult": mult})
            rows.append({"kind": "groq_sizer_pnl", "mint": mint, "mult": mult, "pnl_usd": pnl})
        self._write(p, rows)
        assert "monotonic" in g.buckets(str(p))["verdict"]

    def test_inverted_verdict(self, tmp_path):
        p = tmp_path / "j.jsonl"
        rows = []
        for mint, mult, pnl in [("a", 1.0, -3.0), ("b", 0.75, -1.0),
                                ("c", 0.5, 1.0), ("d", 0.25, 3.0)]:
            rows.append({"kind": "groq_sizer", "status": "ok", "mint": mint, "mult": mult})
            rows.append({"kind": "groq_sizer_pnl", "mint": mint, "mult": mult, "pnl_usd": pnl})
        self._write(p, rows)
        assert "NOT monotonic" in g.buckets(str(p))["verdict"]

    def test_missing_journal_is_not_a_crash(self, tmp_path):
        assert "error" in g.buckets(str(tmp_path / "nope.jsonl"))
