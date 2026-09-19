from types import SimpleNamespace

import pytest

from eval.campaign_adapters import (
    AdapterCapabilityError,
    AdapterConfigurationError,
    AdapterError,
    EngraphisAdapter,
    GraphitiAdapter,
    Mem0Adapter,
    _call_with_fallbacks,
    _merge_peer_result_pages,
    _pack_peer_items,
)


class FakeMem0:
    def __init__(self):
        self.add_calls = []
        self.search_calls = []
        self.counter = 0
        self.records = {}

    def add(self, messages, **kwargs):
        self.add_calls.append((messages, kwargs))
        self.counter += 1
        memory_id = f"mem-{self.counter}"
        content = messages[0]["content"] if isinstance(messages, list) else str(messages)
        metadata = dict(kwargs.get("metadata") or {})
        self.records[memory_id] = {
            "id": memory_id, "memory": content, "metadata": metadata,
            "user_id": kwargs.get("user_id"),
        }
        return {"results": [{"id": memory_id}]}

    def search(self, query, **kwargs):
        self.search_calls.append((query, kwargs))
        filters = kwargs.get("filters") or {}
        user_id = filters.get("user_id", kwargs.get("user_id"))
        return {"results": [
            record for record in self.records.values()
            if user_id is None or record["user_id"] == user_id
        ]}


class FakeGraphiti:
    def __init__(self):
        self.add_calls = []
        self.search_calls = []
        self.counter = 0
        self.index_calls = 0

    async def build_indices_and_constraints(self):
        self.index_calls += 1

    async def add_episode(self, **kwargs):
        self.add_calls.append(kwargs)
        self.counter += 1
        return SimpleNamespace(episode=SimpleNamespace(uuid=f"episode-{self.counter}"))

    def search(self, query, **kwargs):
        self.search_calls.append((query, kwargs))
        return [SimpleNamespace(
            uuid="edge-1", fact="graph fact", episodes=["episode-1"],
        )]


class FakeBudgeted:
    is_budgeted = True


def _workspace_records():
    return [
        {"record_id": "b", "content": "second complete fact", "timestamp": 2,
         "scope": "workspace", "workspace": "workspace-a", "repo": "repo-a",
         "session": "session-a", "trusted": False},
        {"record_id": "a", "content": "first complete fact", "timestamp": 1,
         "scope": "workspace", "workspace": "workspace-a", "repo": "repo-a",
         "session": "session-a", "trusted": True},
    ]


def test_mem0_namespace_and_common_packing_contract():
    client = FakeMem0()
    adapter = Mem0Adapter(client=client, config={"namespace": "attempt-a"})
    prepared = adapter.prepare(workspace_id="workspace-a")
    assert prepared["namespace"] == "attempt-a"
    assert prepared["workspace_id"] != "workspace-a"
    adapter.ingest(_workspace_records())
    assert [call[0][0]["content"] for call in client.add_calls] == [
        "first complete fact", "second complete fact",
    ]
    result = adapter.recall("fact", k=2, token_budget=30)
    assert result.source_ids == ("a", "b")
    assert result.usage.context_tokens <= 30
    assert "[a] trusted=true" in result.context
    assert "[b] trusted=false" in result.context
    assert result.provenance["source_ids_are_packed_only"] is True
    assert client.search_calls[0][1]["filters"]["user_id"] == prepared["workspace_id"]


def test_adapter_signature_fallback_does_not_retry_an_in_body_type_error():
    class FailingMem0:
        def __init__(self):
            self.calls = 0

        def add(self, messages, **kwargs):
            self.calls += 1
            raise TypeError("backend write failed after mutation")

    client = FailingMem0()
    adapter = Mem0Adapter(client=client)
    adapter.prepare(workspace_id="workspace-a")
    with pytest.raises(AdapterError, match="after execution"):
        adapter.ingest([{"record_id": "one", "content": "one", "workspace": "workspace-a"}])
    assert client.calls == 1


def test_signature_fallback_skips_incompatible_shapes_before_execution():
    calls = []

    def narrow(value, *, user_id):
        calls.append((value, user_id))
        return "ok"

    result = _call_with_fallbacks(
        narrow,
        (
            (("payload",), {"user_id": "w", "metadata": {"x": 1}}),
            (("payload",), {"user_id": "w"}),
        ),
    )
    assert result == "ok"
    assert calls == [("payload", "w")]


def test_peer_packing_omits_unmapped_text_instead_of_shifting_citations():
    context, source_ids, usage, unmapped = _pack_peer_items(
        [
            {"id": "unknown", "memory": "unmapped private fact"},
            {"id": "backend-1", "memory": "mapped public fact"},
        ],
        query="fact",
        k=2,
        token_budget=50,
        memory_ids={"record-1": "backend-1"},
        trust_by_id={"record-1": True},
    )

    assert context == "[record-1] trusted=true\nmapped public fact"
    assert source_ids == ("record-1",)
    assert usage.packed_count == 1
    assert usage.omitted_count == 1
    assert unmapped == 1


