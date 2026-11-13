"""The Termux supervisor must never produce a port-8000 bind storm.

Real log from the phone:

    ---- 17:41:46Z launching uvicorn ----
    ---- 17:41:47Z launching uvicorn ----
    ---- 17:41:48Z launching uvicorn ----
    ERROR: [Errno 98] error while attempting to bind on address
           ('0.0.0.0', 8000): [errno 98] address already in use
    ---- uvicorn exited (code 3); restarting in 10s ----

Three supervisors had been started (Termux:Boot + update-all + reset-balance).
None of them checked whether another was alive or whether the port was already
serving, so all but one died on bind and every death was respawned forever.

These tests pin the three defences: a singleton lock, a port probe before
launch, and a TERM trap that actually exits.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "termux" / "start-desk.sh"
STOP = ROOT / "termux" / "stop-desk.sh"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_supervisor_takes_a_singleton_lock():
    t = _text(START)
    assert "LOCKDIR=" in t
    assert 'mkdir "$LOCKDIR"' in t
    # a second launch must exit, not fall through to the loop
    assert "not starting a second one" in t


def test_supervisor_probes_the_port_before_launching():
    t = _text(START)
    assert "port_busy()" in t
    assert 'port_busy "$PORT"' in t
    assert "already serving" in t


def test_supervisor_has_backoff_not_a_fixed_ten_seconds():
    t = _text(START)
    assert "fails=$((fails + 1))" in t
    assert "delay=" in t
    assert "restart in ${delay}s" in t


def test_term_trap_exits_so_stop_is_immediate():
    t = _text(START)
    # a trap that only cleans up leaves the loop running (needed SIGKILL before)
    assert re.search(r"trap\s+'cleanup;\s*exit 0'\s+INT TERM HUP", t)
    # and the sleeps must be interruptible, or bash defers the trap
    assert "nap()" in t and "wait $!" in t


def test_stop_script_matches_exact_argv_not_a_substring():
    t = _text(STOP)
    # pgrep -f on a bare name also matches the shell that merely mentions it
    assert "pids_matching()" in t
    assert "/proc/$pid/cmdline" in t
    assert "grep -Eq" in t
    # supervisor must be stopped before uvicorn or it respawns
    assert t.index("start-desk") < t.index("app:app")


def test_scripts_parse():
    for f in (START, STOP):
        out = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr


def test_verify_update_can_fix_itself():
    """It only reported 'MISS ... run: git pull', so re-running changed nothing."""
    t = _text(ROOT / "termux" / "verify-update.sh")
    assert "--fix)" in t
    assert "git pull --ff-only" in t
    assert "stop-desk.sh" in t
    assert 'exec bash "$0" --start' in t


def test_update_all_attaches_a_detached_head():
    """A detached HEAD made `git pull` fail as if it were a network problem."""
    t = _text(ROOT / "termux" / "update-all.sh")
    assert "symbolic-ref -q HEAD" in t
    assert "git checkout main" in t
