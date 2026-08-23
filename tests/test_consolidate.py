import importlib
import json
import re
import sqlite3
import time

import pytest

from engraphis.core.consolidate import _cluster_by_subject, consolidate
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.service import MemoryService, ValidationError


def _engine_with_repeats():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    texts = [
        "Build failed on the flaky network integration test in CI run 101.",
        "Build failed on the flaky network integration test in CI run 202.",
        "Build failed on the flaky network integration test in CI run 303.",
        "Design review scheduled for the onboarding flow mockups.",   # unrelated
    ]
    for t in texts:
        eng.remember(t, workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                     resolve_conflicts=False)
    return eng, wid, rid


def test_entity_clustering_uses_connected_components_not_first_link_assignment():
    memories = [
        MemoryRecord(id="mem_a", content="a"),
        MemoryRecord(id="mem_b", content="b"),
        MemoryRecord(id="mem_c", content="c"),
    ]

    class IncidenceStore:
        def list_memory_entities(self, _flt, *, memory_ids=None):
            # A bridges X and Y. A first-link implementation splits C away.
            assert memory_ids == ["mem_a", "mem_b", "mem_c"]
            return [
                {"memory_id": "mem_a", "entity_id": "ent_x"},
                {"memory_id": "mem_a", "entity_id": "ent_y"},
                {"memory_id": "mem_b", "entity_id": "ent_x"},
                {"memory_id": "mem_c", "entity_id": "ent_y"},
            ]

    groups = _cluster_by_subject(
        memories, threshold=1.0, store=IncidenceStore(), flt=SearchFilter()
    )
    assert [[memory.id for memory in group] for group in groups] == [
        ["mem_a", "mem_b", "mem_c"]
    ]


def test_service_rejects_non_finite_archive_threshold():
    service = MemoryService.create(":memory:")
    service.create_workspace("w")
    with pytest.raises(ValidationError, match="finite"):
        service.consolidate(workspace="w", archive_below=float("nan"), dry_run=True)


@pytest.mark.parametrize("kwargs", [
    {"archive_below": float("nan")},
    {"archive_below": float("inf")},
    {"subject_jaccard": float("-inf")},
    {"now": float("nan")},
    {"min_cluster": 1},
    {"min_mentions": 51},
    {"min_cluster": True},
    {"min_mentions": False},
    {"archive_below": True},
])
def test_core_rejects_invalid_controls_before_mutation(kwargs):
    eng, wid, rid = _engine_with_repeats()
    before = eng.store.conn.execute(
        "SELECT id, valid_to, valid_to_recorded_at FROM memories ORDER BY id"
    ).fetchall()
    before_changes = eng.store.conn.total_changes

    with pytest.raises(ValueError):
        consolidate(eng, workspace_id=wid, repo_id=rid, **kwargs)

    after = eng.store.conn.execute(
        "SELECT id, valid_to, valid_to_recorded_at FROM memories ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert eng.store.conn.total_changes == before_changes


def test_consolidate_distills_recurring_episodes_into_semantic_digest():
    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert report["clusters_found"] == 1
    assert len(report["digests_created"]) == 1
    digest_id = report["digests_created"][0]["id"]
    digest = eng.store.get_memory(digest_id)
    assert digest.mtype == MemoryType.SEMANTIC
    assert "flaky" in digest.content or "network" in digest.content
    assert digest.metadata["provenance"]["source"] == "consolidation"
    links = eng.store.get_links(digest_id)
    assert sum(1 for link in links if link["relation"] == "consolidates") == 3


def test_consolidate_fills_eligible_episode_cap_after_pending_rows(monkeypatch):
    module = importlib.import_module("engraphis.core.consolidate")
    monkeypatch.setattr(module, "DISTILL_SCAN_LIMIT", 3)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    approved = [
        eng.remember(
            f"Approved deployment recurrence run {index}.", workspace_id=wid,
            repo_id=rid, mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )
        for index in range(3)
    ]
    for index in range(3):
        eng.remember_with_resolution(
            f"Pending deployment recurrence run {index}.", workspace_id=wid,
            repo_id=rid, mtype=MemoryType.EPISODIC, resolve_conflicts=False,
            metadata={"provenance": {"source": "import", "trusted": False,
                                      "review_state": "pending"}},
        )

    report = consolidate(eng, workspace_id=wid, repo_id=rid)

    assert len(report["digests_created"]) == 1
    assert set(report["digests_created"][0]["consolidates"]) == set(approved)


def test_consolidate_is_idempotent():
    eng, wid, rid = _engine_with_repeats()
    first = consolidate(eng, workspace_id=wid, repo_id=rid)
    second = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert len(first["digests_created"]) == 1
    assert len(second["digests_created"]) == 0
    assert second["skipped_already_consolidated"] >= 1


def test_workspace_consolidation_excludes_session_memories():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    sid = eng.store.start_session(wid, rid)
    source_ids = [
        eng.remember(
            f"Session-only deployment incident repeat {n}.",
            workspace_id=wid, repo_id=rid, session_id=sid, scope="session",
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )
        for n in range(3)
    ]

    report = consolidate(eng, workspace_id=wid)

    assert report["digests_created"] == []
    assert all(eng.store.get_memory(mid).valid_to is None for mid in source_ids)


def test_workspace_consolidation_partitions_repo_owned_sources():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    repo_a = eng.store.get_or_create_repo(wid, "a")
    repo_b = eng.store.get_or_create_repo(wid, "b")
    for repo_id, marker in ((repo_a, "REPO_A"), (repo_b, "REPO_B")):
        for n in range(3):
            eng.remember(
                f"Shared deployment incident {marker} run {n}.",
                workspace_id=wid, repo_id=repo_id, mtype=MemoryType.EPISODIC,
                resolve_conflicts=False,
            )

    report = consolidate(eng, workspace_id=wid)

    assert len(report["digests_created"]) == 2
    for entry in report["digests_created"]:
        digest = eng.store.get_memory(entry["id"])
        source_repos = {
            eng.store.get_memory(source_id).repo_id for source_id in entry["consolidates"]
        }
        assert source_repos == {digest.repo_id}


def test_subject_clustering_limits_entity_lookup_to_the_scanned_memories(monkeypatch):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    scanned = eng.remember(
        "The scanned episodic memory has entity evidence.",
        workspace_id=wid, mtype=MemoryType.EPISODIC, resolve_conflicts=False,
    )
    eng.remember(
        "An unrelated episodic memory also has entity evidence.",
        workspace_id=wid, mtype=MemoryType.EPISODIC, resolve_conflicts=False,
    )
    calls = []
    original = eng.store.list_memory_entities

    def limited_lookup(flt, *, entity_ids=None, memory_ids=None, limit=None):
        calls.append(memory_ids)
        return original(
            flt, entity_ids=entity_ids, memory_ids=memory_ids, limit=limit,
        )

    monkeypatch.setattr(eng.store, "list_memory_entities", limited_lookup)
    _cluster_by_subject(
        [eng.store.get_memory(scanned)], threshold=0.5, store=eng.store,
        flt=SearchFilter(workspace_id=wid),
    )

    assert calls == [[scanned]]


def test_consolidate_processes_new_members_of_an_existing_cluster():
    eng, wid, rid = _engine_with_repeats()
    consolidate(eng, workspace_id=wid, repo_id=rid)
    new_ids = [
        eng.remember(
            f"Build failed on the flaky network integration test in CI run {run}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
            resolve_conflicts=False)
        for run in (404, 505, 606)
    ]

    report = consolidate(eng, workspace_id=wid, repo_id=rid)

    assert set(report["digests_created"][0]["consolidates"]) == set(new_ids)


def test_consolidate_dry_run_changes_nothing():
    eng, wid, rid = _engine_with_repeats()
    before = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    report = consolidate(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    after = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    assert report["dry_run"] is True
    assert before == after
    assert report["digests_created"] and "would_consolidate" in report["digests_created"][0]


def test_consolidate_archives_decayed_transients_but_not_pinned():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    stale = eng.remember("Currently blocked on CI quota.", workspace_id=wid, repo_id=rid,
                         mtype=MemoryType.WORKING)
    pinned = eng.remember("Blocked on the vendor contract renewal.", workspace_id=wid,
                          repo_id=rid, mtype=MemoryType.WORKING)
    eng.pin(pinned)
    # Age both far past any plausible retention: tiny stability, ancient
    # last_access, and a back-dated creation so the sweep cannot tie ingest.
    old = time.time() - 90 * 86400
    for mid in (stale, pinned):
        eng.store.conn.execute(
            "UPDATE memories SET stability=0.5, last_access=?, valid_from=? WHERE id=?",
            (old, old, mid))
    eng.store.conn.commit()

    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    archived_ids = {a["id"] for a in report["archived"]}
    assert stale in archived_ids
    assert pinned not in archived_ids
    live = {m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100)}
    assert stale not in live                     # left the live view...
    assert eng.store.get_memory(stale) is not None   # ...but never hard-deleted
    assert pinned in live


def test_consolidate_uses_llm_summary_when_available():
    class FakeLLM:
        def chat(self, messages, system=None, **kw):
            return "CI is flaky on the network integration test; treat failures as retryable."

    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, llm=FakeLLM())
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert digest.content.startswith("CI is flaky")
    assert digest.provenance["trusted"] is False
    assert digest.provenance["review_state"] == "pending"
    assert digest.provenance["derived_by_llm"] is True
    assert digest.metadata["llm_consolidation"]["review_required"] is True
    prompt_ids = {
        memory.id for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid, repo_id=rid), prompt_only=True,
        )
    }
    assert digest.id not in prompt_ids


def test_consolidate_llm_failure_falls_back_to_deterministic():
    class BrokenLLM:
        def chat(self, messages, system=None, **kw):
            raise RuntimeError("provider down")

    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, llm=BrokenLLM())
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert "Recurring pattern" in digest.content
    assert digest.provenance["trusted"] is True
    assert digest.provenance["review_state"] == "approved"


# ── structured LLM consolidation (schema-first, graph-fed, safe fallback) ─────

class _StructuredConsolidationLLM:
    def extract_json(self, prompt, schema):
        self.prompt = prompt
        self.schema = schema
        source_ids = re.findall(r"ID: (mem_[A-Z0-9]+)", prompt)
        return {
            "subject": "Acme API auth tokens",
            "facts": [{
                "content": "Acme API uses PASETO tokens after JWT key rotation failures.",
                "title": "Acme API auth standard",
                "confidence": 0.91,
                "importance": 0.8,
                "keywords": ["Acme API", "PASETO", "JWT"],
                "entities": ["Acme API", "PASETO", "JWT"],
                "relations": [{"source": "Acme API", "relation": "uses",
                               "target": "PASETO", "confidence": 0.9}],
                "source_ids": source_ids[:2],
            }],
        }


class _BrokenStructuredConsolidationLLM:
    def extract_json(self, prompt, schema):
        raise RuntimeError("provider down")


def _engine_with_auth_repeats():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for run in (101, 202, 303):
        eng.remember(
            f"Auth outage: Acme API switched from JWT to PASETO after key rotation "
            f"failed in CI run {run}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
            resolve_conflicts=False)
    return eng, wid, rid


