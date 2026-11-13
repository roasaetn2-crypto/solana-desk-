"""A cold start must never crash on the ledger.

The phone boots with DESK_DATA_DIR pointing at a directory that may not exist
yet (first install, or right after a reset that recreates it). LiveBroker writes
an empty book at construction time, and write_text() does not create missing
parents, so `import app` used to die with FileNotFoundError: live_ledger.json.
That is the exact failure the one-command update script hit on a fresh dir.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from desk.broker import PaperBroker
from desk.live import LiveBroker


def test_paper_broker_saves_into_a_missing_directory(tmp_path):
    target = tmp_path / "does" / "not" / "exist" / "paper_ledger.json"
    b = PaperBroker(target)
    b._save()
    assert target.exists()
    assert json.loads(target.read_text())["cash"] == b.s["cash"]


def test_live_broker_constructs_into_a_missing_directory(tmp_path):
    target = tmp_path / "fresh" / "live_ledger.json"
    lb = LiveBroker(target)          # used to raise FileNotFoundError here
    assert target.exists()
    assert lb.s["cash"] == 0.0
    assert lb.s["positions"] == {}


def test_import_app_with_a_fresh_data_dir(tmp_path):
    """The real cold start: a brand-new DESK_DATA_DIR and `import app`."""
    data = tmp_path / "data"
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.update({"DESK_DATA_DIR": str(data), "DESK_TOKEN": ""})
    out = subprocess.run(
        [sys.executable, "-c", "import app; print('ok')"],
        cwd=root, capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "ok" in out.stdout
