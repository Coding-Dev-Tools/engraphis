"""Offline tests for the free-text -> entity/relation graph feeder
(engraphis.backends.graph_extractor). numpy-only: no model download, no network.

Covers the regex NER (entity types, single-word stopwords, the title/content
boundary regression), the scoped feed() writer (entities+relations land in the
graph, scoped to one workspace and isolated from another), idempotent re-feed,
defanging of untrusted extractor output, the default 'none' no-op, and the opt-in
wiring on MemoryEngine.remember (regex on -> graph populated; default -> empty).
"""
from __future__ import annotations


from engraphis.backends.graph_extractor import (
    GraphExtraction, NullGraphExtractor, RegexGraphExtractor, feed, get_graph_extractor,
)
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store


def _entity_names(store, workspace_id):
    return {r["name"] for r in store.conn.execute(
        "SELECT name FROM entities WHERE workspace_id=?", (workspace_id,)).fetchall()}


# ── the extractor (pure) ──────────────────────────────────────────────────────

def test_regex_entities_types_and_stopwords():
    ex = RegexGraphExtractor().extract(
        "Alice Johnson emailed bob@acme.com about #launch with @carol. It is done."
    )
    names = {n: t for n, t in ex.entities}
    assert names.get("Alice Johnson") == "person_or_concept"
    assert names.get("bob@acme.com") == "email"
    assert names.get("#launch") == "hashtag"
    assert names.get("@carol") == "mention"
    assert "It" not in names          # single-word stopword dropped


def test_title_content_boundary_no_bleed():
    # title + content must be NER'd as separate passes; concatenating first lets a
    # match bridge the boundary and fragment a real entity.
    ex = RegexGraphExtractor().extract("Alice Johnson met Bob Smith.", title="Meeting Notes")
    names = [n for n, _ in ex.entities]
    assert "Alice Johnson" in names and "Bob Smith" in names
    assert not any("\n" in n for n in names)
    assert not any("Meeting" in n and "Alice" in n for n in names)


def test_relations_by_proximity():
    ex = RegexGraphExtractor().extract("Alice Johnson works at Acme Corporation.")
    assert any(r == "works at" and {s, d} == {"Alice Johnson", "Acme Corporation"}
               for s, r, d in ex.relations)


def test_factory_and_null_default():
    assert isinstance(get_graph_extractor("regex"), RegexGraphExtractor)
    assert isinstance(get_graph_extractor("none"), NullGraphExtractor)
    assert isinstance(get_graph_extractor(""), NullGraphExtractor)
    assert NullGraphExtractor().extract("Alice Johnson works at Acme.").entities == []


# ── the feed() writer (scoped graph) ──────────────────────────────────────────

def test_feed_writes_scoped_entities_and_edges():
    store = Store(":memory:")
    n = feed(store, "Alice Johnson works at Acme Corporation.",
             workspace_id="w1", repo_id="r1", extractor=RegexGraphExtractor())
    assert n["entities"] >= 2 and n["relations"] >= 1
    assert {"Alice Johnson", "Acme Corporation"} <= _entity_names(store, "w1")
    edges = store.edges_in_scope(SearchFilter(workspace_id="w1"))
    assert any(e.relation == "works at" for e in edges)


def test_feed_isolation_between_workspaces():
    store = Store(":memory:")
    feed(store, "Alice Johnson works at Acme Corporation.",
         workspace_id="w1", extractor=RegexGraphExtractor())
    assert _entity_names(store, "w2") == set()
    assert store.edges_in_scope(SearchFilter(workspace_id="w2")) == []


def test_feed_idempotent_refeed():
    store, ex = Store(":memory:"), RegexGraphExtractor()
    feed(store, "Alice Johnson works at Acme Corporation.", workspace_id="w1", extractor=ex)
    before = len(store.edges_in_scope(SearchFilter(workspace_id="w1")))
    again = feed(store, "Alice Johnson works at Acme Corporation.", workspace_id="w1", extractor=ex)
    after = len(store.edges_in_scope(SearchFilter(workspace_id="w1")))
    assert after == before and again["relations"] == 0     # no duplicate edges


