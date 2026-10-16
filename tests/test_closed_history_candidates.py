"""Closed-history lookup ranks topics before its bounded candidate window."""

import pytest

from engraphis.core import store as store_module
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, Scope


@pytest.fixture
def engine():
    engine = MemoryEngine.create(":memory:")
    try:
        yield engine
    finally:
        engine.store.close()


def _closed(engine, workspace, repo=None, **overrides):
    fields = dict(
        id="", content="quasar launch protocol", workspace_id=workspace, repo_id=repo,
        scope=Scope.REPO if repo else Scope.WORKSPACE,
        valid_from=-1.0, valid_to=0.0, valid_to_recorded_at=1.0, ingested_at=1.0,
        provenance={"trusted": True, "review_state": "approved"},
    )
    fields.update(overrides)
    return engine.store.add_memory(MemoryRecord(**fields))


@pytest.mark.parametrize("topic_field", ["content", "title"])
@pytest.mark.parametrize("query", ["quasar launch protocol", "PROTOCOL launch quasar"])
def test_older_exact_topic_survives_501_unrelated_closed_memories(
    engine, monkeypatch, topic_field, query,
):
    workspace = engine.store.get_or_create_workspace("history")
    fields = {topic_field: "quasar launch protocol"}
    if topic_field == "title":
        fields["content"] = "A preserved historical decision."
    exact = _closed(engine, workspace, **fields)
    for index in range(501):
        _closed(engine, workspace, content=f"Gardening weather bulletin {index}", ingested_at=2.0 + index)

    hydrated = []
    original = store_module._row_to_record

    def record_hydration(row):
        hydrated.append(row["id"])
        return original(row)

    monkeypatch.setattr(store_module, "_row_to_record", record_hydration)
    history = engine.timeline(
        query, workspace_id=workspace, known_at=1000.0,
        valid_at=1000.0, closed_only=True, prompt_only=True,
    )
    assert [record.id for record in history] == [exact]
    assert len(hydrated) == 500, "query-aware lookup must retain its bounded record window"


def test_topic_candidates_preserve_scope_status_and_temporal_boundaries(engine):
    workspace = engine.store.get_or_create_workspace("history")
    foreign = engine.store.get_or_create_workspace("foreign")
    repo = engine.store.get_or_create_repo(workspace, "api")
    sibling = engine.store.get_or_create_repo(workspace, "web")
    session = engine.start_session(workspace, repo)
    exact = _closed(engine, workspace, repo)
    forbidden = set()
    excluded = [
        {"workspace_id": foreign, "repo_id": None, "scope": Scope.WORKSPACE},
        {"repo_id": sibling},
        {"scope": Scope.SESSION, "session_id": session},
        {"provenance": {"trusted": False, "review_state": "pending"}},
        {"metadata": {"quarantine": {"state": "quarantined"}}},
        {"ingested_at": 2000.0},
        {"expired_at": 10.0},
        {"valid_to_recorded_at": 2000.0},
        {"valid_to": None},
        {"valid_to": 2000.0},
    ]
    for index in range(501):
        _closed(engine, workspace, repo,
                content=f"Gardening weather bulletin {index}", ingested_at=2.0 + index)
        for fields in excluded:
            overrides = {"ingested_at": 2.0 + index, **fields}
            forbidden.add(_closed(engine, workspace, repo, **overrides))
    history = engine.timeline(
        "quasar launch protocol", workspace_id=workspace, repo_id=repo,
        limit=1, valid_at=1000.0, known_at=1000.0, closed_only=True, prompt_only=True,
    )
    assert [record.id for record in history] == [exact]
    assert not forbidden.intersection(record.id for record in history)


def test_closed_history_candidate_query_keeps_a_recent_nonlexical_fallback(engine):
    workspace = engine.store.get_or_create_workspace("history")
    exact = _closed(engine, workspace)
    recent = _closed(engine, workspace, content="Gardening weather bulletin", ingested_at=2.0)
    records = engine.store.list_memories(
        include_invalid=True, closed_only=True, prompt_only=True, limit=2,
        candidate_query="quasar launch protocol",
    )
    assert [record.id for record in records] == [exact, recent]
    ordinary = engine.store.list_memories(
        include_invalid=True, closed_only=True, prompt_only=True, limit=2,
    )
    assert [record.id for record in ordinary] == [recent, exact]


def test_candidate_query_binds_literals_and_bounds_expanded_terms(engine):
    workspace = engine.store.get_or_create_workspace("history")
    exact = _closed(engine, workspace, content="literal_%' phrase")
    _closed(engine, workspace, content="literalOTHER phrase", ingested_at=2.0)
    records = engine.store.list_memories(
        include_invalid=True, closed_only=True, limit=1,
        candidate_query="literal_%' phrase",
    )
    assert [record.id for record in records] == [exact]
    # A long query must not exceed SQLite's expression/parameter limits. Its topic
    # terms beyond the documented expansion window carry no recall guarantee.
    records = engine.store.list_memories(
        include_invalid=True, closed_only=True, limit=1,
        candidate_query="phrase " + " ".join(f"term{i}" for i in range(1500)),
    )
    assert len(records) == 1
