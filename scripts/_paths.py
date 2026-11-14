"""Where research artifacts go, resolved from the repo root, not the cwd.

Every script here used to write `backtest_100.json` and friends to whatever
directory you happened to be standing in. Run it from `scripts/` and the
artifact landed in `scripts/`; run it from `~` and it landed in `~`. The repo
root accumulated twenty of these, and the same command produced different
results depending on where you typed it.

`out("name.json")` returns `<repo>/research/name.json` and creates the
directory. Absolute names passed on the command line still work as before --
`--out /tmp/x.json` is respected -- this only changes the *default*.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESEARCH = ROOT / "research"


def out(name: str) -> str:
    """Absolute path for a research artifact, creating the directory."""
    p = Path(name)
    if p.is_absolute():
        return str(p)
    RESEARCH.mkdir(parents=True, exist_ok=True)
    return str(RESEARCH / p.name)


def read(name: str) -> Path:
    """Locate an artifact: research/ first, then the repo root (legacy)."""
    p = Path(name)
    if p.is_absolute():
        return p
    for base in (RESEARCH, ROOT):
        cand = base / p.name
        if cand.exists():
            return cand
    return RESEARCH / p.name
