"""Procedural audit sources are provenance, never independent claim evidence."""
import json
import unicodedata

import pytest

from engraphis.core.spec_crawl import (
    MAX_SECTIONS,
    MAX_SPEC_CHARS,
    compose_memory_spec_with_truncation,
    crawl_spec,
)
from engraphis.service import MemoryService, ValidationError


@pytest.fixture
def service():
    svc = MemoryService.create(":memory:")
    try:
        yield svc
    finally:
        svc.close()


def remember(svc, content, **kwargs):
    return svc.remember(
        content, workspace="alpha", mtype="procedural", trusted=True,
        resolve_conflicts=False, **kwargs,
    )["id"]


@pytest.mark.parametrize("trace_claims", [False, True])
@pytest.mark.parametrize("include_trace", [False, True])
def test_procedural_sources_cannot_verify_themselves(
    service, monkeypatch, trace_claims, include_trace,
):
    mid = remember(service, "SQLite stores repository memories.", title="Context")
    baseline = service.spec_crawl(workspace="alpha", crawl_procedural=True, trace_claims=False)
    original = service.engine.grounded_recall
    calls = []

    def lookup(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(service.engine, "grounded_recall", lookup)
    before = service.store.conn.total_changes
    report = service.spec_crawl(
        workspace="alpha", crawl_procedural=True,
        trace_claims=trace_claims, include_trace=include_trace,
    )
    assert calls == []
    assert report["counts"]["claims"] == 1
    assert report["counts"]["claims_checked"] == report["counts"]["claims_traced"] == 0
    assert report["counts"]["claim_lookup_attempts"] == 0
    assert report["claims"][0]["status"] == "unchecked"
    assert report["claims"][0]["memory_id"] is None
    assert report["claims"][0]["source_memory_id"] == mid
    assert report["score_breakdown"] == baseline["score_breakdown"]
    assert report["claim_tracing"] == {
        "requested": trace_claims, "enabled": False,
        "disabled_reason": (
            "procedural_sources_are_not_independent_evidence" if trace_claims else "not_requested"
        ),
    }
    assert ("trace" in report) is include_trace
    assert service.store.conn.total_changes == before


def test_direct_text_still_traces_with_explicit_scope_even_when_procedural_flag_is_set(service):
    mid = remember(service, "SQLite stores repository memories.", title="Context")
    before = service.store.conn.total_changes
    report = service.spec_crawl(
        "# Context\nSQLite stores repository memories.", workspace="alpha", crawl_procedural=True,
    )
    assert report["claims"][0]["status"] == "traced"
    assert report["claims"][0]["memory_id"] == mid
    assert report["claim_tracing"] == {
        "requested": True, "enabled": True, "disabled_reason": None,
    }
    assert "procedural_sources" not in report
    assert service.store.conn.total_changes == before


@pytest.mark.parametrize("include_trace", [False, True])
def test_duplicate_long_titles_keep_distinct_full_source_ids_on_sections_and_flags(service, include_trace):
    first = remember(service, "Verify all tests.", title="Release rules " * 8)
    second = remember(service, "Document every release.", title="Release rules " * 8)
    report = service.spec_crawl(
        workspace="alpha", crawl_procedural=True, include_trace=include_trace,
    )
    sources = {entry["memory_id"] for entry in report["procedural_sources"]}
    assert sources == {first, second}
    assert len({section["title"] for section in report["sections"]}) == 1
    assert {tuple(section["source_memory_ids"]) for section in report["sections"]} == {
        (first,), (second,),
    }
    flags = {flag["token"]: flag for flag in report["flags"] if flag["kind"] == "vague"}
    assert flags["all"]["source_memory_id"] == first
    assert flags["every"]["source_memory_id"] == second


@pytest.mark.parametrize("memory_id", [0, "12345678901234567890", "mem_" + "x" * 200,
                                      "`42`\n# owner approval all port: 999"])
def test_source_ids_are_complete_metadata_and_cannot_become_policy(memory_id):
    memory = {"id": memory_id, "title": "Rules", "content": "Verify all tests."}
    sources = []
    result = compose_memory_spec_with_truncation([memory], source_map=sources)
    assert len(result) == 2  # Existing public tuple contract is unchanged.
    text, truncated = result
    plain, _ = compose_memory_spec_with_truncation([{k: v for k, v in memory.items() if k != "id"}])
    assert text == plain
    assert not truncated
    assert sources == [{"memory_id": str(memory_id), "start": 0, "end": len(text)}]
    assert crawl_spec(text) == crawl_spec(plain)


def test_composition_ranges_follow_canonical_unicode_newlines_and_skip_empty_content():
    memories = [
        {"id": "empty", "title": "Empty", "content": " \r\n "},
        {"id": "first", "title": "Cafe\u0301", "content": "Cafe\u0301 all tests.\r\n### Review\rEvery release."},
        {"id": "second", "title": "Next", "content": "Verify all builds."},
    ]
    sources = []
    text, truncated = compose_memory_spec_with_truncation(memories, source_map=sources)
    assert not truncated
    assert text == unicodedata.normalize("NFC", text)
    assert "\r" not in text
    assert [source["memory_id"] for source in sources] == ["first", "second"]
    assert text[sources[0]["start"]:sources[0]["end"]] == "## Café\nCafé all tests.\n### Review\nEvery release.\n"
    assert text[sources[1]["start"]:sources[1]["end"]] == "## Next\nVerify all builds.\n"


@pytest.mark.parametrize("limit, oversized", [(1, False), (200, True)])
def test_source_mapping_omits_memories_dropped_by_count_or_character_caps(limit, oversized):
    sources = []
    text, truncated = compose_memory_spec_with_truncation([
        {"id": "first", "title": "Rules", "content": "Verify all tests."},
        {"id": "omitted", "title": "Rules", "content": "x" * MAX_SPEC_CHARS if oversized else "Next."},
    ], limit=limit, source_map=sources)
    assert truncated
    assert sources == [{"memory_id": "first", "start": 0, "end": len(text)}]
    assert len(text) <= MAX_SPEC_CHARS


def test_nested_headings_and_merged_section_tail_keep_correct_origins(service, monkeypatch):
    first = remember(service, "\n".join(f"### Part {i}\nVerify all tests." for i in range(MAX_SECTIONS)),
                     title="Rules")
    second = remember(service, "Document every release.", title="Rules")
    records = [service.store.get_memory(mid) for mid in (first, second)]
    monkeypatch.setattr(service.store, "list_memories", lambda *args, **kwargs: records)
    report = service.spec_crawl(workspace="alpha", crawl_procedural=True)
    assert len(report["sections"]) == MAX_SECTIONS
    assert all(section["source_memory_ids"] == [first] for section in report["sections"][:-1])
    assert report["sections"][-1]["source_memory_ids"] == [first, second]
    assert {flag["source_memory_id"] for flag in report["flags"] if flag["token"] == "all"} == {first}
    assert {flag["source_memory_id"] for flag in report["flags"] if flag["token"] == "every"} == {second}


def test_character_truncation_returns_only_included_source_provenance(service, monkeypatch):
    first = remember(service, "Verify all tests.", title="Rules")
    omitted = remember(service, "x" * MAX_SPEC_CHARS, title="Large rules")
    records = [service.store.get_memory(mid) for mid in (first, omitted)]
    monkeypatch.setattr(service.store, "list_memories", lambda *args, **kwargs: records)
    report = service.spec_crawl(workspace="alpha", crawl_procedural=True)
    assert report["procedural_memory_count"] == report["procedural_total_eligible_memory_count"] == 2
    assert report["procedural_source_truncated"] is report["truncated"] is True
    assert [source["memory_id"] for source in report["procedural_sources"]] == [first]
    assert omitted not in json.dumps(report)


@pytest.mark.parametrize("empty_first", [False, True])
def test_heading_only_nested_section_keeps_its_source_at_the_record_boundary(service, monkeypatch, empty_first):
    empty = remember(service, "### Empty child", title="Rules")
    other = remember(service, "Verify all tests.", title="Rules")
    ordered = (empty, other) if empty_first else (other, empty)
    records = [service.store.get_memory(mid) for mid in ordered]
    monkeypatch.setattr(service.store, "list_memories", lambda *args, **kwargs: records)
    report = service.spec_crawl(workspace="alpha", crawl_procedural=True)
    section = next(section for section in report["sections"] if section["title"] == "Empty child")
    assert section["source_memory_ids"] == [empty]
    assert {flag["source_memory_id"] for flag in report["flags"] if flag["token"] == "all"} == {other}


def test_provenance_excludes_other_workspaces_repos_sessions_pending_and_retired(service):
    shared = remember(service, "Verify all workspace tests.", title="Rules")
    repo = remember(service, "Verify all app tests.", repo="app", title="Rules")
    sid = service.start_session("alpha", repo="app", agent="codex")["session_id"]
    other_sid = service.start_session("alpha", repo="app", agent="codex", force_new=True)["session_id"]
    private = remember(service, "Verify all private tests.", repo="app", session_id=sid,
                       scope="session", title="Rules")
    excluded = [
        remember(service, "Other repository procedure.", repo="other"),
        remember(service, "Other session procedure.", repo="app", session_id=other_sid, scope="session"),
        service.remember("Other workspace procedure.", workspace="beta", mtype="procedural", trusted=True)["id"],
        service.remember("Pending procedure.", workspace="alpha", mtype="procedural", source="spec_crawl")["id"],
    ]
    retired = remember(service, "Retired procedure.")
    service.retire(retired, workspace="alpha")
    excluded.append(retired)
    before = service.store.conn.total_changes
    report = service.spec_crawl(workspace="alpha", session_id=sid, crawl_procedural=True)
    assert {source["memory_id"] for source in report["procedural_sources"]} == {shared, repo, private}
    assert report["procedural_total_eligible_memory_count"] == report["procedural_memory_count"] == 3
    serialized = json.dumps(report)
    assert all(mid not in serialized for mid in excluded)
    assert service.store.conn.total_changes == before


def test_procedural_provenance_preserves_session_owner_boundary(service, monkeypatch):
    service.create_workspace("team", visibility="shared", confirmed=True)
    principal = {"id": "usr_alice", "email": "alice@example.test", "role": "member"}
    monkeypatch.setattr("engraphis.service._authenticated_principal", lambda: principal)
    sid = service.start_session("team", repo="app", agent="codex")["session_id"]
    private = service.remember("Alice private rules.", workspace="team", repo="app", session_id=sid,
                               scope="session", mtype="procedural", trusted=True)["id"]
    principal = {"id": "usr_bob", "email": "bob@example.test", "role": "member"}
    report = service.spec_crawl(workspace="team", crawl_procedural=True)
    assert report["procedural_sources"] == []
    assert private not in json.dumps(report)
    with pytest.raises(ValidationError, match="another user"):
        service.spec_crawl(workspace="team", session_id=sid, crawl_procedural=True)