def test_mem0_preflights_unsupported_scope_before_any_add():
    client = FakeMem0()
    adapter = Mem0Adapter(client=client)
    adapter.prepare(workspace_id="workspace-a")
    records = _workspace_records() + [{
        "record_id": "repo-only", "content": "repo fact", "timestamp": 3,
        "scope": "repo", "workspace": "workspace-a", "repo": "repo-a",
        "session": "session-a",
    }]
    with pytest.raises(AdapterCapabilityError, match="repo"):
        adapter.ingest(records)
    assert client.add_calls == []


def test_peer_packing_backfills_after_unmapped_and_empty_rows():
    context, source_ids, usage, unmapped = _pack_peer_items(
        [{"id": "unknown-1", "memory": "first derived fact"},
         {"id": "unknown-2", "memory": "second derived fact"},
         {"id": "empty", "memory": " "},
         {"id": "backend-1", "memory": "first mapped fact"},
         {"id": "backend-2", "memory": "second mapped fact"},
         {"id": "backend-3", "memory": "beyond the candidate limit"}],
        query="fact", k=2, token_budget=100,
        memory_ids={"record-1": "backend-1", "record-2": "backend-2", "record-3": "backend-3"},
    )
    assert source_ids == ("record-1", "record-2")
    assert usage.packed_count == 2
    assert usage.omitted_count == 3
    assert unmapped == 2
    assert "derived" not in context
    assert "beyond" not in context


def test_peer_packing_applies_k_to_mapped_candidates_before_budget_filtering():
    context, source_ids, usage, unmapped = _pack_peer_items(
        [{"id": "backend-1", "memory": "oversized " * 100},
         {"id": "backend-2", "memory": "short fact"}],
        query="fact", k=1, token_budget=20,
        memory_ids={"record-1": "backend-1", "record-2": "backend-2"},
    )
    assert context == ""
    assert source_ids == ()
    assert usage.omitted_count == 1
    assert unmapped == 0


def test_mem0_repo_partition_results_are_merged_by_score_before_k_limit():
    class PartitionedMem0(FakeMem0):
        def search(self, query, **kwargs):
            result = super().search(query, **kwargs)
            partition = (kwargs.get("filters") or {}).get("user_id")
            if partition == adapter._workspace_partition:
                result["results"] = [{
                    "id": "workspace-backend",
                    "memory": "workspace distractor",
                    "score": 0.1,
                    "metadata": {"campaign_record_id": "workspace-fact",
                                  "campaign_partition": partition,
                                  "campaign_trusted": True},
                }]
            else:
                result["results"] = [{
                    "id": "repo-backend",
                    "memory": "repo-specific evidence",
                    "score": 0.9,
                    "metadata": {"campaign_record_id": "repo-fact",
                                  "campaign_partition": partition,
                                  "campaign_trusted": True},
                }]
            return result

    client = PartitionedMem0()
    adapter = Mem0Adapter(
        client=client,
        config={"namespace": "attempt-merge", "scope_partition": "repo"},
    )
    adapter.prepare(workspace_id="workspace-a", repo_id="repo-a")
    adapter.ingest([{
        "record_id": "workspace-fact", "content": "workspace distractor",
        "scope": "workspace", "workspace": "workspace-a", "trusted": True,
    }, {
        "record_id": "repo-fact", "content": "repo-specific evidence",
        "scope": "repo", "workspace": "workspace-a", "repo": "repo-a",
        "trusted": True,
    }])

    result = adapter.recall("evidence", k=1, token_budget=20)

    assert result.source_ids == ("repo-fact",)
    assert "repo-specific evidence" in result.context


def test_peer_page_merge_interleaves_when_backend_scores_are_unavailable():
    merged = _merge_peer_result_pages(
        [[{"id": "workspace"}], [{"id": "repo"}]],
    )

    assert [item["id"] for item in merged] == ["workspace", "repo"]


def test_peer_repo_partition_is_explicit_and_keeps_sibling_facts_out():
    client = FakeMem0()
    adapter = Mem0Adapter(
        client=client,
        config={"namespace": "attempt-repo", "scope_partition": "repo"},
    )
    prepared = adapter.prepare(workspace_id="workspace-a", repo_id="repo-a")
    assert prepared["scope_partition"] == "repo"
    assert prepared["scope_projection"].startswith("isolated_repo_partition")
    adapter.ingest([{
        "record_id": "workspace-fact", "content": "shared workspace fact", "timestamp": 1,
        "scope": "workspace", "workspace": "workspace-a", "repo": "repo-a",
    }, {
        "record_id": "repo-fact", "content": "repo fact", "timestamp": 2,
        "scope": "repo", "workspace": "workspace-a", "repo": "repo-a",
    }])
    assert len(client.add_calls) == 2
    adapter.ingest([{
        "record_id": "sibling", "content": "sibling repo fact", "timestamp": 3,
        "scope": "repo", "workspace": "workspace-a", "repo": "repo-b",
    }])
    assert len(client.add_calls) == 3
    result = adapter.recall("fact", k=10, token_budget=50)
    assert "shared workspace fact" in result.context
    assert "repo fact" in result.context
    assert "sibling repo fact" not in result.context
    assert "sibling" not in result.source_ids
    assert len(client.search_calls) == 2


