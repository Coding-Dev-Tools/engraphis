"""Optimistic versions include canonical temporal history without breaking legacy hashes."""
from __future__ import annotations

import hashlib
import itertools
import json
import time

import pytest

from engraphis import create_memory_engine
from engraphis.core import store as store_module
from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.mutations import MemoryCommand, MemoryConflict, memory_version


def _legacy_version(record):
    # Frozen pre-schema-19 hash contract. Changing an unrelated default must not make
    # every open editor token stale merely because histories gained a child table.
    fields = (
        "id", "workspace_id", "repo_id", "session_id", "scope", "mtype",
        "title", "content", "summary", "keywords", "importance", "confidence",
        "provenance", "metadata", "pinned", "sensitivity", "subject_key",
        "claim_kind", "valid_from", "valid_to", "valid_to_recorded_at",
        "ingested_at", "expired_at", "modified_hlc",
    )
    encoded = json.dumps(
        {field: getattr(record, field, None) for field in fields},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return "mv1:" + hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize(("valid_to", "recorded_at"), [(None, None), (20.0, None), (20.0, 100.0)])
def test_empty_and_scalar_only_histories_keep_legacy_mv1_hash(valid_to, recorded_at):
    record = MemoryRecord(
        id="mem_legacy_version", workspace_id="ws_version", scope=Scope.WORKSPACE,
        content="An established editable fact", valid_from=1.0, ingested_at=1.0,
        valid_to=valid_to, valid_to_recorded_at=recorded_at,
    )
    assert memory_version(record) == _legacy_version(record)


def test_history_hash_normalizes_duplicate_permutation_and_scalar_union():
    observations = ((20.0, 100.0), (30.0, None), (25.0, 90.0))
    record = MemoryRecord(
        id="mem_history_version", workspace_id="ws_version", scope=Scope.WORKSPACE,
        content="A fact with multiple historical closures", valid_from=1.0, ingested_at=1.0,
        valid_to=20.0, valid_to_recorded_at=100.0, closure_observations=observations,
    )
    expected = memory_version(record)
    assert expected != _legacy_version(record)
    for permutation in itertools.permutations(observations):
        # MemoryRecord is mutable; the version boundary must remain canonical even
        # when a caller supplies unnormalized duplicate observation pairs later.
        record.closure_observations = list(permutation) + [permutation[0]]
        assert memory_version(record) == expected
    record.closure_observations = [(30.0, None), (25.0, 90.0)]
    assert memory_version(record) == expected


@pytest.fixture
def engines(tmp_path):
    path = str(tmp_path / "closure-version.db")
    first = create_memory_engine(path, auto_evolve=False)
    second = create_memory_engine(path, auto_evolve=False)
    try:
        yield first, second
    finally:
        second.close()
        first.close()


def _source_with_future_closure(engine):
    workspace = engine.store.get_or_create_workspace("closure-version")
    memory_id = engine.remember(
        "The service uses an established deployment target", workspace_id=workspace,
        resolve_conflicts=False,
    )
    endpoint = time.time() + 3600
    engine.store.close_validity(memory_id, at=endpoint)
    return memory_id, endpoint


def test_prepared_command_rejects_history_only_change_from_other_connection(engines):
    first, second = engines
    memory_id, endpoint = _source_with_future_closure(first)
    prepared = first.store.get_memory(memory_id)
    command = MemoryCommand(first.store, "correct", [prepared], {"content": "A replacement target"})

    second.store.close_validity(memory_id, at=endpoint + 600)
    changed = first.store.get_memory(memory_id)

    assert changed.valid_to == prepared.valid_to
    assert changed.valid_to_recorded_at == prepared.valid_to_recorded_at
    assert changed.modified_hlc == prepared.modified_hlc
    assert changed.closure_observations != prepared.closure_observations
    assert memory_version(changed) != memory_version(prepared)
    with first.store.write_transaction():
        with pytest.raises(MemoryConflict, match="changed during preparation"):
            command.validate()


@pytest.mark.parametrize("during_embedding", [False, True])
def test_revision_rejects_stale_history_without_partial_successor(
        engines, monkeypatch, during_embedding):
    first, second = engines
    memory_id, endpoint = _source_with_future_closure(first)
    prepared = first.store.get_memory(memory_id)
    expected_version = memory_version(prepared)
    original_embed = first.embedder.embed

    def append_history():
        assert not first.store.conn.transaction_owned_by_current_thread()
        second.store.close_validity(memory_id, at=endpoint + 600)

    if during_embedding:
        def embed(texts, *, kind="text"):
            append_history()
            return original_embed(texts, kind=kind)

        monkeypatch.setattr(first.embedder, "embed", embed)
    else:
        append_history()

    with pytest.raises(MemoryConflict) as caught:
        first.revise_memory(
            memory_id, expected_version=expected_version, operation_id="stale-history-edit",
            content="The service uses a different deployment target", reason="A governed edit",
        )

    assert caught.value.code == ("memory_conflict" if during_embedding else "version_conflict")
    assert first.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert first.store.conn.execute("SELECT COUNT(*) FROM mem_vectors").fetchone()[0] == 1
    assert first.store.conn.execute("SELECT COUNT(*) FROM memory_commands").fetchone()[0] == 0
    assert first.store.conn.execute("SELECT COUNT(*) FROM memory_command_sources").fetchone()[0] == 0
    current = first.store.get_memory(memory_id)
    assert current.valid_to == endpoint
    assert len(current.closure_observations) == 2


def test_duplicate_observation_and_reinforcement_do_not_change_version(engines, monkeypatch):
    first, _ = engines
    memory_id, endpoint = _source_with_future_closure(first)
    learned_at = time.time()
    monkeypatch.setattr(store_module, "now_ts", lambda: learned_at)
    first.store.close_validity(memory_id, at=endpoint + 600)
    before = first.store.get_memory(memory_id)
    expected = memory_version(before)

    first.store.close_validity(memory_id, at=endpoint + 600)
    first.store.reinforce(memory_id, boost=.15)

    after = first.store.get_memory(memory_id)
    assert after.closure_observations == before.closure_observations
    assert after.stability > before.stability
    assert after.access_count == before.access_count + 1
    assert memory_version(after) == expected