def test_structured_consolidation_keeps_llm_fact_graph_pending_and_sources_live():
    pytest.importorskip("pydantic")
    eng, wid, rid = _engine_with_auth_repeats()
    llm = _StructuredConsolidationLLM()
    report = consolidate(
        eng, workspace_id=wid, repo_id=rid, structured=True, llm=llm,
    )

    assert report["structured"]["attempted"] == 1
    assert report["structured"]["succeeded"] == 1
    entry = report["digests_created"][0]
    assert entry["structured"] is True
    digest = eng.store.get_memory(entry["id"])
    assert digest.mtype == MemoryType.SEMANTIC
    assert digest.metadata["provenance"]["source"] == "structured_consolidation"
    assert digest.provenance["trusted"] is False
    assert digest.provenance["review_state"] == "pending"
    assert digest.provenance["derived_by_llm"] is True
    assert digest.metadata["structured_consolidation"]["confidence"] == 0.91
    assert digest.confidence == 0.91
    deferred_graph = digest.metadata["unverified_derived_graph"]
    assert deferred_graph["entities"] == ["Acme API", "PASETO", "JWT"]
    assert deferred_graph["relations"][0]["relation"] == "uses"
    assert "source_ids" in digest.metadata["provenance"]
    llm_audit = digest.metadata["structured_consolidation"]["llm"]
    assert len(llm_audit["prompt_sha256"]) == 64
    assert len(llm_audit["response_sha256"]) == 64

    # Valid source IDs prove lineage, not entailment. The fact and graph hints remain
    # pending, while authoritative source episodes remain live.
    assert eng.store.list_entities(SearchFilter(workspace_id=wid, repo_id=rid)) == []
    assert eng.store.edges_in_scope(SearchFilter(workspace_id=wid, repo_id=rid)) == []
    prompt_ids = {
        memory.id for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid, repo_id=rid), prompt_only=True,
        )
    }
    assert digest.id not in prompt_ids
    source_ids = digest.provenance["source_ids"]
    assert len(source_ids) == 2
    live_ids = {
        memory.id
        for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid), limit=20,
        )
    }
    assert set(source_ids) <= live_ids
    assert all(eng.store.get_memory(source_id).valid_to is None
               for source_id in source_ids)
    episodes = [
        memory for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid), include_invalid=True, limit=20,
        )
        if memory.mtype == MemoryType.EPISODIC
    ]
    assert sum(memory.valid_to is None for memory in episodes) == 3


def test_structured_consolidation_blocks_graph_writes_for_untrusted_sources():
    pytest.importorskip("pydantic")
    eng, wid, rid = _engine_with_auth_repeats()
    eng.store.conn.execute("UPDATE memories SET provenance='{\"trusted\": false}'")
    eng.store.conn.commit()

    report = consolidate(
        eng,
        workspace_id=wid,
        repo_id=rid,
        structured=True,
        llm=_StructuredConsolidationLLM(),
    )

    # Pending inputs cannot be supplied to an LLM consolidator or create a
    # derivative graph bridge. Review/approval comes before any derived state.
    assert report["digests_created"] == []
    assert eng.store.edges_in_scope(SearchFilter(workspace_id=wid, repo_id=rid)) == []



def test_structured_consolidation_failure_falls_back_to_deterministic_digest():
    eng, wid, rid = _engine_with_auth_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, structured=True,
                         llm=_BrokenStructuredConsolidationLLM())
    assert report["structured"]["attempted"] == 1
    assert report["structured"]["fallbacks"] == 1
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert "Recurring pattern" in digest.content
    assert digest.metadata["provenance"]["source"] == "consolidation"


def test_structured_consolidation_bounds_prompt_sources_and_output_facts():
    pytest.importorskip("pydantic")
    module = importlib.import_module("engraphis.core.consolidate")
    eng, wid, rid = _engine_with_large_cluster(
        n=module.STRUCTURED_MAX_SOURCE_ITEMS + 3,
    )

    class OverproducingLLM:
        prompt_source_ids = []

        def extract_json(self, prompt, _schema):
            self.prompt_source_ids = re.findall(r"^ID: (mem_[^\s]+)$", prompt, re.M)
            valid_facts = [
                {
                    "content": f"Bounded durable fact {index}.",
                    "confidence": float("nan"),
                    "importance": float("inf"),
                    "source_ids": ["mem_not_in_prompt", *self.prompt_source_ids],
                }
                for index in range(module.STRUCTURED_MAX_FACTS)
            ]
            return {
                "facts": valid_facts + [
                    {"content": None}
                    for _ in range(3)
                ],
            }

    llm = OverproducingLLM()
    report = consolidate(
        eng, workspace_id=wid, repo_id=rid, structured=True, llm=llm,
    )

    assert len(llm.prompt_source_ids) == module.STRUCTURED_MAX_SOURCE_ITEMS
    assert report["digests_created"][0]["facts"] == module.STRUCTURED_MAX_FACTS
    assert len(report["digests_created"][0]["ids"]) == module.STRUCTURED_MAX_FACTS
    for memory_id in report["digests_created"][0]["ids"]:
        memory = eng.store.get_memory(memory_id)
        source_ids = memory.metadata["structured_consolidation"]["source_ids"]
        assert 0 < len(source_ids) <= module.STRUCTURED_MAX_SOURCE_ITEMS
        assert "mem_not_in_prompt" not in source_ids
        assert memory.confidence == 0.0
        assert memory.importance == pytest.approx(0.5)


def test_malformed_structured_output_leaves_no_partial_structured_writes():
    pytest.importorskip("pydantic")
    class MalformedLLM:
        def extract_json(self, _prompt, _schema):
            return {"facts": {"content": "not a fact list"}}

    eng, wid, rid = _engine_with_auth_repeats()
    source_ids = {
        memory.id
        for memory in eng.store.list_memories(
            SearchFilter(
                workspace_id=wid, repo_id=rid, mtypes=[MemoryType.EPISODIC],
            ),
        )
    }
    report = consolidate(
        eng, workspace_id=wid, repo_id=rid, structured=True, llm=MalformedLLM(),
    )

    assert report["structured"]["fallbacks"] == 1
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE action='distill_structured'"
    ).fetchone()[0] == 0
    assert all(eng.store.get_memory(source_id).valid_to is None for source_id in source_ids)
    assert not [
        memory
        for memory in eng.store.list_memories(
            SearchFilter(
                workspace_id=wid, repo_id=rid, mtypes=[MemoryType.SEMANTIC],
            ),
        )
        if memory.provenance.get("source") == "structured_consolidation"
    ]


def test_structured_workspace_consolidation_partitions_repo_owned_sources():
    pytest.importorskip("pydantic")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    repo_a = eng.store.get_or_create_repo(wid, "a")
    repo_b = eng.store.get_or_create_repo(wid, "b")
    for repo_id, marker in ((repo_a, "REPO_A"), (repo_b, "REPO_B")):
        for n in range(3):
            eng.remember(
                f"Auth incident {marker} PASETO outage run {n}.",
                workspace_id=wid, repo_id=repo_id, mtype=MemoryType.EPISODIC,
                resolve_conflicts=False,
            )

    report = consolidate(
        eng, workspace_id=wid, structured=True, llm=_StructuredConsolidationLLM(),
    )

    assert len(report["digests_created"]) == 2
    for entry in report["digests_created"]:
        digest = eng.store.get_memory(entry["id"])
        source_repos = {
            eng.store.get_memory(source_id).repo_id
            for source_id in digest.metadata["structured_consolidation"]["source_ids"]
        }
        assert source_repos == {digest.repo_id}


def test_structured_consolidation_rejects_facts_without_prompt_sources():
    class HallucinatedSourceLLM:
        def extract_json(self, prompt, schema):
            return {
                "subject": "auth",
                "facts": [{
                    "content": "Use an invented authentication standard.",
                    "title": "Invented standard",
                    "confidence": 0.9,
                    "source_ids": ["mem_NOT_IN_PROMPT"],
                }],
            }

    eng, wid, rid = _engine_with_auth_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, structured=True,
                         llm=HallucinatedSourceLLM())

    assert report["structured"]["succeeded"] == 0
    assert report["structured"]["fallbacks"] == 1
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert digest.metadata["provenance"]["source"] == "consolidation"


def test_structured_consolidation_does_not_trust_invented_claim_with_valid_sources():
    pytest.importorskip("pydantic")
    class HallucinatedClaimLLM:
        def extract_json(self, prompt, schema):
            source_ids = re.findall(r"ID: (mem_[A-Z0-9]+)", prompt)
            return {
                "subject": "invented deployment",
                "facts": [{
                    "content": "Acme API stores production keys on a lunar relay.",
                    "title": "Invented lunar relay",
                    "confidence": 0.99,
                    "entities": ["Acme API", "Lunar Relay"],
                    "relations": [{
                        "source": "Acme API",
                        "relation": "stores_keys_on",
                        "target": "Lunar Relay",
                        "confidence": 0.99,
                    }],
                    "source_ids": source_ids[:2],
                }],
            }

    eng, wid, rid = _engine_with_auth_repeats()
    report = consolidate(
        eng,
        workspace_id=wid,
        repo_id=rid,
        structured=True,
        llm=HallucinatedClaimLLM(),
    )

    entry = report["digests_created"][0]
    digest = eng.store.get_memory(entry["id"])
    assert digest.provenance["trusted"] is False
    assert digest.provenance["review_state"] == "pending"
    assert digest.provenance["source_ids"]
    assert eng.store.list_entities(SearchFilter(workspace_id=wid, repo_id=rid)) == []
    assert eng.store.edges_in_scope(SearchFilter(workspace_id=wid, repo_id=rid)) == []
    assert all(
        eng.store.get_memory(source_id).valid_to is None
        for source_id in digest.provenance["source_ids"]
    )


