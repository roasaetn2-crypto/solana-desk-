"""Artifacts go to research/, and the repo root stays a repo root.

The root had accumulated 30 generated files -- backtest JSON, PNG charts, pool
caches -- because every script wrote to a bare filename in whatever directory
you happened to be standing in. `python scripts/backtest_100.py` from the repo
root and from `scripts/` put the output in two different places, and the docs
linked to whichever one the author had used.

These tests pin the two halves of the fix: `_paths` resolves from the repo
root, and no script has quietly reintroduced a bare artifact filename.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ARTIFACT_RE = re.compile(r"^[a-z0-9_]+\.(json|png|svg|csv|jsonl)$")

# Files that are legitimately read/written by name at runtime and are not
# research artifacts: the desk's own state lives in data/ via desk.ops.
ALLOWED = {
    "journal.jsonl", "paper_ledger.json", "desk.db", "desk.log",
    "live_ledger.json", "ledger.json",
    "operator_intent.json", "operator_settings.json", "proposals.json",
    "desk_chat.json", "desk_devices.json", "autotune.json",
    "groq_postmortem.json", "groq_sizer_state.json", "fomo_usage.json",
}


def _paths():
    import sys
    sys.path.insert(0, str(SCRIPTS))
    import _paths as p
    return p


def test_out_resolves_under_research_regardless_of_cwd(tmp_path, monkeypatch):
    p = _paths()
    monkeypatch.chdir(tmp_path)
    got = Path(p.out("backtest_100.json"))
    assert got == ROOT / "research" / "backtest_100.json"
    assert (ROOT / "research").is_dir()


def test_out_preserves_an_absolute_path(tmp_path):
    p = _paths()
    target = tmp_path / "elsewhere.json"
    assert p.out(str(target)) == str(target)


def test_read_finds_research_then_falls_back_to_root(tmp_path, monkeypatch):
    p = _paths()
    assert p.read("backtest_100.json").parent == ROOT / "research"
    # a name that only exists at the root still resolves (legacy artifacts)
    legacy = ROOT / "legacy_probe.json"
    legacy.write_text("{}", encoding="utf-8")
    try:
        assert p.read("legacy_probe.json") == legacy
    finally:
        legacy.unlink()


def test_no_script_writes_a_bare_artifact_filename():
    """Regression guard: this is the bug that cluttered the root."""
    offenders = []
    for f in sorted(SCRIPTS.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # open("x.json", "w") / open("x.json")
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
                if node.args and isinstance(node.args[0], ast.Constant):
                    v = node.args[0].value
                    if isinstance(v, str) and ARTIFACT_RE.match(v) and v not in ALLOWED:
                        offenders.append(f"{f.name}: open({v!r})")
            # argparse default="x.json"
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
                for kw in node.keywords:
                    if kw.arg in ("default", "out", "cache", "chart") and \
                            isinstance(kw.value, ast.Constant) and \
                            isinstance(kw.value.value, str) and \
                            ARTIFACT_RE.match(kw.value.value):
                        offenders.append(f"{f.name}: --{kw.arg} default={kw.value.value!r}")
    assert not offenders, "bare artifact paths reintroduced:\n" + "\n".join(offenders)


def test_repo_root_has_no_generated_artifacts():
    """The root should hold code, config and the top-level docs. Nothing else."""
    stray = [p.name for p in ROOT.iterdir()
             if p.is_file() and p.suffix.lower() in {".json", ".png", ".svg"}
             and p.name not in ALLOWED]
    assert not stray, f"generated files at the repo root: {stray}"


def test_research_dir_is_committed_and_documented():
    assert (ROOT / "research").is_dir()
    assert (ROOT / "research" / "README.md").exists()
