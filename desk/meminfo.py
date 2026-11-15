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


def _mb_bytes(v) -> float | None:
    """Bytes -> MiB."""
    try:
        return round(int(v) / 1024 / 1024, 1)
    except (TypeError, ValueError):
        return None


def _mb_kb(v) -> float | None:
    """KiB -> MiB (VmRSS and friends are reported in kB)."""
    try:
        return round(int(v) / 1024, 1)
    except (TypeError, ValueError):
        return None


def _rss_mb() -> float | None:
    raw = _read("/proc/self/status") or ""
    for line in raw.splitlines():
        if line.startswith("VmRSS:"):
            return _mb_kb(line.split()[1])
    return None


def journal_size_mb() -> float | None:
    try:
        from desk.ops import data_file
        return round(data_file("journal.jsonl").stat().st_size / 1024 / 1024, 1)
    except Exception:
        return None


def _candidates() -> list[tuple[str, str]]:
    """(label, path) pairs to try, v2 first then v1, root then the process's own
    cgroup path from /proc/self/cgroup."""
    out: list[tuple[str, str]] = [
        ("v2", "/sys/fs/cgroup/memory.current"),
        ("v1", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ]
    rel = ""
    for line in (_read("/proc/self/cgroup") or "").splitlines():
        # v2: "0::/path"   v1: "N:memory:/path"
        parts = line.split(":", 2)
        if len(parts) == 3 and (parts[1] == "" or "memory" in parts[1]):
            rel = parts[2].strip()
            break
    if rel:
        base = "/sys/fs/cgroup" + rel
        out += [
            ("v2-path", base + "/memory.current"),
            ("v1-path", base + "/memory.usage_in_bytes"),
        ]
    return out


def cgroup() -> dict:
    """Current cgroup memory use and the breakdown that matters for OOM.

    ``current_mb`` vs ``max_mb`` is the number the kernel acts on. ``anon_mb``
    is real process memory; ``file_mb`` is page cache (the journal); ``slab_mb``
    is kernel-side. If the box dies with current near max but anon low, the leak
    is file cache, not the Python heap.
    """
    current = None
    source = None
    for label, path in _candidates():
        raw = _read(path)
        if raw is not None:
            current = _mb_bytes(raw)
            source = path
            break

    mx_raw = _read("/sys/fs/cgroup/memory.max")
    if mx_raw is None:
        mx_raw = _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    max_mb = None if mx_raw in (None, "max") else _mb_bytes(mx_raw)

    stat: dict[str, str] = {}
    stat_raw = _read("/sys/fs/cgroup/memory.stat") or _read("/sys/fs/cgroup/memory/memory.stat") or ""
    for line in stat_raw.splitlines():
        parts = line.split()
        if len(parts) == 2:
            stat[parts[0]] = parts[1]

    return {
        "current_mb": current,
        "max_mb": max_mb,
        "anon_mb": _mb_bytes(stat.get("anon") or stat.get("total_rss")),
        "file_mb": _mb_bytes(stat.get("file") or stat.get("total_cache")),
        "slab_mb": _mb_bytes(stat.get("slab") or stat.get("total_slab")),
        "sock_mb": _mb_bytes(stat.get("sock")),
        "shmem_mb": _mb_bytes(stat.get("shmem")),
        "rss_mb": _rss_mb(),
        "journal_file_mb": journal_size_mb(),
        "source": source,
    }