def test_consolidation_repairs_already_open_legacy_structured_graph_state():
    from engraphis.core.interfaces import Edge, Node

    eng, wid, rid = _engine_with_auth_repeats()
    sources = [
        memory for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid, repo_id=rid)
        )
        if memory.mtype == MemoryType.EPISODIC
    ]
    legacy_id = eng.remember(
        "A governed legacy structured claim.", workspace_id=wid, repo_id=rid,
        mtype=MemoryType.SEMANTIC, resolve_conflicts=False,
    )
    provenance = {
        "source": "structured_consolidation",
        "trusted": True,
        "review_state": "approved",
        "source_ids": [sources[0].id],
        "consolidates": [sources[0].id],
    }
    metadata = {
        "provenance": provenance,
        "entities": ["Acme API", "Lunar Relay"],
        "relations": [{
            "source": "Acme API", "relation": "stores_keys_on",
            "target": "Lunar Relay",
        }],
    }
    eng.store.conn.execute(
        "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
        (json.dumps(provenance), json.dumps(metadata), legacy_id),
    )
    eng.store.conn.commit()
    eng.store.add_link(legacy_id, sources[0].id, "consolidates")
    eng.store.add_link(legacy_id, sources[1].id, "related")
    api_id = eng.store.upsert_entity(Node(
        id="", name="Acme API", workspace_id=wid, repo_id=rid,
    ))
    relay_id = eng.store.upsert_entity(Node(
        id="", name="Lunar Relay", workspace_id=wid, repo_id=rid,
    ))
    edge_id = eng.store.upsert_edge(Edge(
        id="", src=api_id, dst=relay_id, relation="stores_keys_on",
        workspace_id=wid, repo_id=rid,
        provenance={"source": "structured_extractor", "memory_id": legacy_id},
    ))
    eng.store.link_memory_entity(
        memory_id=legacy_id, entity_id=relay_id, workspace_id=wid, repo_id=rid,
        provenance={"source": "structured_extractor", "memory_id": legacy_id},
    )

    report = consolidate(eng, workspace_id=wid, repo_id=rid, min_cluster=20)

    assert report["errors"] == []
    repaired = eng.store.get_memory(legacy_id)
    assert repaired.provenance["trusted"] is False
    assert repaired.provenance["review_state"] == "pending"
    assert repaired.provenance["derived_by_llm"] is True
    assert repaired.provenance["derived_graph_inert"] is True
    assert repaired.metadata["provenance"] == repaired.provenance
    assert "entities" not in repaired.metadata
    assert "relations" not in repaired.metadata
    assert repaired.metadata["unverified_derived_graph"]["entities"] == [
        "Acme API", "Lunar Relay",
    ]
    assert eng.store.conn.execute(
        "SELECT valid_to FROM edges WHERE id=?", (edge_id,)
    ).fetchone()["valid_to"] is not None
    assert {link["relation"] for link in eng.store.get_links(legacy_id)} == {
        "consolidates"
    }




# ── compaction token-accounting (made a number) ───────

def _engine_with_large_cluster(n: int = 12):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for i in range(n):
        eng.remember(
            f"Nightly deploy to staging failed because the migration lock timed out, run {i}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC, resolve_conflicts=False)
    return eng, wid, rid


def test_consolidate_reports_compaction_savings_on_a_real_cluster():
    eng, wid, rid = _engine_with_large_cluster()
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    comp = report["compaction"]["distilled"]
    assert comp == {
        "tokens_before": 230,
        "tokens_after": 120,
        "tokens_saved": 110,
        "reduction_pct": 47.8,
        "units": 1,
    }
    assert comp["tokens_before"] > comp["tokens_after"] > 0
    assert comp["tokens_saved"] == comp["tokens_before"] - comp["tokens_after"]
    assert 0 < comp["reduction_pct"] <= 100
    # every digest entry carries its own before/after so the report is auditable
    entry = report["digests_created"][0]
    for key in ("tokens_before", "tokens_after", "tokens_saved", "reduction_pct"):
        assert key in entry
    assert report["compaction"]["total_tokens_saved"] >= comp["tokens_saved"]


def test_consolidate_archive_reports_freed_tokens():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("Temporary: blocked on CI quota until the weekend.",
                       workspace_id=wid, repo_id=rid, mtype=MemoryType.WORKING)
    # Back-date creation too: a same-tick sweep would defer the archive
    # instead of closing it, and this test asserts the closed-path report.
    old = time.time() - 90 * 86400
    eng.store.conn.execute(
        "UPDATE memories SET stability=0.5, last_access=?, valid_from=? WHERE id=?",
        (old, old + 1.0, mid))
    eng.store.conn.commit()
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert report["archived"] and report["archived"][0]["tokens_freed"] > 0
    assert report["compaction"]["archived_tokens_freed"] >= report["archived"][0]["tokens_freed"]


def test_consolidate_dry_run_reports_compaction_without_writing():
    eng, wid, rid = _engine_with_large_cluster()
    before = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    report = consolidate(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    after = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    assert before == after                                   # nothing written
    assert report["compaction"]["distilled"]["tokens_saved"] > 0   # but savings estimated


# ── entity Profiles pass (a "profile that grows with you") ──────────

def _engine_with_entity_mentions(name: str = "Aurora", n: int = 8):
    from engraphis.core.interfaces import Node
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    lines = [
        f"{name} prefers PASETO over JWT because key rotation was painful last quarter.",
        f"{name} approved raising the API rate limit to 500 requests per minute.",
        f"{name} owns the billing-service migration and wants it done before the freeze.",
        f"{name} dislikes force-pushes to shared branches on the deploy repo.",
        f"{name} asked that all new endpoints ship with contract tests attached.",
        f"{name} reviewed the incident and blamed the migration lock timeout.",
        f"{name} set the staging deploy window to weekday evenings only.",
        f"{name} keeps the on-call runbook in the ops workspace, not the wiki.",
    ][:n]
    for t in lines:
        eng.remember(t, workspace_id=wid, repo_id=rid, mtype=MemoryType.SEMANTIC,
                     resolve_conflicts=False)
    eng.store.upsert_entity(Node(id="", name=name, ntype="person", workspace_id=wid, repo_id=rid))
    return eng, wid, rid, name


def test_profiles_pass_rolls_entity_memories_into_one_digest():
    from engraphis.core.consolidate import consolidate_profiles
    eng, wid, rid, name = _engine_with_entity_mentions()
    report = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)
    assert report["entities_considered"] == 1
    assert len(report["profiles_created"]) == 1
    entry = report["profiles_created"][0]
    assert entry["entity"] == name and entry["mentions"] == 8
    prof = eng.store.get_memory(entry["id"])
    assert prof.mtype == MemoryType.SEMANTIC
    assert prof.title == f"Profile: {name}"
    assert prof.metadata["provenance"]["source"] == "profile_consolidation"
    assert prof.provenance["trusted"] is True
    assert prof.provenance["review_state"] == "approved"
    links = eng.store.get_links(entry["id"])
    assert sum(1 for link in links if link["relation"] == "profiles") == 8
    assert report["compaction"]["tokens_before"] > report["compaction"]["tokens_after"] > 0


def test_llm_profile_summary_remains_pending_until_human_review():
    from engraphis.core.consolidate import consolidate_profiles

    class HallucinatedProfileLLM:
        def chat(self, messages, system=None, **kwargs):
            return "Aurora secretly operates a lunar payment relay."

    eng, wid, rid, _ = _engine_with_entity_mentions()
    report = consolidate_profiles(
        eng, workspace_id=wid, repo_id=rid, llm=HallucinatedProfileLLM(),
    )

    profile = eng.store.get_memory(report["profiles_created"][0]["id"])
    assert profile.content.startswith("Aurora secretly operates")
    assert profile.provenance["trusted"] is False
    assert profile.provenance["review_state"] == "pending"
    assert profile.provenance["derived_by_llm"] is True
    prompt_audit = profile.metadata["llm_consolidation"]
    assert prompt_audit["review_required"] is True
    assert prompt_audit["source_count"] == 8
    assert prompt_audit["kind"] == "entity_profile"
    assert prompt_audit["prompt_source_ids"] == profile.provenance["profiles"]
    assert prompt_audit["prompt_source_count"] == 8
    assert prompt_audit["prompt_omitted_count"] == 0
    prompt_ids = {
        memory.id for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid, repo_id=rid), prompt_only=True,
        )
    }
    assert profile.id not in prompt_ids


def test_profiles_batch_all_eligible_memories(monkeypatch):
    from engraphis.core import consolidate as consolidate_module
    from engraphis.core.consolidate import consolidate_profiles

    monkeypatch.setattr(consolidate_module, "PROFILE_SCAN_LIMIT", 2)
    eng, wid, rid, name = _engine_with_entity_mentions()

    report = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)

    assert report["errors"] == []
    assert len(report["profiles_created"]) == 1
    assert report["profiles_created"][0]["entity"] == name
    assert report["profiles_created"][0]["mentions"] == 8

def test_profiles_rotate_bounded_memory_window(monkeypatch):
    from engraphis.core import consolidate as consolidate_module
    from engraphis.core.consolidate import consolidate_profiles
    from engraphis.core.interfaces import Node

    monkeypatch.setattr(consolidate_module, "PROFILE_SCAN_LIMIT", 3)
    monkeypatch.setattr(consolidate_module, "PROFILE_MEMORY_LIMIT", 3)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for index in range(6):
        eng.remember(
            f"Maintenance note placeholder {index}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.SEMANTIC,
            resolve_conflicts=False,
        )
    flt = SearchFilter(workspace_id=wid, repo_id=rid)
    first_page = eng.store.list_memories_page(flt, after_id="", limit=3)
    second_page = eng.store.list_memories_page(
        flt, after_id=first_page[-1].id, limit=3,
    )
    assert len(first_page) == len(second_page) == 3
    for memory in first_page:
        eng.store.conn.execute(
            "UPDATE memories SET content=? WHERE id=?",
            ("Unrelated maintenance note.", memory.id),
        )
    for index, memory in enumerate(second_page):
        eng.store.conn.execute(
            "UPDATE memories SET content=? WHERE id=?",
            (f"Aurora owns the deployment runbook section {index}.", memory.id),
        )
    eng.store.conn.commit()
    eng.store.upsert_entity(
        Node(id="", name="Aurora", ntype="person", workspace_id=wid, repo_id=rid)
    )

    first = consolidate_profiles(eng, workspace_id=wid, repo_id=rid, min_mentions=3)
    assert first["profiles_created"] == []
    assert eng.store.get_maintenance_cursor(
        wid, rid, consolidate_module.PROFILE_CURSOR_NAME,
    )

    second = consolidate_profiles(eng, workspace_id=wid, repo_id=rid, min_mentions=3)
    assert len(second["profiles_created"]) == 1
    profile_id = second["profiles_created"][0]["id"]
    assert sum(
        link["relation"] == "profiles"
        for link in eng.store.get_links(profile_id)
    ) == 3


def test_profile_nested_cursors_do_not_starve_mismatched_pages(monkeypatch):
    from engraphis.core import consolidate as consolidate_module
    from engraphis.core.consolidate import consolidate_profiles
    from engraphis.core.interfaces import Node

    monkeypatch.setattr(consolidate_module, "PROFILE_SCAN_LIMIT", 3)
    monkeypatch.setattr(consolidate_module, "PROFILE_MEMORY_LIMIT", 2)
    monkeypatch.setattr(consolidate_module, "PROFILE_ENTITY_LIMIT", 1)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for index in range(2):
        eng.remember(
            f"Zeta owns durable workflow {index}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.SEMANTIC,
            resolve_conflicts=False,
        )
    eng.remember(
        "Noise owns an unrelated note.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.SEMANTIC,
        resolve_conflicts=False,
    )
    eng.store.upsert_entity(
        Node(id="", name="Noise", ntype="topic", workspace_id=wid, repo_id=rid)
    )
    eng.store.upsert_entity(
        Node(id="", name="Zeta", ntype="person", workspace_id=wid, repo_id=rid)
    )

    created = []
    for _ in range(4):
        report = consolidate_profiles(
            eng, workspace_id=wid, repo_id=rid, min_mentions=2,
        )
        created.extend(report["profiles_created"])

    assert [entry["entity"] for entry in created] == ["Zeta"]

