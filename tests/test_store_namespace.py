"""Two lanes, one database.

A free Turso plan gives you one database. Running a paper lane and a live lane
on separate Render services therefore means both processes point at the same
kv table and the same journal table. Without namespacing the live lane's
`paper_ledger` key overwrites the paper lane's, and the journal becomes one
interleaved history of two unrelated books -- which silently corrupts both.

DESK_STORE_NAMESPACE separates them. The default (empty) must stay
byte-for-byte identical to the old behaviour so an existing single-lane
deployment is unaffected.
"""
from __future__ import annotations

import pytest

from desk import store


def test_default_namespace_is_empty(monkeypatch):
    monkeypatch.delenv("DESK_STORE_NAMESPACE", raising=False)
    assert store.namespace() == ""
    # legacy behaviour: keys are not prefixed
    assert store._ns_key("paper_ledger") == "paper_ledger"


def test_namespace_prefixes_kv_keys(monkeypatch):
    monkeypatch.setenv("DESK_STORE_NAMESPACE", "paper")
    assert store.namespace() == "paper"
    assert store._ns_key("paper_ledger") == "paper:paper_ledger"


def test_two_lanes_get_different_keys(monkeypatch):
    monkeypatch.setenv("DESK_STORE_NAMESPACE", "paper")
    paper = store._ns_key("paper_ledger")
    monkeypatch.setenv("DESK_STORE_NAMESPACE", "live")
    live = store._ns_key("paper_ledger")
    assert paper != live


def test_namespace_is_sanitised(monkeypatch):
    """It ends up in a key and a SQL parameter, so only safe characters."""
    monkeypatch.setenv("DESK_STORE_NAMESPACE", " Paper Lane!'; DROP TABLE kv-- ")
    ns = store.namespace()
    assert "'" not in ns and ";" not in ns and " " not in ns
    assert ns == "paperlanedroptablekv--"


def test_namespace_is_bounded(monkeypatch):
    monkeypatch.setenv("DESK_STORE_NAMESPACE", "x" * 200)
    assert len(store.namespace()) == 32


def test_journal_insert_and_read_are_namespaced(monkeypatch):
    """The SQL actually carries the namespace, not just the helper."""
    seen = []

    def fake_execute(sql, params=None):
        seen.append((sql, params))
        return {"rows": []}

    monkeypatch.setenv("DESK_STORE_NAMESPACE", "paper")
    monkeypatch.setattr(store, "configured", lambda: True)
    monkeypatch.setattr(store, "_ensure", lambda: True)
    monkeypatch.setattr(store, "execute", fake_execute)

    store.append_journal({"time": "t", "kind": "entry", "mint": "M1"})
    sql, params = seen[-1]
    assert "ns" in sql
    assert params[-1] == "paper"

    list(store.iter_journal_rows(page=10))
    sql2, params2 = seen[-1]
    assert "ns=?" in sql2
    assert params2[0] == "paper"
