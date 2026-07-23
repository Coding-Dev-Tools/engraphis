"""Offline tests for engraphis.engines.ingest's entity extraction (the data source
behind the v1 dashboard's interactive Knowledge Graph view). No network or model
download: ingest_document() accepts a pre-computed vector to bypass the real
(sentence-transformers) embedder, which is not part of the offline core dependency set.

Regression coverage for the title/content boundary-bleed bug: naively concatenating
f"{title}\n\n{content}" before running the capitalized-word entity regex let it match
*across* that boundary, fragmenting one real entity (e.g. "Alice Johnson") into several
differently-named graph nodes that each carried only part of its real document set. That
is what made "click a node to see its documents" unreliable in the Graph view — the fix
extracts entities from title and content as separate passes and merges by name.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from engraphis import stores
from engraphis.config import settings
from engraphis.engines import ingest as ingest_engine
from engraphis.stores import graph as graph_store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the v1 store at a fresh temp sqlite file per test and drop any thread-local
    connection cached by a previous test, so tests never share state or touch a real
    engraphis.db."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    stores._local.conn = None
    stores.init_db()
    yield
    stores._local.conn = None


def _ingest(namespace: str, document_id: str, title: str, content: str) -> dict:
    return ingest_engine.ingest_document(
        namespace=namespace, document_id=document_id, title=title, content=content,
        vector=np.zeros(8, dtype=np.float32),   # bypass the real (network) embedder
    )


def _entities_by_name(namespace: str) -> dict:
    snap = graph_store.graph_snapshot(namespace=namespace, limit=100)
    return {e["name"]: e for e in snap["entities"]}


def test_entity_extraction_does_not_bridge_title_and_content():
    """The regression this test guards: title text must never fuse with the first
    entity mention in content into one garbled node."""
    _ingest("ns1", "doc-1", "Meeting Notes", "Alice Johnson met with Bob Smith to discuss Apollo.")
    ents = _entities_by_name("ns1")
    assert "Alice Johnson" in ents
    assert "Meeting Notes" in ents
    assert not any("\n" in name for name in ents)            # no boundary-bleed garbage
    assert not any("Meeting" in name and "Alice" in name for name in ents)


def test_hyphenated_title_word_is_not_fragmented():
    _ingest("ns2", "doc-1", "Follow-up", "Nothing else capitalized here.")
    ents = _entities_by_name("ns2")
    assert "Follow-up" in ents
    assert "Follow" not in ents      # previously leaked out as an orphan one-word entity


def test_same_entity_across_two_documents_links_both():
    """The concrete, user-visible payoff: clicking the "Alice Johnson" node must reach
    every document that mentions her, not just whichever one she was extracted with first."""
    _ingest("ns3", "doc-1", "Meeting Notes", "Alice Johnson met with Bob Smith to discuss Apollo.")
    _ingest("ns3", "doc-2", "Follow-up", "Alice Johnson sent Bob Smith the Apollo roadmap.")
    ents = _entities_by_name("ns3")
    assert set(ents["Alice Johnson"]["documents"]) == {"doc-1", "doc-2"}
    assert set(ents["Bob Smith"]["documents"]) == {"doc-1", "doc-2"}
    assert set(ents["Apollo"]["documents"]) == {"doc-1", "doc-2"}


def test_same_entity_name_isolated_across_namespaces():
    _ingest("ns4a", "doc-1", "", "Alice Johnson works here.")
    _ingest("ns4b", "doc-2", "", "Alice Johnson works there too.")
    a = _entities_by_name("ns4a")["Alice Johnson"]
    b = _entities_by_name("ns4b")["Alice Johnson"]
    assert a["documents"] == ["doc-1"]
    assert b["documents"] == ["doc-2"]


def test_click_target_shape_has_namespace_and_documents():
    """Shape the dashboard's click handler actually reads (static/index.html
    network.on('click', ...) → graphEntityData[name].documents / .namespace)."""
    _ingest("ns5", "doc-1", "Roadmap", "Zephyr project kicked off this week.")
    ents = _entities_by_name("ns5")
    ent = ents["Zephyr"]
    assert ent["documents"] == ["doc-1"]
    assert "preview_title" in ent and "preview_content" in ent


def test_entity_extraction_rejects_long_non_email_in_linear_time():
    """Keep ingestion responsive when untrusted text resembles an email local part
    but never supplies an ``@`` separator."""
    start = time.perf_counter()
    entities = ingest_engine._extract_entities("a" * 100_000)
    elapsed = time.perf_counter() - start

    assert entities == []
    assert elapsed < 1.0