def test_profiles_overlap_entity_boundary(monkeypatch):
    from engraphis.core import consolidate as consolidate_module
    from engraphis.core.consolidate import consolidate_profiles
    from engraphis.core.interfaces import Node

    monkeypatch.setattr(consolidate_module, "PROFILE_SCAN_LIMIT", 3)
    monkeypatch.setattr(consolidate_module, "PROFILE_MEMORY_LIMIT", 3)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for index in range(6):
        eng.remember(
            f"marker{index} value{index} signal{index}.",
            workspace_id=wid, repo_id=rid,
            mtype=MemoryType.SEMANTIC, resolve_conflicts=False,
        )
    flt = SearchFilter(workspace_id=wid, repo_id=rid)
    first_page = eng.store.list_memories_page(flt, after_id="", limit=3)
    second_page = eng.store.list_memories_page(
        flt, after_id=first_page[-1].id, limit=3,
    )
    assert len(first_page) == len(second_page) == 3
    for memory in first_page:
        eng.store.conn.execute(
            "UPDATE memories SET content=? WHERE id=?",
            ("Unrelated deployment note.", memory.id),
        )
    for index, memory in enumerate(second_page):
        eng.store.conn.execute(
            "UPDATE memories SET content=? WHERE id=?",
            (f"Aurora owns the deployment runbook section {index}.", memory.id),
        )
    eng.store.conn.commit()
    eng.store.upsert_entity(
        Node(id="", name="Aurora", ntype="person", workspace_id=wid, repo_id=rid)
    )

    first = consolidate_profiles(eng, workspace_id=wid, repo_id=rid, min_mentions=3)
    assert first["profiles_created"] == []

    second = consolidate_profiles(eng, workspace_id=wid, repo_id=rid, min_mentions=3)
    assert len(second["profiles_created"]) == 1
    profile_id = second["profiles_created"][0]["id"]
    assert sum(
        link["relation"] == "profiles"
        for link in eng.store.get_links(profile_id)
    ) == 3

def test_profile_retry_completes_an_interrupted_link_set(monkeypatch):
    from engraphis.core.consolidate import consolidate_profiles

    eng, wid, rid, _ = _engine_with_entity_mentions()
    original_add_link = eng.store.add_link
    state = {"calls": 0}

    def fail_once(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            raise RuntimeError("link store unavailable")
        return original_add_link(*args, **kwargs)

    monkeypatch.setattr(eng.store, "add_link", fail_once)
    first = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)
    assert len(first["profiles_created"]) == 0
    assert len(first["errors"]) == 1

    second = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)

    assert second["errors"] == []
    profiles = [
        memory for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid, repo_id=rid, mtypes=[MemoryType.SEMANTIC])
        )
        if memory.metadata.get("provenance", {}).get("source") == "profile_consolidation"
    ]
    assert len(profiles) == 1
    assert sum(link["relation"] == "profiles"
               for link in eng.store.get_links(profiles[0].id)) == 8

def test_profiles_pass_via_consolidate_flag_and_is_idempotent():
    eng, wid, rid, _ = _engine_with_entity_mentions()
    first = consolidate(eng, workspace_id=wid, repo_id=rid, profiles=True)
    second = consolidate(eng, workspace_id=wid, repo_id=rid, profiles=True)
    assert len(first["profiles"]["profiles_created"]) == 1
    assert len(second["profiles"]["profiles_created"]) == 0
    assert second["profiles"]["skipped_existing"] >= 1


def test_profiles_pass_respects_min_mentions():
    from engraphis.core.consolidate import consolidate_profiles
    eng, wid, rid, _ = _engine_with_entity_mentions(name="Rare", n=2)
    report = consolidate_profiles(eng, workspace_id=wid, repo_id=rid, min_mentions=3)
    assert report["profiles_created"] == []


def test_workspace_profiles_partition_repo_owned_sources():
    from engraphis.core.consolidate import consolidate_profiles
    from engraphis.core.interfaces import Node

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    repo_a = eng.store.get_or_create_repo(wid, "a")
    repo_b = eng.store.get_or_create_repo(wid, "b")
    for repo_id, marker in ((repo_a, "REPO_A"), (repo_b, "REPO_B")):
        for n in range(3):
            eng.remember(
                f"Aurora {marker} architectural decision {n}.",
                workspace_id=wid, repo_id=repo_id, mtype=MemoryType.SEMANTIC,
                resolve_conflicts=False,
            )
    eng.store.upsert_entity(Node(id="", name="Aurora", workspace_id=wid))

    report = consolidate_profiles(eng, workspace_id=wid)

    assert len(report["profiles_created"]) == 2
    for entry in report["profiles_created"]:
        profile = eng.store.get_memory(entry["id"])
        source_repos = {
            eng.store.get_memory(
                link["b"] if link["a"] == profile.id else link["a"]
            ).repo_id
            for link in eng.store.get_links(profile.id)
            if link["relation"] == "profiles"
        }
        assert source_repos == {profile.repo_id}


def test_profiles_dry_run_changes_nothing():
    from engraphis.core.consolidate import consolidate_profiles
    eng, wid, rid, _ = _engine_with_entity_mentions()
    before = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    report = consolidate_profiles(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    after = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    assert before == after
    assert report["profiles_created"] and "would_profile" in report["profiles_created"][0]


def test_profiles_do_not_match_entity_names_inside_other_words():
    from engraphis.core.consolidate import consolidate_profiles
    from engraphis.core.interfaces import Node

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.store.upsert_entity(Node(
        id="", name="Redis", ntype="tech", workspace_id=wid, repo_id=rid))
    for run in range(3):
        eng.remember(
            f"We rediscovered an unrelated archive in run {run}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
            resolve_conflicts=False)

    report = consolidate_profiles(
        eng, workspace_id=wid, repo_id=rid, min_mentions=3)

    assert report["profiles_created"] == []


# ── safety inheritance: a digest may not launder its sources ────────────────────────
#
# Every consolidation write quotes source text verbatim, but ``engine.remember()`` takes
# no ``sensitivity`` argument and defaults ``provenance.trusted`` to True. Since
# ``SyncEngine.export_bundle`` filters on ``sensitivity != 'secret'``, an un-inherited
# digest would ferry secret quotes to every other machine — and hand a poisoned source's
# text a trusted label. ``merge``/``correct``/``promote`` already inherit; these pin the
# consolidation paths to the same rule.

def _cluster_with_one_secret_untrusted_source():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    ids = []
    for run in (101, 202, 303):
        ids.append(eng.remember(
            f"Build failed on the flaky network integration test in CI run {run}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
            metadata={"provenance": {"trusted": False}} if run == 202 else None,
            resolve_conflicts=False))
    eng.store.conn.execute(
        "UPDATE memories SET sensitivity='secret' WHERE id=?", (ids[0],))
    eng.store.conn.commit()
    return eng, wid, rid, ids


def test_digest_is_not_created_from_a_cluster_with_pending_sources():
    eng, wid, rid, ids = _cluster_with_one_secret_untrusted_source()

    report = consolidate(eng, workspace_id=wid, repo_id=rid)

    # A source cluster is a derived-state read. One pending source blocks it
    # rather than letting a digest carry a downgraded copy of source text.
    assert report["digests_created"] == []
    assert all(eng.store.get_memory(memory_id).valid_to is None for memory_id in ids)


def test_untrusted_consolidation_never_reaches_graph_extraction():
    from engraphis.backends.graph_extractor import GraphExtraction

    class RecordingGraphExtractor:
        def __init__(self):
            self.calls = []

        def extract(self, content, *, title=""):
            self.calls.append((content, title))
            return GraphExtraction()

    eng, wid, rid, _ = _cluster_with_one_secret_untrusted_source()
    extractor = RecordingGraphExtractor()
    eng.graph_extractor = extractor
    evolved = []

    def record_evolution(memory_id, *args, **kwargs):
        evolved.append(memory_id)
        return []

    eng._evolve = record_evolution

    report = consolidate(eng, workspace_id=wid, repo_id=rid)

    assert report["digests_created"] == []
    assert extractor.calls == []
    assert evolved == []


def test_unlabelled_legacy_sources_fail_closed_during_consolidation():
    eng, wid, rid, source_ids = _cluster_with_one_secret_untrusted_source()
    eng.store.conn.executemany(
        "UPDATE memories SET provenance='{}' WHERE id=?",
        [(source_id,) for source_id in source_ids],
    )
    eng.store.conn.commit()

    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert report["digests_created"] == []


def test_profile_digest_excludes_pending_sources():
    from engraphis.core.consolidate import consolidate_profiles

    eng, wid, rid, name = _engine_with_entity_mentions()
    source = eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100)[0]
    eng.store.conn.execute(
        "UPDATE memories SET sensitivity='sensitive', provenance='{\"trusted\": false}' "
        "WHERE id=?", (source.id,))
    eng.store.conn.commit()
    evolved = []

    def record_evolution(memory_id, *args, **kwargs):
        evolved.append(memory_id)
        return []

    eng._evolve = record_evolution

    report = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)

    assert len(report["profiles_created"]) == 1
    profile = eng.store.get_memory(report["profiles_created"][0]["id"])
    assert source.id not in profile.metadata["provenance"]["profiles"]
    assert profile.provenance["review_state"] == "approved"
    assert evolved == [profile.id]


# ── scan-limit regression: the type filter must run in SQL, not in Python ───────────
#
# ``store.list_memories`` truncates with ``ORDER BY ingested_at DESC LIMIT n``. Filtering
# by ``mtype`` afterwards means that once the newest n rows are all of the wrong type,
# every pass sees zero candidates and reports a clean, empty sweep — a silent wrong
# answer rather than an error. These shrink the budget instead of writing 2000 rows.