def test_peer_temporal_filter_is_explicitly_unsupported():
    adapter = Mem0Adapter(client=FakeMem0())
    adapter.prepare(workspace_id="workspace-a")
    with pytest.raises(AdapterCapabilityError, match="valid_at"):
        adapter.recall("fact", valid_at=1.0)


def test_graphiti_maps_episode_evidence_and_preflights_scope():
    client = FakeGraphiti()
    adapter = GraphitiAdapter(client=client, config={"namespace": "attempt-b"})
    prepared = adapter.prepare(workspace_id="workspace-a")
    adapter.ingest(_workspace_records())
    result = adapter.recall("fact", k=1, token_budget=10)
    assert client.index_calls == 1
    assert result.source_ids == ("a",)
    assert result.context == "[a] trusted=true\ngraph fact"
    assert client.search_calls[0][1]["group_ids"] == [prepared["workspace_id"]]
    assert [item["group_id"] for item in client.add_calls] == [prepared["workspace_id"]] * 2

    with pytest.raises(AdapterCapabilityError, match="repo"):
        adapter.ingest([{
            "record_id": "repo-only", "content": "repo fact", "timestamp": 3,
            "scope": "repo", "workspace": "workspace-a", "repo": "repo-a",
        }])
    assert len(client.add_calls) == 2


def test_peer_requires_budgeted_client_for_real_constructor():
    with pytest.raises(AdapterConfigurationError, match="budgeted"):
        Mem0Adapter(client_factory=lambda **_: object())
    with pytest.raises(AdapterConfigurationError, match="budgeted"):
        GraphitiAdapter(client_factory=lambda **_: object())


def test_engraphis_real_engine_uses_packed_chunks_and_scopes(tmp_path):
    adapter = EngraphisAdapter(db_path=str(tmp_path / "memory.db"))
    adapter.prepare(
        workspace_id="workspace-a", repo_id="repo-a", session_id="session-a",
    )
    ids = adapter.ingest([{
        "record_id": "repo-fact", "content": "repository owner is delta", "timestamp": 1,
        "scope": "repo", "workspace": "workspace-a", "repo": "repo-a", "session": "session-a",
        "trusted": True,
    }])
    assert ids
    result = adapter.recall("repository owner", k=1, token_budget=20)
    assert result.source_ids == ("repo-fact",)
    assert result.usage.context_tokens <= 20
    with pytest.raises(ValueError, match="positive"):
        adapter.recall("repository owner", k=0)
    adapter.close()


def test_engraphis_fixture_clock_seeds_known_at_and_retention_age(tmp_path):
    anchor = 1_000_000.0
    adapter = EngraphisAdapter(
        db_path=str(tmp_path / "fixture-clock.db"),
        engine_kwargs={
            "fixture_clock": {
                "mode": "anchored",
                "anchor": anchor,
            },
        },
    )
    adapter.prepare(workspace_id="workspace-a", repo_id="repo-a")
    adapter.ingest([{
        "record_id": "old-fact", "content": "fixture owner is alpha", "timestamp": 5,
        "valid_at": 5, "known_at": 5, "scope": "repo", "workspace": "workspace-a",
        "repo": "repo-a", "trusted": True,
    }, {
        "record_id": "new-fact", "content": "fixture owner is beta", "timestamp": 25,
        "valid_at": 25, "known_at": 25, "scope": "repo", "workspace": "workspace-a",
        "repo": "repo-a", "trusted": True,
    }])
    old_id = adapter._memory_ids["old-fact"]
    row = adapter.engine.store.conn.execute(
        "SELECT ingested_at, valid_from FROM memories WHERE id=?", (old_id,)
    ).fetchone()
    assert row["ingested_at"] == anchor + 5
    assert row["valid_from"] == anchor + 5

    result = adapter.recall(
        "fixture owner alpha", k=2, token_budget=20, valid_at=5, known_at=25,
    )
    assert result.source_ids == ("old-fact",)
    assert result.provenance["fixture_clock"]["mapped_known_at"] == anchor + 25
    assert adapter.metrics()["fixture_clock"]["mode"] == "fixture_anchor"
    adapter.close()
