"""The Termux helpers must find the checkout wherever it actually is.

Every helper used to hardcode $HOME/solana-desk, but the GitHub repo is named
`solana-desk-` (trailing dash), so a plain `git clone` produced a directory the
helpers could not see and every one of them died with "No desk dir at
~/solana-desk". That is a naming bug, not an operator mistake, so it is pinned
here: the resolver must find the checkout under either spelling, honour an
explicit DESK_DIR, and refuse a directory that is not a checkout.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "termux" / "lib-deskdir.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _resolve(home: Path, desk_dir: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    env.pop("DESK_DIR", None)
    if desk_dir is not None:
        env["DESK_DIR"] = desk_dir
    script = f'source "{LIB}"; desk_dir'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.py").write_text("MEME = {}\n")
    (path / "app.py").write_text("# app\n")
    return path


def test_finds_trailing_dash_clone(tmp_path):
    want = _checkout(tmp_path / "solana-desk-")
    r = _resolve(tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == str(want)


def test_finds_plain_name_clone(tmp_path):
    want = _checkout(tmp_path / "solana-desk")
    r = _resolve(tmp_path)
    assert r.stdout.strip() == str(want)


def test_finds_shared_storage_clone(tmp_path):
    want = _checkout(tmp_path / "storage" / "shared" / "solana-desk-")
    r = _resolve(tmp_path)
    assert r.stdout.strip() == str(want)


def test_explicit_desk_dir_wins(tmp_path):
    _checkout(tmp_path / "solana-desk-")
    other = _checkout(tmp_path / "elsewhere" / "my-desk")
    r = _resolve(tmp_path, desk_dir=str(other))
    assert r.stdout.strip() == str(other)


def test_explicit_desk_dir_must_be_a_checkout(tmp_path):
    """A wrong DESK_DIR must not be silently accepted -- that is how you end up
    running uvicorn in an empty directory and getting ModuleNotFoundError."""
    _checkout(tmp_path / "solana-desk-")
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _resolve(tmp_path, desk_dir=str(empty))
    # falls through to the real checkout rather than trusting the bad override
    assert r.stdout.strip() == str(tmp_path / "solana-desk-")


def test_reports_failure_when_nothing_exists(tmp_path):
    r = _resolve(tmp_path)
    assert r.returncode != 0
    assert r.stdout.strip() == ""


def test_hint_mentions_the_actual_repo_name(tmp_path):
    script = f'source "{LIB}"; desk_dir_hint'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "HOME": str(tmp_path)})
    assert "solana-desk-.git" in r.stdout
    assert "DESK_DIR=" in r.stdout


def test_no_helper_still_hardcodes_the_old_name():
    """Regression guard: the bug was 10 files agreeing on a wrong default."""
    offenders = []
    for p in (ROOT / "termux").glob("*.sh"):
        if p.name == "lib-deskdir.sh":
            continue
        text = p.read_text()
        if 'DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"' in text:
            offenders.append(p.name)
    assert offenders == [], f"still hardcoding $HOME/solana-desk: {offenders}"
