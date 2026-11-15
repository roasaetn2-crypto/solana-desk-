"""Cgroup memory accounting for the live box.

Process RSS (``/proc/self/status`` VmRSS) only sees the Python heap. On Render
the 512 Mi limit is enforced on the whole cgroup, which also counts file cache,
kernel, slab and sockets. That is why the desk was OOM-killed while the old
probe printed ~85 MB "rss": the number it printed was never the number the
kernel kills on.

This module is read-only and cheap (a few small files), so it is safe to expose
on /api/health. It answers one question precisely: is the growth anonymous
memory (a real leak in the process) or file cache (the journal being rewritten
and re-read on every boot and every health check)?
"""
from __future__ import annotations


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def _mb(v) -> float | None:
    try:
        return round(int(v) / 1024 / 1024, 1)
    except (TypeError, ValueError):
        return None


def _rss_mb() -> float | None:
    raw = _read("/proc/self/status") or ""
    for line in raw.splitlines():
        if line.startswith("VmRSS:"):
            return _mb(line.split()[1])
    return None


def journal_size_mb() -> float | None:
    try:
        from desk.ops import data_file
        return round(data_file("journal.jsonl").stat().st_size / 1024 / 1024, 1)
    except Exception:
        return None


def cgroup() -> dict:
    """Current cgroup memory use and the breakdown that matters for OOM.

    ``current_mb`` vs ``max_mb`` is the number the kernel acts on. ``anon_mb``
    is real process memory; ``file_mb`` is page cache (the journal); ``slab_mb``
    and ``kernel_mb`` are kernel-side. If the box dies with current near max but
    anon low, the leak is file cache, not the Python heap.
    """
    cur = _read("/sys/fs/cgroup/memory.current")
    mx = _read("/sys/fs/cgroup/memory.max")
    stat: dict[str, str] = {}
    for line in (_read("/sys/fs/cgroup/memory.stat") or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            stat[parts[0]] = parts[1]

    out = {
        "current_mb": _mb(cur),
        "max_mb": None if mx in (None, "max") else _mb(mx),
        "anon_mb": _mb(stat.get("anon")),
        "file_mb": _mb(stat.get("file")),
        "slab_mb": _mb(stat.get("slab")),
        "sock_mb": _mb(stat.get("sock")),
        "shmem_mb": _mb(stat.get("shmem")),
        "rss_mb": _rss_mb(),
        "journal_file_mb": journal_size_mb(),
    }
    # cgroup v1 fallback
    if out["current_mb"] is None:
        out["current_mb"] = _mb(_read("/sys/fs/cgroup/memory/memory.usage_in_bytes"))
        out["max_mb"] = _mb(_read("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    return out