def test_feed_defangs_untrusted_names():
    """Extractor output is untrusted (memory-poisoning threat model): control/escape
    chars must be stripped before a name reaches the graph."""
    class _Evil:
        def extract(self, content, *, title=""):
            return GraphExtraction(
                entities=[("Ali\x00ce\x07", "person_or_concept"), ("Bob\x1bSmith", "person_or_concept")],
                relations=[("Ali\x00ce\x07", "is", "Bob\x1bSmith")],
            )
    store = Store(":memory:")
    feed(store, "irrelevant", workspace_id="w1", extractor=_Evil())
    assert _entity_names(store, "w1") == {"Alice", "BobSmith"}
    assert len(store.edges_in_scope(SearchFilter(workspace_id="w1"))) == 1


def test_feed_default_none_writes_nothing():
    store = Store(":memory:")
    n = feed(store, "Alice Johnson works at Acme Corporation.", workspace_id="w1")  # no extractor
    assert n == {"entities": 0, "relations": 0}
    assert _entity_names(store, "w1") == set()


# ── free-tier connectivity: co-occurrence edges ───────────────────────────────

def test_cooccurrence_connects_entities_without_a_relation():
    """The regex extractor finds few proximity relations, so multi-entity memories
    would otherwise be all isolated nodes. Co-occurrence edges connect them, giving
    the Graph tab a real graph and the PPR recall arm something to walk. The number:
    zero isolated entities in a 4-entity memory."""
    store = Store(":memory:")
    feed(store, "Priya Patel, Diego Alvarez and Mei Chen met in Berlin.",
         workspace_id="w1", extractor=RegexGraphExtractor())
    edges = store.edges_in_scope(SearchFilter(workspace_id="w1"))
    assert edges and all(e.relation == "co_occurs" for e in edges)   # no verb -> all co_occurs
    entity_ids = {r["id"] for r in store.conn.execute(
        "SELECT id FROM entities WHERE workspace_id='w1'").fetchall()}
    connected = {e.src for e in edges} | {e.dst for e in edges}
    assert entity_ids <= connected                                   # 0 isolated nodes


def test_cooccurrence_is_idempotent():
    store, ex = Store(":memory:"), RegexGraphExtractor()
    feed(store, "Priya Patel, Diego Alvarez and Mei Chen met in Berlin.",
         workspace_id="w1", extractor=ex)
    before = len(store.edges_in_scope(SearchFilter(workspace_id="w1")))
    again = feed(store, "Priya Patel, Diego Alvarez and Mei Chen met in Berlin.",
                 workspace_id="w1", extractor=ex)
    after = len(store.edges_in_scope(SearchFilter(workspace_id="w1")))
    assert after == before and again["relations"] == 0


def test_cooccurrence_skips_pairs_with_a_specific_relation():
    """A specific relation must win: a 2-entity memory joined by 'works at' gets exactly
    that edge, not a redundant co_occurs alongside it."""
    store = Store(":memory:")
    feed(store, "Alice Johnson works at Acme Corporation.",
         workspace_id="w1", extractor=RegexGraphExtractor())
    edges = store.edges_in_scope(SearchFilter(workspace_id="w1"))
    assert len(edges) == 1 and edges[0].relation == "works at"


def test_entity_canonicalization_merges_leading_article_variants():
    ex = RegexGraphExtractor().extract("The Acme Corporation shipped. Acme Corporation grew.")
    names = [n for n, _ in ex.entities]
    assert "Acme Corporation" in names
    assert "The Acme Corporation" not in names       # leading article stripped -> one node


# ── opt-in wiring on the write path ───────────────────────────────────────────

def test_remember_default_off_leaves_graph_empty():
    eng = MemoryEngine.create(":memory:")                 # graph_extractor defaults to "none"
    eng.remember("Alice Johnson works at Acme Corporation.", workspace_id="w1", repo_id="r1")
    assert eng.store.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] == 0


def test_remember_regex_on_populates_graph():
    eng = MemoryEngine.create(":memory:", graph_extractor="regex")
    eng.remember("Alice Johnson works at Acme Corporation.", workspace_id="w1", repo_id="r1")
    assert {"Alice Johnson", "Acme Corporation"} <= _entity_names(eng.store, "w1")
    assert len(eng.store.edges_in_scope(SearchFilter(workspace_id="w1"))) >= 1