def test_distill_pass_sees_episodics_behind_newer_semantic_rows(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DISTILL_SCAN_LIMIT", 4)
    eng, wid, rid = _engine_with_repeats()          # 4 episodic rows, 3 of them a cluster
    for n in range(6):                              # …then bury them under newer rows
        eng.remember(f"Durable architecture note {n} about module layout.",
                     workspace_id=wid, repo_id=rid, mtype=MemoryType.SEMANTIC,
                     resolve_conflicts=False)

    report = consolidate(eng, workspace_id=wid, repo_id=rid)

    assert len(report["digests_created"]) == 1, "old code truncated to 6 semantic rows"

def test_distill_batches_all_eligible_episodes(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DISTILL_SCAN_LIMIT", 2)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    source_ids = [
        eng.remember(
            f"Recurring deploy failure on the flaky integration test, run {index}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
            resolve_conflicts=False,
        )
        for index in range(6)
    ]

    report = consolidate(eng, workspace_id=wid, repo_id=rid)

    assert len(report["digests_created"]) == 1
    assert set(report["digests_created"][0]["consolidates"]) == set(source_ids)
    assert report["errors"] == []

def test_distill_cursor_rotates_past_unclusterable_window(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DISTILL_SCAN_LIMIT", 3)
    monkeypatch.setattr(consolidate_module, "DISTILL_CLUSTER_LIMIT", 3)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for content in (
        "Amber protocol observation.",
        "Cobalt ledger anomaly.",
        "Violet queue measurement.",
    ):
        eng.remember(
            content, workspace_id=wid, repo_id=rid,
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )

    first = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert first["clusters_found"] == 0
    assert eng.store.get_maintenance_cursor(
        wid, rid, consolidate_module.DISTILL_CURSOR_NAME,
    )

    source_ids = [
        eng.remember(
            f"Recurring deploy failure during run {index}.",
            workspace_id=wid, repo_id=rid,
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )
        for index in range(3)
    ]
    second = consolidate(eng, workspace_id=wid, repo_id=rid)

    assert len(second["digests_created"]) == 1
    assert set(second["digests_created"][0]["consolidates"]) == set(source_ids)


def test_distill_cursor_overlaps_cluster_boundary(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DISTILL_SCAN_LIMIT", 3)
    monkeypatch.setattr(consolidate_module, "DISTILL_CLUSTER_LIMIT", 3)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for index in range(9):
        eng.remember(
            f"Placeholder episodic note {index}.", workspace_id=wid, repo_id=rid,
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )
    flt = SearchFilter(workspace_id=wid, repo_id=rid)
    first_page = eng.store.list_memories_page(flt, after_id="", limit=3)
    second_page = eng.store.list_memories_page(
        flt, after_id=first_page[-1].id, limit=3,
    )
    third_page = eng.store.list_memories_page(
        flt, after_id=second_page[-1].id, limit=3,
    )
    assert len(first_page) == len(second_page) == len(third_page) == 3
    recurring = (
        "Recurring deploy failure during the integration test, run 1.",
        "Recurring deploy failure during the integration test, run 2.",
        "Recurring deploy failure during the integration test, run 3.",
    )
    replacements = {
        first_page[0].id: "Unrelated maintenance observation.",
        second_page[0].id: "Unrelated release note.",
        second_page[1].id: recurring[0],
        second_page[2].id: recurring[1],
        third_page[0].id: recurring[2],
        third_page[1].id: "Unrelated incident note.",
        third_page[2].id: "Unrelated audit note.",
    }
    for memory_id, content in replacements.items():
        eng.store.conn.execute(
            "UPDATE memories SET content=? WHERE id=?", (content, memory_id),
        )
    eng.store.conn.commit()

    first = consolidate(eng, workspace_id=wid, repo_id=rid, min_cluster=3)
    assert first["digests_created"] == []
    assert eng.store.get_maintenance_cursor(
        wid, rid, consolidate_module.DISTILL_CURSOR_NAME,
    )

    second = consolidate(eng, workspace_id=wid, repo_id=rid, min_cluster=3)
    assert len(second["digests_created"]) == 1
    assert set(second["digests_created"][0]["consolidates"]) == {
        second_page[1].id, second_page[2].id, third_page[0].id,
    }


def test_distill_cursor_carries_interleaved_partial_cluster(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DISTILL_SCAN_LIMIT", 3)
    monkeypatch.setattr(consolidate_module, "DISTILL_CLUSTER_LIMIT", 3)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    source_ids = [
        eng.remember(
            f"marker{index} value{index} signal{index}.", workspace_id=wid, repo_id=rid,
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )
        for index in range(9)
    ]
    # The maintenance cursor is a keyset cursor over sorted ULIDs.  Several writes can
    # share a millisecond, so insertion order is not a stable proxy for page order.
    ordered_ids = [
        memory.id for memory in eng.store.list_memories_page(
            SearchFilter(workspace_id=wid, repo_id=rid), limit=len(source_ids),
        )
    ]
    recurring = {
        ordered_ids[index]: f"Recurring deploy failure during run {index}."
        for index in (0, 3, 6)
    }
    for memory_id, content in recurring.items():
        eng.store.conn.execute(
            "UPDATE memories SET content=? WHERE id=?", (content, memory_id),
        )
    eng.store.conn.commit()

    first = consolidate(eng, workspace_id=wid, repo_id=rid, min_cluster=3)
    assert first["digests_created"] == []
    cursor = eng.store.get_maintenance_cursor(
        wid, rid, consolidate_module.DISTILL_CURSOR_NAME,
    )
    assert "pending" in cursor

    second = consolidate(eng, workspace_id=wid, repo_id=rid, min_cluster=3)

    assert len(second["digests_created"]) == 1
    assert set(second["digests_created"][0]["consolidates"]) == set(recurring)


def test_distill_cursor_drops_closed_partial_cluster_sources(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DISTILL_SCAN_LIMIT", 3)
    monkeypatch.setattr(consolidate_module, "DISTILL_CLUSTER_LIMIT", 3)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    source_ids = [
        eng.remember(
            f"marker{index} value{index} signal{index}.",
            workspace_id=wid, repo_id=rid,
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )
        for index in range(9)
    ]
    # The maintenance cursor is a keyset cursor over sorted ULIDs.  Several writes
    # can share a millisecond, so insertion order is not a stable proxy for page
    # order.  Use the actual keyset order to pick recurring targets deterministically.
    ordered_ids = [
        memory.id for memory in eng.store.list_memories_page(
            SearchFilter(workspace_id=wid, repo_id=rid), limit=len(source_ids),
        )
    ]
    recurring_indices = (0, 3, 6)
    for index in recurring_indices:
        eng.store.conn.execute(
            "UPDATE memories SET content=? WHERE id=?",
            (f"Recurring deploy failure during run {index}.", ordered_ids[index]),
        )
    eng.store.conn.commit()

    first = consolidate(eng, workspace_id=wid, repo_id=rid, min_cluster=3)
    assert first["digests_created"] == []
    eng.store.close_validity(ordered_ids[0], at=time.time())

    second = consolidate(eng, workspace_id=wid, repo_id=rid, min_cluster=3)

    assert second["digests_created"] == []


def test_scan_advances_past_a_fully_excluded_page():
    from engraphis.core import consolidate as consolidate_module

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    source_ids = [
        eng.remember(
            f"Recurring maintenance event {index}.", workspace_id=wid, repo_id=rid,
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )
        for index in range(4)
    ]
    flt = SearchFilter(
        workspace_id=wid, repo_id=rid, mtypes=[MemoryType.EPISODIC],
    )
    first_page = eng.store.list_memories_page(flt, limit=2)
    assert len(first_page) == 2
    derived_id = eng.remember(
        "Derived summary.", workspace_id=wid, repo_id=rid,
        mtype=MemoryType.SEMANTIC, resolve_conflicts=False,
    )
    for memory in first_page:
        eng.store.add_link(derived_id, memory.id, "consolidates")

    scanned = consolidate_module._scan_memories(
        eng.store, flt, mtypes=[MemoryType.EPISODIC], batch_size=2,
        exclude_relation="consolidates",
    )

    assert {memory.id for memory in scanned} == set(source_ids) - {
        memory.id for memory in first_page
    }


def test_scan_memories_streams_bounded_pages_lazily(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    memory_ids = {
        eng.remember(
            f"Streaming archive candidate {index}.",
            workspace_id=wid, mtype=MemoryType.WORKING,
            resolve_conflicts=False,
        )
        for index in range(5)
    }
    calls = []
    original_page = eng.store.list_memories_page

    def record_page(page_filter, *, after_id="", limit=500, include_invalid=False):
        calls.append((after_id, limit))
        return original_page(
            page_filter, after_id=after_id, limit=limit,
            include_invalid=include_invalid,
        )

    monkeypatch.setattr(eng.store, "list_memories_page", record_page)
    stream = consolidate_module._scan_memories(
        eng.store,
        SearchFilter(workspace_id=wid),
        mtypes=[MemoryType.WORKING],
        batch_size=2,
    )

    assert calls == []
    first = next(stream)
    assert len(calls) == 1
    scanned_ids = {first.id, *(memory.id for memory in stream)}
    assert scanned_ids == memory_ids
    assert len(calls) == 3

def test_scan_enforces_raw_advance_cap_when_rows_are_excluded(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for index in range(20):
        eng.remember(
            f"Excluded maintenance event {index}.",
            workspace_id=wid, repo_id=rid,
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )
    flt = SearchFilter(
        workspace_id=wid, repo_id=rid, mtypes=[MemoryType.EPISODIC],
    )
    calls = []
    original_page = eng.store.list_memories_page

    def record_page(page_filter, *, after_id="", limit=500, include_invalid=False):
        calls.append((after_id, limit))
        return original_page(
            page_filter, after_id=after_id, limit=limit,
            include_invalid=include_invalid,
        )

    monkeypatch.setattr(eng.store, "list_memories_page", record_page)
    scanned_ids = []

    def exclude_page(_store, memory_ids, *, relation):
        scanned_ids.extend(memory_ids)
        return set(memory_ids)

    monkeypatch.setattr(consolidate_module, "_linked_memory_ids", exclude_page)
    records, next_cursor = consolidate_module._scan_memory_window(
        eng.store, flt, mtypes=[MemoryType.EPISODIC], batch_size=2,
        max_records=5, exclude_relation="consolidates",
        overlap=2, advance_records=5,
    )

    assert records == []
    assert len(scanned_ids) == 5
    assert [limit for _after_id, limit in calls] == [2, 2, 1]
    assert next_cursor == scanned_ids[2]


def test_linked_memory_ids_respects_sqlite_bind_limit():
    from engraphis.core import consolidate as consolidate_module

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    derived_id = eng.remember(
        "Derived summary.", workspace_id=wid, resolve_conflicts=False,
    )
    source_ids = [
        eng.remember(
            f"Source {index}.", workspace_id=wid,
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        )
        for index in range(500)
    ]
    for source_id in source_ids:
        eng.store.add_link(derived_id, source_id, "consolidates")

    linked = consolidate_module._linked_memory_ids(
        eng.store, source_ids, relation="consolidates",
    )

    assert linked == set(source_ids)


def test_archive_batches_all_eligible_transients(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DISTILL_SCAN_LIMIT", 2)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    stale_ids = [
        eng.remember(
            f"Old scratch note {index}.", workspace_id=wid,
            mtype=MemoryType.WORKING, resolve_conflicts=False,
        )
        for index in range(5)
    ]
    old = time.time() - 86_400
    eng.store.conn.executemany(
        "UPDATE memories SET stability=0.01, last_access=?, valid_from=? WHERE id=?",
        [(old, old, memory_id) for memory_id in stale_ids],
    )
    eng.store.conn.executemany(
        "UPDATE mem_vectors SET vector=zeroblob(?) WHERE id=?",
        [(256 * 1024, memory_id) for memory_id in stale_ids],
    )
    vector_bytes = eng.store.conn.execute(
        "SELECT SUM(length(vector)) FROM mem_vectors WHERE id IN (?,?,?,?,?)",
        stale_ids,
    ).fetchone()[0]
    eng.store.conn.commit()

    report = consolidate(eng, workspace_id=wid, now=time.time())

    assert {row["id"] for row in report["archived"]} == set(stale_ids)
    assert report["errors"] == []
    assert eng.store.conn.execute(
        "SELECT SUM(length(vector)) FROM mem_vectors WHERE id IN (?,?,?,?,?)",
        stale_ids,
    ).fetchone()[0] == vector_bytes


def test_digest_retry_completes_an_interrupted_link_set(monkeypatch):
    eng, wid, rid = _engine_with_repeats()
    original_add_link = eng.store.add_link
    state = {"calls": 0}

    def fail_once(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            raise RuntimeError("link store unavailable")
        return original_add_link(*args, **kwargs)

    monkeypatch.setattr(eng.store, "add_link", fail_once)
    first = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert len(first["digests_created"]) == 0
    assert len(first["errors"]) == 1

    second = consolidate(eng, workspace_id=wid, repo_id=rid)

    assert second["errors"] == []
    assert len(second["digests_created"]) == 0
    semantic = eng.store.list_memories(
        SearchFilter(workspace_id=wid, repo_id=rid, mtypes=[MemoryType.SEMANTIC])
    )
    digest = [memory for memory in semantic
              if memory.metadata.get("provenance", {}).get("source") == "consolidation"]
    assert len(digest) == 1
    assert sum(link["relation"] == "consolidates" for link in eng.store.get_links(digest[0].id)) == 3



def test_digest_resume_reapplies_source_safety_after_partial_write(monkeypatch):
    """A retry must repair safety metadata on a derived row committed before failure."""
    from engraphis.core import consolidate as consolidate_module

    eng, wid, rid = _engine_with_repeats()
    source = next(
        memory for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid, repo_id=rid, mtypes=[MemoryType.EPISODIC]),
            limit=10,
        )
        if "flaky network" in memory.content
    )
    eng.store.conn.execute(
        "UPDATE memories SET sensitivity='secret' WHERE id=?", (source.id,)
    )
    eng.store.conn.commit()

    original_inherit = consolidate_module._inherit_safety
    state = {"calls": 0}

    def fail_once(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("safety patch interrupted")
        return original_inherit(*args, **kwargs)

    monkeypatch.setattr(consolidate_module, "_inherit_safety", fail_once)
    first = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert first["digests_created"] == []
    assert len(first["errors"]) == 1

    second = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert second["errors"] == []
    assert second["digests_created"] == []
    assert second["skipped_already_consolidated"] == 1
    semantic = eng.store.list_memories(
        SearchFilter(workspace_id=wid, repo_id=rid, mtypes=[MemoryType.SEMANTIC]),
        limit=20,
    )
    digests = [
        memory for memory in semantic
        if memory.metadata.get("provenance", {}).get("source") == "consolidation"
    ]
    assert len(digests) == 1
    digest = digests[0]
    assert digest.sensitivity == "secret"
    assert digest.provenance["trusted"] is True
    assert sum(
        link["relation"] == "consolidates"
        for link in eng.store.get_links(digest.id)
    ) == 3
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE actor='consolidation' AND action='distill'"
    ).fetchone()[0] == 1
    consolidate(eng, workspace_id=wid, repo_id=rid)
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE actor='consolidation' AND action='distill'"
    ).fetchone()[0] == 1


def test_completed_digest_safety_is_repaired_after_source_tightening():
    eng, wid, rid = _engine_with_repeats()
    first = consolidate(eng, workspace_id=wid, repo_id=rid)
    digest_id = first["digests_created"][0]["id"]
    source_id = first["digests_created"][0]["consolidates"][0]
    eng.store.conn.execute(
        "UPDATE memories SET sensitivity='secret' WHERE id=?", (source_id,)
    )
    eng.store.conn.commit()

    second = consolidate(eng, workspace_id=wid, repo_id=rid)

    assert second["errors"] == []
    assert eng.store.get_memory(digest_id).sensitivity == "secret"


def test_safety_repair_cursor_eventually_reaches_rows_beyond_limit(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DERIVED_MAINTENANCE_LIMIT", 2)
    eng = MemoryEngine.create(":memory:")
    workspace_id = eng.store.get_or_create_workspace("safety-cursor")
    repo_id = eng.store.get_or_create_repo(workspace_id, "repo")
    source_id = eng.remember(
        "Authoritative source.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        mtype=MemoryType.EPISODIC,
        resolve_conflicts=False,
    )
    for index in range(5):
        eng.remember(
            f"Older semantic noise {index}.",
            workspace_id=workspace_id,
            repo_id=repo_id,
            mtype=MemoryType.SEMANTIC,
            resolve_conflicts=False,
        )
    # ULIDs sort by creation millisecond; rows created within the same millisecond
    # order randomly inside it. Force the derived row to sort strictly after every
    # noise row so the paging limit genuinely defers it to a later repair sweep.
    time.sleep(0.005)
    derived_id = eng.remember(
        "Derived summary.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        mtype=MemoryType.SEMANTIC,
        metadata={"provenance": {
            "source": "consolidation",
            "trusted": True,
            "source_ids": [source_id],
            "consolidates": [source_id],
        }},
        resolve_conflicts=False,
    )
    eng.store.add_link(derived_id, source_id, "consolidates")
    eng.store.advance_memory_modified_hlc(source_id, commit=False)
    eng.store.conn.execute(
        "UPDATE memories SET sensitivity='secret' WHERE id=?",
        (source_id,),
    )
    eng.store.conn.commit()
    flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)

    sweeps = 0
    while sweeps < 4 and eng.store.get_memory(derived_id).sensitivity != "secret":
        errors = consolidate_module._repair_derived_safety(
            eng,
            flt,
            provenance_source="consolidation",
            relation="consolidates",
        )
        assert errors == []
        sweeps += 1

    assert sweeps > 1
    assert eng.store.get_memory(derived_id).sensitivity == "secret"


def test_completed_profile_safety_is_repaired_after_source_tightening():
    from engraphis.core.consolidate import consolidate_profiles

    eng, wid, rid, _name = _engine_with_entity_mentions()
    first = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)
    profile_id = first["profiles_created"][0]["id"]
    source_id = next(
        link["b"] if link["a"] == profile_id else link["a"]
        for link in eng.store.get_links(profile_id)
        if link["relation"] == "profiles"
    )
    eng.store.conn.execute(
        "UPDATE memories SET sensitivity='secret' WHERE id=?", (source_id,)
    )
    eng.store.conn.commit()

    second = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)

    assert second["errors"] == []
    assert eng.store.get_memory(profile_id).sensitivity == "secret"

def test_profile_resume_reapplies_source_safety_after_partial_write(monkeypatch):
    from engraphis.core import consolidate as consolidate_module
    from engraphis.core.consolidate import consolidate_profiles

    eng, wid, rid, _name = _engine_with_entity_mentions()
    source = eng.store.list_memories(
        SearchFilter(workspace_id=wid, repo_id=rid, mtypes=[MemoryType.SEMANTIC]),
        limit=20,
    )[0]
    eng.store.conn.execute(
        "UPDATE memories SET sensitivity='secret' WHERE id=?", (source.id,)
    )
    eng.store.conn.commit()

    original_inherit = consolidate_module._inherit_safety
    state = {"calls": 0}

    def fail_once(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("safety patch interrupted")
        return original_inherit(*args, **kwargs)

    monkeypatch.setattr(consolidate_module, "_inherit_safety", fail_once)
    first = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)
    assert first["profiles_created"] == []
    assert len(first["errors"]) == 1

    second = consolidate_profiles(eng, workspace_id=wid, repo_id=rid)
    assert second["errors"] == []
    assert second["profiles_created"] == []
    assert second["skipped_existing"] == 1
    semantic = eng.store.list_memories(
        SearchFilter(workspace_id=wid, repo_id=rid, mtypes=[MemoryType.SEMANTIC]),
        limit=30,
    )
    profiles = [
        memory for memory in semantic
        if memory.metadata.get("provenance", {}).get("source")
        == "profile_consolidation"
    ]
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.sensitivity == "secret"
    assert profile.provenance["trusted"] is True
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE actor='consolidation' AND action='profile'"
    ).fetchone()[0] == 1
    consolidate_profiles(eng, workspace_id=wid, repo_id=rid)
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE actor='consolidation' AND action='profile'"
    ).fetchone()[0] == 1
    assert sum(
        link["relation"] == "profiles"
        for link in eng.store.get_links(profile.id)
    ) == 8

def test_structured_resume_repairs_each_partial_fact_once(monkeypatch):
    pytest.importorskip("pydantic")
    from engraphis.core import consolidate as consolidate_module

    eng, wid, rid = _engine_with_auth_repeats()
    source_ids = [
        memory.id for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid, repo_id=rid, mtypes=[MemoryType.EPISODIC]),
            limit=10,
        )
    ]
    facts = [
        {
            "content": "The first structured fact.",
            "title": "First fact",
            "confidence": 0.8,
            "importance": 0.5,
            "keywords": ["first"],
            "entities": [],
            "relations": [],
            "source_ids": [source_ids[0]],
        },
        {
            "content": "The second structured fact.",
            "title": "Second fact",
            "confidence": 0.7,
            "importance": 0.5,
            "keywords": ["second"],
            "entities": [],
            "relations": [],
            "source_ids": source_ids[1:],
        },
    ]

    def fake_facts(_cluster, *, llm, subject_hint):
        return facts

    monkeypatch.setattr(consolidate_module, "_structured_cluster_facts", fake_facts)
    original_add_link = eng.store.add_link
    state = {"calls": 0}

    def fail_once(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            raise RuntimeError("link store unavailable")
        return original_add_link(*args, **kwargs)

    monkeypatch.setattr(eng.store, "add_link", fail_once)
    first = consolidate(
        eng, workspace_id=wid, repo_id=rid, structured=True, llm=object(),
    )
    assert first["digests_created"] == []
    assert len(first["errors"]) == 1

    second = consolidate(
        eng, workspace_id=wid, repo_id=rid, structured=True, llm=object(),
    )
    assert second["errors"] == []
    assert second["digests_created"] == []
    semantic = eng.store.list_memories(
        SearchFilter(workspace_id=wid, repo_id=rid, mtypes=[MemoryType.SEMANTIC]),
        limit=20,
    )
    facts_written = [
        memory for memory in semantic
        if memory.metadata.get("provenance", {}).get("source")
        == "structured_consolidation"
    ]
    assert len(facts_written) == 2
    assert sorted(
        sum(link["relation"] == "consolidates"
            for link in eng.store.get_links(memory.id))
        for memory in facts_written
    ) == [1, 2]
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM audit "
        "WHERE actor='consolidation' AND action='distill_structured'"
    ).fetchone()[0] == 2
    consolidate(
        eng, workspace_id=wid, repo_id=rid, structured=True, llm=object(),
    )
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM audit "
        "WHERE actor='consolidation' AND action='distill_structured'"
    ).fetchone()[0] == 2

