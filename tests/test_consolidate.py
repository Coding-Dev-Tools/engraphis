import re
import time

import pytest

from engraphis.core.consolidate import _cluster_by_subject, consolidate
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, MemoryType, SearchFilter
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
    # Age both far past any plausible retention: tiny stability, ancient last_access.
    old = time.time() - 90 * 86400
    for mid in (stale, pinned):
        eng.store.conn.execute(
            "UPDATE memories SET stability=0.5, last_access=? WHERE id=?", (old, mid))
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


def test_consolidate_llm_failure_falls_back_to_deterministic():
    class BrokenLLM:
        def chat(self, messages, system=None, **kw):
            raise RuntimeError("provider down")

    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, llm=BrokenLLM())
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert "Recurring pattern" in digest.content


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


def test_structured_consolidation_writes_typed_fact_graph_and_can_supersede_sources():
    pytest.importorskip("pydantic")
    eng, wid, rid = _engine_with_auth_repeats()
    llm = _StructuredConsolidationLLM()
    # Called as the module function on purpose: the entities/relations below survive only
    # because _write_structured_digests vouches for them explicitly (_trusted_graph_keys).
    # If that vouch is ever dropped, the engine demotes them as caller-supplied metadata
    # and the graph assertions below fail — see core/engine.py::_rehome_untrusted_graph_hints.
    report = consolidate(eng, workspace_id=wid, repo_id=rid, structured=True,
                         supersede_sources=True, llm=llm)

    assert report["structured"]["attempted"] == 1
    assert report["structured"]["succeeded"] == 1
    entry = report["digests_created"][0]
    assert entry["structured"] is True
    digest = eng.store.get_memory(entry["id"])
    assert digest.mtype == MemoryType.SEMANTIC
    assert digest.metadata["provenance"]["source"] == "structured_consolidation"
    assert digest.metadata["structured_consolidation"]["confidence"] == 0.91
    assert digest.metadata["entities"] == ["Acme API", "PASETO", "JWT"]
    assert digest.metadata["relations"][0]["relation"] == "uses"
    assert "source_ids" in digest.metadata["provenance"]
    llm_audit = digest.metadata["structured_consolidation"]["llm"]
    assert len(llm_audit["prompt_sha256"]) == 64
    assert len(llm_audit["response_sha256"]) == 64

    # Structured metadata feeds graph nodes/edges even without the regex graph extractor.
    ents = {e.name: e.id for e in eng.store.list_entities(
        SearchFilter(workspace_id=wid, repo_id=rid))}
    assert {"Acme API", "PASETO", "JWT"} <= set(ents)
    edges = eng.store.edges_in_scope(SearchFilter(workspace_id=wid, repo_id=rid))
    assert any(e.src == ents["Acme API"] and e.dst == ents["PASETO"]
               and e.relation == "uses" for e in edges)

    # Supersession is explicit and opt-in: source episodes leave live recall but remain
    # inspectable in history.
    live_ids = {m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid), limit=20)}
    for source_id in entry["superseded_sources"]:
        assert source_id not in live_ids
        assert eng.store.get_memory(source_id).valid_to is not None
    episodes = [
        memory for memory in eng.store.list_memories(
            SearchFilter(workspace_id=wid), include_invalid=True, limit=20)
        if memory.mtype == MemoryType.EPISODIC
    ]
    assert len(entry["superseded_sources"]) == 2
    assert sum(memory.valid_to is None for memory in episodes) == 1


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

    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert digest.provenance["trusted"] is False
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


def test_supersede_sources_requires_structured_mode():
    eng, wid, rid = _engine_with_auth_repeats()
    with pytest.raises(ValueError, match="requires structured"):
        consolidate(eng, workspace_id=wid, repo_id=rid, supersede_sources=True)


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
    old = time.time() - 90 * 86400
    eng.store.conn.execute("UPDATE memories SET stability=0.5, last_access=? WHERE id=?",
                           (old, mid))
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
    links = eng.store.get_links(entry["id"])
    assert sum(1 for link in links if link["relation"] == "profiles") == 8
    assert report["compaction"]["tokens_before"] > report["compaction"]["tokens_after"] > 0


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


def test_digest_inherits_strictest_sensitivity_and_trust_of_its_sources():
    eng, wid, rid, ids = _cluster_with_one_secret_untrusted_source()

    report = consolidate(eng, workspace_id=wid, repo_id=rid)

    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    # The laundering channel is real: the secret source is quoted verbatim.
    assert "CI run 101" in digest.content
    assert digest.sensitivity == "secret", "a digest quoting secret sources must not sync"
    assert digest.provenance.get("trusted") is False
    assert digest.metadata["provenance"]["trusted"] is False
    # Inheritance must not clobber the provenance the digest already carries.
    assert digest.metadata["provenance"]["source"] == "consolidation"
    assert set(digest.metadata["provenance"]["consolidates"]) == set(ids)


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

    assert report["digests_created"]
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
    digest = eng.store.get_memory(report["digests_created"][0]["id"])

    assert digest.provenance["trusted"] is False
    assert digest.metadata["provenance"]["trusted"] is False


def test_profile_digest_inherits_strictest_sensitivity_and_trust():
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

    profile = eng.store.get_memory(report["profiles_created"][0]["id"])
    assert profile.sensitivity == "sensitive"
    assert profile.provenance.get("trusted") is False
    assert profile.metadata["provenance"]["source"] == "profile_consolidation"
    assert evolved == []


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


def test_archive_pass_sees_transients_behind_newer_semantic_rows(monkeypatch):
    from engraphis.core import consolidate as consolidate_module

    monkeypatch.setattr(consolidate_module, "DISTILL_SCAN_LIMIT", 3)
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    stale = eng.remember("Scratch note from an old session.", workspace_id=wid,
                         mtype=MemoryType.WORKING, resolve_conflicts=False)
    eng.store.conn.execute("UPDATE memories SET stability=0.01, last_access=? WHERE id=?",
                           (time.time() - 86_400, stale))
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
    eng.store.conn.execute(
        "UPDATE memories SET stability=0.01, last_access=? WHERE id=?",
        (time.time() - 86_400, stale),
    )
    eng.store.conn.commit()

    archived_at = time.time()
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


# ── explicit local consolidation command ─────────────────────────────────────

from scripts.consolidate import main as consolidate_main  # noqa: E402


def _seed_db(tmp_path):
    db = tmp_path / "mem.db"
    eng = MemoryEngine.create(str(db))
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for i in range(3):
        eng.remember(f"Build failed on the flaky network test in CI run {i}.",
                     workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                     resolve_conflicts=False)
    return db


def test_supersede_sources_cli_flag_requires_structured(tmp_path, capsys):
    db = _seed_db(tmp_path)
    assert consolidate_main([
        "--db", str(db), "--workspace", "w", "--supersede-sources",
    ]) == 2
    assert "requires --structured" in capsys.readouterr().err


def test_explicit_sweep_needs_no_license(tmp_path):
    db = _seed_db(tmp_path)
    assert consolidate_main(["--db", str(db), "--workspace", "w"]) == 0