def test_derived_digest_uses_sweep_timestamp():
    eng, wid, rid = _engine_with_repeats()
    sweep_time = time.time() - 10

    report = consolidate(eng, workspace_id=wid, repo_id=rid, now=sweep_time)

    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert digest.valid_from == sweep_time


def test_archive_pass_sees_transients_behind_newer_semantic_rows(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DISTILL_SCAN_LIMIT", 3)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    stale = eng.remember("Scratch note from an old session.", workspace_id=wid,
                         mtype=MemoryType.WORKING, resolve_conflicts=False)
    # Back-date creation so the default-now sweep cannot tie ingest and defer
    # the archive this test asserts.
    eng.store.conn.execute(
        "UPDATE memories SET stability=0.01, last_access=?, valid_from=? WHERE id=?",
        (time.time() - 86_400, time.time() - 86_400, stale))
    eng.store.conn.commit()
    for n in range(5):
        eng.remember(f"Durable architecture note {n}.", workspace_id=wid,
                     mtype=MemoryType.SEMANTIC, resolve_conflicts=False)

    report = consolidate(eng, workspace_id=wid)

    assert [row["id"] for row in report["archived"]] == [stale]


def test_archive_preserves_vector_for_historical_recall():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    stale = eng.remember(
        "Scratch note from an old session.",
        workspace_id=wid,
        mtype=MemoryType.WORKING,
        resolve_conflicts=False,
    )
    # Windows wall-clock resolution (~15.6 ms) can tie the memory's creation stamp to
    # ``archived_at``, collapsing [valid_from, archived_at) to a zero-width interval that
    # the half-open temporal predicate hides at every as_of. Back-date creation so this
    # test asserts archival semantics, not host clock granularity.
    created_at = time.time() - 3_600
    eng.store.conn.execute(
        "UPDATE memories SET stability=0.01, last_access=?, valid_from=? WHERE id=?",
        (time.time() - 86_400, created_at, stale),
    )
    eng.store.conn.commit()

    # valid_from defaults to creation time; a same-tick archived_at would make the
    # historical as_of midpoint land exactly on valid_to and the half-open temporal
    # predicate would exclude the row. Nudge the archive stamp well into the future
    # so the midpoint sits strictly inside [valid_from, valid_to) even on coarse clocks.
    archived_at = time.time() + 3_600
    report = consolidate(eng, workspace_id=wid, now=archived_at)

    assert [row["id"] for row in report["archived"]] == [stale]
    assert eng.store.conn.execute(
        "SELECT 1 FROM mem_vectors WHERE id=?", (stale,)
    ).fetchone() is not None
    valid_from = eng.store.get_memory(stale).valid_from
    historical = eng.recall_engine.recall(
        "What scratch note came from the old session?",
        SearchFilter(workspace_id=wid, as_of=(valid_from + archived_at) / 2),
        reinforce=False,
    )
    assert [chunk["id"] for chunk in historical.chunks] == [stale]



def test_archive_tied_to_ingest_stays_historically_visible():
    """Consolidation one clock tick after ingest must not erase the fact.

    Coarse host clocks can hand ``consolidate`` a ``now`` equal to the
    memory's ``valid_from``. Closing there would be invisible to every read
    (zero-width [t, t)), and fabricating window width would briefly resurrect
    the row into the live view — so the sweep defers the archive instead. A
    strictly later sweep closes it with ordinary half-open semantics: hidden
    from current reads immediately, still reproducible by an as_of query.
    """
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    stale = eng.remember(
        "Fleeting note captured moments before the sweep.",
        workspace_id=wid,
        mtype=MemoryType.WORKING,
        resolve_conflicts=False,
    )
    tied_at = time.time()
    eng.store.conn.execute(
        "UPDATE memories SET stability=0.01, last_access=?, valid_from=? WHERE id=?",
        (tied_at - 86_400, tied_at, stale),
    )
    eng.store.conn.commit()

    deferred = consolidate(eng, workspace_id=wid, now=tied_at)

    # The tied sweep neither archives nor closes anything: the memory stays
    # live, honestly, because this clock cannot yet separate ingest from now.
    assert [row["id"] for row in deferred["archived"]] == []
    assert deferred.get("archive_deferred") == 1
    assert eng.store.get_memory(stale).valid_to is None

    later = tied_at + 1.0
    report = consolidate(eng, workspace_id=wid, now=later)

    assert [row["id"] for row in report["archived"]] == [stale]
    archived = eng.store.get_memory(stale)
    assert archived.valid_to is not None and archived.valid_to > archived.valid_from
    historical = eng.recall_engine.recall(
        "What fleeting note was captured before the sweep?",
        SearchFilter(workspace_id=wid, as_of=(archived.valid_from + later) / 2),
        reinforce=False,
    )
    assert [chunk["id"] for chunk in historical.chunks] == [stale]

# ── explicit local consolidation command ─────────────────────────────────────

from scripts.consolidate import main as consolidate_main  # noqa: E402


def test_cli_closes_owned_service_exactly_once_on_success_and_failure(monkeypatch):
    module = importlib.import_module("scripts.consolidate")

    class TrackingService:
        engine = object()

        def __init__(self):
            self.close_count = 0

        def close(self):
            self.close_count += 1

    success = TrackingService()
    monkeypatch.setattr(module, "_service", lambda _db: success)
    monkeypatch.setattr(module, "_consolidate", lambda _args, _engine: 0)
    assert module.main(["--db", "unused.db", "--workspace", "w"]) == 0
    assert success.close_count == 1

    failure = TrackingService()

    def fail(_args, _engine):
        raise RuntimeError("consolidation failed")

    monkeypatch.setattr(module, "_service", lambda _db: failure)
    monkeypatch.setattr(module, "_consolidate", fail)
    with pytest.raises(RuntimeError, match="consolidation failed"):
        module.main(["--db", "unused.db", "--workspace", "w"])
    assert failure.close_count == 1


def _seed_db(tmp_path):
    db = tmp_path / "mem.db"
    eng = MemoryEngine.create(str(db))
    try:
        wid = eng.store.get_or_create_workspace("w")
        rid = eng.store.get_or_create_repo(wid, "r")
        for i in range(3):
            eng.remember(f"Build failed on the flaky network test in CI run {i}.",
                         workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                         resolve_conflicts=False)
    finally:
        eng.store.close()
    return db


def test_removed_supersede_sources_cli_flag_is_rejected(tmp_path, capsys):
    db = _seed_db(tmp_path)
    with pytest.raises(SystemExit) as exc:
        consolidate_main([
            "--db", str(db), "--workspace", "w", "--supersede-sources",
        ])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_invalid_cli_threshold_exits_two_without_mutating_memories(tmp_path, capsys):
    db = _seed_db(tmp_path)
    with sqlite3.connect(db) as conn:
        before = conn.execute(
            "SELECT id, valid_to, valid_to_recorded_at FROM memories ORDER BY id"
        ).fetchall()

    assert consolidate_main([
        "--db", str(db), "--workspace", "w", "--archive-below", "nan",
    ]) == 2

    assert "archive_below must be between" in capsys.readouterr().err
    with sqlite3.connect(db) as conn:
        after = conn.execute(
            "SELECT id, valid_to, valid_to_recorded_at FROM memories ORDER BY id"
        ).fetchall()
    assert after == before


def test_removed_supersede_sources_apis_reject_the_kwarg():
    eng = MemoryEngine.create(":memory:")
    workspace_id = eng.store.get_or_create_workspace("w")
    service = MemoryService(eng)

    with pytest.raises(TypeError, match="supersede_sources"):
        consolidate(
            eng,
            workspace_id=workspace_id,
            supersede_sources=True,
        )
    with pytest.raises(TypeError, match="supersede_sources"):
        eng.consolidate(
            workspace_id=workspace_id,
            supersede_sources=True,
        )
    with pytest.raises(TypeError, match="supersede_sources"):
        service.consolidate(
            workspace="w",
            supersede_sources=True,
        )


def test_removed_unimplemented_consolidation_level_core_apis_reject_the_kwarg():
    eng = MemoryEngine.create(":memory:")
    workspace_id = eng.store.get_or_create_workspace("w")

    with pytest.raises(TypeError, match="consolidation_level"):
        consolidate(
            eng,
            workspace_id=workspace_id,
            consolidation_level="hierarchical",
        )
    with pytest.raises(TypeError, match="consolidation_level"):
        eng.consolidate(
            workspace_id=workspace_id,
            consolidation_level="hierarchical",
        )


def test_explicit_sweep_needs_no_license(tmp_path):
    db = _seed_db(tmp_path)
    assert consolidate_main(["--db", str(db), "--workspace", "w"]) == 0



def test_prose_llm_prompts_are_bounded_and_record_exact_selected_sources(monkeypatch):
    from engraphis.core import consolidate as consolidate_module
    from engraphis.core.consolidate import consolidate_profiles

    prompts = []

    class CapturingLLM:
        def chat(self, messages, system=None):
            prompts.append(messages[0]["content"])
            return "Bounded summary."

    monkeypatch.setattr(consolidate_module, "PROSE_MAX_SOURCE_ITEMS", 3)
    monkeypatch.setattr(consolidate_module, "PROSE_MAX_SOURCE_CHARS", 500)
    monkeypatch.setattr(consolidate_module, "PROSE_MAX_ITEM_CHARS", 80)

    eng, wid, rid = _engine_with_large_cluster(n=8)
    report = consolidate(
        eng, workspace_id=wid, repo_id=rid, llm=CapturingLLM(),
    )
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    digest_prompt = digest.metadata["llm_consolidation"]
    digest_sources = digest.provenance["consolidates"]
    assert len(prompts[0]) <= 500
    assert digest_prompt["prompt_chars"] == len(prompts[0])
    assert digest_prompt["prompt_source_ids"] == digest_sources[:3]
    assert digest_prompt["prompt_source_count"] == 3
    assert digest_prompt["prompt_omitted_count"] == len(digest_sources) - 3

    profile_engine, profile_wid, profile_rid, _ = _engine_with_entity_mentions(n=8)
    profile_report = consolidate_profiles(
        profile_engine,
        workspace_id=profile_wid,
        repo_id=profile_rid,
        llm=CapturingLLM(),
    )
    profile = profile_engine.store.get_memory(
        profile_report["profiles_created"][0]["id"]
    )
    profile_prompt = profile.metadata["llm_consolidation"]
    profile_sources = profile.provenance["profiles"]
    assert len(prompts[1]) <= 500
    assert profile_prompt["prompt_chars"] == len(prompts[1])
    assert profile_prompt["prompt_source_ids"] == profile_sources[:3]
    assert profile_prompt["prompt_source_count"] == 3
    assert profile_prompt["prompt_omitted_count"] == len(profile_sources) - 3


def test_profile_entity_cursor_eventually_reaches_entities_beyond_limit(monkeypatch):
    from engraphis.core import consolidate as consolidate_module
    from engraphis.core.consolidate import consolidate_profiles
    from engraphis.core.interfaces import Node

    monkeypatch.setattr(consolidate_module, "PROFILE_ENTITY_LIMIT", 2)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for index in range(5):
        eng.store.upsert_entity(Node(
            id="", name=f"Noise {index}", ntype="topic",
            workspace_id=wid, repo_id=rid,
        ))
    for index in range(3):
        eng.remember(
            f"Zeta owns durable workflow {index}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.SEMANTIC,
            resolve_conflicts=False,
        )
    target_id = eng.store.upsert_entity(Node(
        id="", name="Zeta", ntype="person", workspace_id=wid, repo_id=rid,
    ))

    created = []
    for _ in range(4):
        report = consolidate_profiles(
            eng, workspace_id=wid, repo_id=rid, min_mentions=3,
        )
        created.extend(report["profiles_created"])
        if created:
            break

    assert any(entry["entity"] == "Zeta" for entry in created)
    for _ in range(3):
        consolidate_profiles(
            eng, workspace_id=wid, repo_id=rid, min_mentions=3,
        )

    profiles = eng.store.list_memories(
        SearchFilter(
            workspace_id=wid, repo_id=rid, mtypes=[MemoryType.SEMANTIC],
        ),
        include_invalid=True,
    )
    zeta_profiles = [
        memory
        for memory in profiles
        if memory.title == "Profile: Zeta"
        and target_id in {
            link["entity_id"]
            for link in eng.store.list_memory_entities(
                SearchFilter(workspace_id=wid, repo_id=rid),
                memory_ids=[memory.id],
            )
        }
    ]
    assert len(zeta_profiles) == 1


def test_structured_recovery_cursor_reaches_newer_incomplete_derived_row(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DERIVED_MAINTENANCE_LIMIT", 2)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for index in range(5):
        eng.remember(
            f"Older semantic noise {index}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.SEMANTIC,
            resolve_conflicts=False,
        )
    source_ids = [
        eng.remember(
            f"Recovery source {index}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
            resolve_conflicts=False,
        )
        for index in range(2)
    ]
    derived_id = eng.remember(
        "Partially linked structured fact.",
        workspace_id=wid,
        repo_id=rid,
        mtype=MemoryType.SEMANTIC,
        metadata={
            "provenance": {
                "source": "structured_consolidation",
                "trusted": False,
                "review_state": "pending",
                "source_ids": source_ids,
                "consolidates": source_ids,
            },
            "structured_consolidation": {"confidence": 0.8, "llm": {}},
        },
        resolve_conflicts=False,
    )
    eng.store.add_link(derived_id, source_ids[0], "consolidates")

    for _ in range(4):
        consolidate(
            eng, workspace_id=wid, repo_id=rid,
            structured=True, llm=object(),
        )
        links = [
            link for link in eng.store.get_links(derived_id)
            if link["relation"] == "consolidates"
        ]
        if len(links) == 2:
            break

    assert {
        link["b"] if link["a"] == derived_id else link["a"]
        for link in eng.store.get_links(derived_id)
        if link["relation"] == "consolidates"
    } == set(source_ids)
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM audit "
        "WHERE actor='consolidation' AND action='distill_structured' AND target=?",
        (derived_id,),
    ).fetchone()[0] == 1


def test_safety_rewrite_clock_is_monotonic_and_rolls_back_with_commit(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    eng = MemoryEngine.create(":memory:")
    workspace_id = eng.store.get_or_create_workspace("clock-safety")
    source_id = eng.remember(
        "Source memory.",
        workspace_id=workspace_id,
        resolve_conflicts=False,
    )
    derived_id = eng.remember(
        "Derived memory.",
        workspace_id=workspace_id,
        mtype=MemoryType.SEMANTIC,
        resolve_conflicts=False,
    )
    source = eng.store.get_memory(source_id)
    before = eng.store.get_memory(derived_id)

    consolidate_module._inherit_safety(eng, derived_id, [source])

    advanced = eng.store.get_memory(derived_id)
    assert advanced.modified_hlc > before.modified_hlc

    def fail_commit(_connection):
        raise RuntimeError("fail safety commit")

    with monkeypatch.context() as patch:
        patch.setattr(type(eng.store.conn), "commit", fail_commit)
        with pytest.raises(RuntimeError, match="fail safety commit"):
            consolidate_module._inherit_safety(eng, derived_id, [source])

    rolled_back = eng.store.get_memory(derived_id)
    assert rolled_back.modified_hlc == advanced.modified_hlc
    assert rolled_back.metadata == advanced.metadata
    assert rolled_back.provenance == advanced.provenance


# ── consolidation audit: working→semantic promotion, decay safety, scope, dry-run ──

def test_working_memories_are_never_promoted_to_semantic_by_consolidation():
    """Consolidation distills EPISODIC→SEMANTIC only. WORKING memories are transient
    (archivable) but must never be clustered into a semantic digest — that would be an
    unintended promotion path outside the explicit promote() API."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    # Create 5 working memories with identical content — enough to form a cluster
    # if the type filter were wrong.
    for i in range(5):
        eng.remember(
            f"Working task state for batch job {i}",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.WORKING,
            resolve_conflicts=False,
        )
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    # No digests should be created from working memories.
    assert report["digests_created"] == []
    assert report["clusters_found"] == 0
    # All working memories remain live (not archived at default threshold).
    working = [
        m for m in eng.store.list_memories(
            SearchFilter(workspace_id=wid, repo_id=rid),
        ) if m.mtype == MemoryType.WORKING
    ]
    assert len(working) == 5
    assert all(m.valid_to is None for m in working)


def test_recently_accessed_episodic_is_not_prematurely_archived():
    """Episodic decay uses retention(stability, last_access, now). A recently accessed
    memory must not be archived even if ingested long ago — the access resets the
    effective age. This guards against premature deletion of active memories."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    # Ingest an episodic memory 60 days ago with default stability (1 day).
    ancient = time.time() - 60 * 86400
    mid = eng.remember(
        "Important recurring pattern observed in production",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
        resolve_conflicts=False,
    )
    # Backdate ingestion to 60 days ago.
    eng.store.conn.execute(
        "UPDATE memories SET ingested_at=?, last_access=? WHERE id=?",
        (ancient, ancient, mid),
    )
    eng.store.conn.commit()
    # Without recent access, retention would be exp(-60/1) ≈ 0 → archived.
    # Now simulate a recent access (1 hour ago).
    recent = time.time() - 3600
    eng.store.conn.execute(
        "UPDATE memories SET last_access=? WHERE id=?", (recent, mid),
    )
    eng.store.conn.commit()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, archive_below=0.05)
    # The memory should NOT be archived because last_access is recent.
    assert report["archived"] == []
    mem = eng.store.get_memory(mid)
    assert mem.valid_to is None


def test_stale_unaccessed_episodic_is_archived_at_default_threshold():
    """An old, unaccessed episodic memory with low stability must be archived when
    its retention drops below the threshold. This confirms decay works correctly
    for genuinely forgotten memories."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    ancient = time.time() - 60 * 86400
    mid = eng.remember(
        "Transient debug observation from old session",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
        resolve_conflicts=False,
    )
    # Back-date creation too: a same-tick sweep would defer the archive
    # instead of closing it, and this test asserts the closed path.
    eng.store.conn.execute(
        "UPDATE memories SET ingested_at=?, last_access=?, valid_from=? WHERE id=?",
        (ancient, ancient, ancient, mid),
    )
    eng.store.conn.commit()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, archive_below=0.05)
    assert len(report["archived"]) == 1
    assert report["archived"][0]["id"] == mid
    mem = eng.store.get_memory(mid)
    assert mem.valid_to is not None


def test_consolidation_respects_scope_boundaries_no_session_leak():
    """Session-scoped memories must never appear in a workspace/repo consolidation
    sweep. MAINTENANCE_SCOPES excludes SESSION; verify this prevents both distillation
    and archival of session-private state."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    sid = eng.store.start_session(wid, rid)
    # Create session-scoped episodic memories that would form a cluster.
    for i in range(5):
        eng.remember(
            f"Session private note about task {i}",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
            scope=Scope.SESSION, session_id=sid,
            resolve_conflicts=False,
        )
    # Also create stale session working memories eligible for archival.
    ancient = time.time() - 60 * 86400
    for i in range(3):
        mid = eng.remember(
            f"Session temp state {i}",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.WORKING,
            scope=Scope.SESSION, session_id=sid,
            resolve_conflicts=False,
        )
        eng.store.conn.execute(
            "UPDATE memories SET ingested_at=?, last_access=? WHERE id=?",
            (ancient, ancient, mid),
        )
    eng.store.conn.commit()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, archive_below=0.05)
    # No digests or archives from session memories.
    assert report["digests_created"] == []
    assert report["archived"] == []
    assert report["clusters_found"] == 0


def test_dry_run_produces_zero_database_writes():
    """dry_run=True must not modify any database state: no new memories, no links,
    no validity changes, no cursor advances. The report describes what *would* happen."""
    eng, wid, rid = _engine_with_repeats()
    # Snapshot pre-state.
    before_memories = eng.store.conn.execute(
        "SELECT COUNT(*) FROM memories"
    ).fetchone()[0]
    before_links = eng.store.conn.execute(
        "SELECT COUNT(*) FROM mem_links"
    ).fetchone()[0]
    before_changes = eng.store.conn.total_changes
    report = consolidate(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    # Report shows what would happen.
    assert report["dry_run"] is True
    assert report["digests_created"]
    assert "would_consolidate" in report["digests_created"][0]
    # Zero mutations.
    after_memories = eng.store.conn.execute(
        "SELECT COUNT(*) FROM memories"
    ).fetchone()[0]
    after_links = eng.store.conn.execute(
        "SELECT COUNT(*) FROM mem_links"
    ).fetchone()[0]
    assert after_memories == before_memories
    assert after_links == before_links
    assert eng.store.conn.total_changes == before_changes


def test_dry_run_does_not_advance_maintenance_cursors():
    """A dry-run sweep must leave maintenance cursors unchanged so the next real
    sweep sees the same window."""
    eng, wid, rid = _engine_with_repeats()
    from engraphis.core.consolidate import DISTILL_CURSOR_NAME
    before_cursor = eng.store.get_maintenance_cursor(wid, rid, DISTILL_CURSOR_NAME)
    consolidate(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    after_cursor = eng.store.get_maintenance_cursor(wid, rid, DISTILL_CURSOR_NAME)
    assert after_cursor == before_cursor


def test_profiles_dry_run_produces_zero_database_writes():
    """Profile consolidation dry_run must also be fully read-only."""
    from engraphis.core.consolidate import consolidate_profiles
    from engraphis.core.interfaces import Node
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.store.upsert_entity(Node(
        id="", name="Aurora", ntype="project", workspace_id=wid, repo_id=rid,
    ))
    for i in range(5):
        eng.remember(
            f"Aurora milestone {i} completed",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
            resolve_conflicts=False,
        )
    before_memories = eng.store.conn.execute(
        "SELECT COUNT(*) FROM memories"
    ).fetchone()[0]
    before_links = eng.store.conn.execute(
        "SELECT COUNT(*) FROM mem_links"
    ).fetchone()[0]
    before_entities = eng.store.conn.execute(
        "SELECT COUNT(*) FROM memory_entities"
    ).fetchone()[0]
    report = consolidate_profiles(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    assert report["dry_run"] is True
    assert report["profiles_created"]
    assert "would_profile" in report["profiles_created"][0]
    after_memories = eng.store.conn.execute(
        "SELECT COUNT(*) FROM memories"
    ).fetchone()[0]
    after_links = eng.store.conn.execute(
        "SELECT COUNT(*) FROM mem_links"
    ).fetchone()[0]
    after_entities = eng.store.conn.execute(
        "SELECT COUNT(*) FROM memory_entities"
    ).fetchone()[0]
    assert after_memories == before_memories
    assert after_links == before_links
    assert after_entities == before_entities