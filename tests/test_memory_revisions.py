"""A complete edit has one durable result, even when its response is lost."""
import pytest

from engraphis.core.interfaces import MemoryType
from engraphis.core.mutations import MemoryConflict, memory_version
from engraphis.service import MemoryService


@pytest.fixture
def svc(tmp_path):
    service = MemoryService.create(str(tmp_path / "revisions.db"), extractor="none")
    yield service
    service.close()


def seed(svc):
    result = svc.remember("The cache expires after 30 days.", workspace="w", repo="api", source="web", trusted=False)
    record = svc.store.get_memory(result["id"])
    return record.id, memory_version(record)


def test_complete_revision_and_retry_after_reopening(svc, monkeypatch):
    mid, version = seed(svc)
    kwargs = dict(workspace="w", repo="api", expected_version=version,
                  operation_id="edit-1", content="The cache expires after 90 days.",
                  title="Cache retention", mtype="procedural", importance=0.9,
                  reason="Updated project policy")
    result = svc.revise_memory(mid, **kwargs)
    replacement = svc.store.get_memory(result["id"])
    assert replacement.content == kwargs["content"]
    assert replacement.title == kwargs["title"]
    assert replacement.mtype == MemoryType.PROCEDURAL
    assert replacement.importance == 0.9
    assert replacement.provenance["trusted"] is False
    assert svc.store.get_memory(mid).valid_to is not None
    assert memory_version(replacement) == result["version"]
    second = MemoryService.create(svc.store.path, extractor="none")
    try:
        def unavailable(*args, **kwargs):
            raise RuntimeError("embedding provider unavailable after committed edit")

        monkeypatch.setattr(second.engine.embedder, "embed", unavailable)
        assert second.revise_memory(mid, **kwargs) == result
    finally:
        second.close()
    assert svc.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


@pytest.mark.parametrize("operation", ["correct", "revise"])
@pytest.mark.parametrize("removal", [None, "retire", "secure_erase"])
def test_committed_edit_retry_does_not_depend_on_embedding(svc, monkeypatch, operation, removal):
    mid, version = seed(svc)

    def edit():
        if operation == "correct":
            return svc.correct(mid, "The cache expires after 90 days.", workspace="w")
        return svc.revise_memory(
            mid, workspace="w", expected_version=version, operation_id="offline-retry",
            content="The cache expires after 90 days.", title="Retention policy",
        )

    committed = edit()
    if removal:
        getattr(svc.engine, removal)(committed["id"])

    def unavailable(*args, **kwargs):
        raise RuntimeError("embedding provider unavailable after committed edit")

    monkeypatch.setattr(svc.engine.embedder, "embed", unavailable)
    before = svc.store.conn.total_changes
    if removal:
        with pytest.raises(MemoryConflict) as caught:
            edit()
        assert caught.value.code == "result_unavailable"
    else:
        assert edit() == committed
    assert svc.store.conn.total_changes == before


def test_conflicting_operation_reuse_and_stale_edit_do_not_mutate(svc):
    mid, version = seed(svc)
    kwargs = dict(workspace="w", expected_version=version, operation_id="edit-1",
                  content="The cache expires after 90 days.")
    first = svc.revise_memory(mid, **kwargs)
    with pytest.raises(MemoryConflict):
        svc.revise_memory(mid, **{**kwargs, "content": "The cache expires after 60 days."})
    with pytest.raises(MemoryConflict):
        svc.revise_memory(first["id"], **{**kwargs, "operation_id": "edit-2"})
    assert svc.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


def test_slow_revision_embedding_does_not_hold_writer_and_revalidates(svc, monkeypatch):
    mid, version = seed(svc)
    other = MemoryService.create(svc.store.path, extractor="none")
    original = svc.engine.embedder.embed

    def embed(texts, *, kind="text"):
        assert not svc.store.conn.transaction_owned_by_current_thread()
        other.pin(mid, workspace="w")
        return original(texts, kind=kind)

    monkeypatch.setattr(svc.engine.embedder, "embed", embed)
    try:
        with pytest.raises(MemoryConflict):
            svc.revise_memory(mid, workspace="w", expected_version=version,
                              operation_id="slow-edit", title="Cache")
        assert svc.store.get_memory(mid).valid_to is None
        assert svc.store.conn.execute("SELECT COUNT(*) FROM memory_commands").fetchone()[0] == 0
    finally:
        other.close()


def test_revision_failure_keeps_content_labels_and_receipt_atomic(svc, monkeypatch):
    mid, version = seed(svc)
    original = svc.store.close_validity

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("interrupted before commit")

    monkeypatch.setattr(svc.store, "close_validity", fail)
    with pytest.raises(RuntimeError):
        svc.revise_memory(mid, workspace="w", expected_version=version,
                          operation_id="failed-edit", content="New fact", title="New label")
    assert memory_version(svc.store.get_memory(mid)) == version
    assert svc.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert svc.store.conn.execute("SELECT COUNT(*) FROM memory_commands").fetchone()[0] == 0


def test_legacy_title_edit_prepares_before_writer_and_preserves_in_place_contract(svc, monkeypatch):
    mid, _ = seed(svc)
    original = svc.engine.embedder.embed
    observed = []

    def embed(texts, *, kind="text"):
        observed.append(svc.store.conn.transaction_owned_by_current_thread())
        return original(texts, kind=kind)

    monkeypatch.setattr(svc.engine.embedder, "embed", embed)
    result = svc.update_memory(mid, workspace="w", title="Cache retention", importance=0.7)
    assert result["id"] == mid and observed == [False]
    assert svc.store.get_memory(mid).title == "Cache retention"
    assert svc.store.get_memory(mid).valid_to is None


def test_rest_revision_returns_typed_conflict_and_keeps_scope(svc, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    mid, version = seed(svc)
    with TestClient(app) as client:
        body = dict(id=mid, workspace="w", repo="api", expected_version=version,
                    operation_id="http-edit", content="The cache expires after 60 days.")
        result = client.post("/api/memory/revise", json=body)
        assert result.status_code == 200, result.text
        assert client.post("/api/memory/revise", json=body).json() == result.json()
        conflict = client.post("/api/memory/revise", json={**body, "content": "Other content"})
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "operation_conflict"
        assert client.get("/api/repos", params={"workspace": "w"}).json()["repos"][0]["name"] == "api"
        detail = client.get("/api/memory/" + result.json()["id"], params={"workspace": "w"})
        assert detail.json()["memory"]["version"] == result.json()["version"]
        assert client.post("/api/memory/revise", json={**body, "workspace": "foreign"}).status_code == 400


def test_history_pages_by_lineage_and_survives_unrelated_activity(svc):
    mid, _ = seed(svc)
    identities = [mid]
    for day in range(31, 94):
        result = svc.correct(identities[-1], f"The cache expires after {day} days.", workspace="w")
        identities.append(result["id"])
    svc.remember("An unrelated fact with the same title.", workspace="w", title="Cache")
    cursor, seen = "", []
    while True:
        page = svc.memory_history(mid, workspace="w", limit=7, cursor=cursor)
        assert page["total_count"] == len(identities)
        seen.extend(record["id"] for record in page["versions"])
        svc.store.audit("test", "unrelated", "", "")
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == identities


@pytest.mark.parametrize("field", ["supersedes", "promoted_from"])
@pytest.mark.parametrize("kind", ["integer", "boolean", "string", "mapping", "invalid-list"])
def test_history_and_inspection_ignore_malformed_lineage(svc, field, kind):
    mid, _ = seed(svc)
    malformed = {"integer": 1, "boolean": True, "string": f"prefix-{mid}-suffix",
                 "mapping": {mid: True}, "invalid-list": [1, None, {}]}[kind]
    unrelated = svc.remember(
        "An unrelated record mentions an identifier.", workspace="w", repo="api",
        metadata={"note": mid, field: malformed},
    )["id"]
    for target in (mid, unrelated):
        assert [row["id"] for row in svc.inspect(target, workspace="w")["chain"]] == [target]
        assert [row["id"] for row in svc.memory_history(target, workspace="w")["versions"]] == [target]


@pytest.mark.parametrize("field", ["supersedes", "promoted_from"])
def test_history_preserves_exact_ids_in_mixed_lineage_lists(svc, field):
    mid, _ = seed(svc)
    successor = svc.remember(
        "A recorded successor.", workspace="w", repo="api",
        metadata={field: [1, None, mid, {}]},
    )["id"]
    for target in (mid, successor):
        assert {row["id"] for row in svc.inspect(target, workspace="w")["chain"]} == {mid, successor}
        assert {row["id"] for row in svc.memory_history(target, workspace="w")["versions"]} == {mid, successor}


def test_unrelated_non_object_metadata_cannot_break_rest_history(svc, monkeypatch):
    import json
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    mid, _ = seed(svc)
    other = svc.remember("Unrelated legacy metadata.", workspace="w")["id"]
    svc.store.conn.execute("UPDATE memories SET metadata=? WHERE id=?", (json.dumps([mid]), other))
    svc.store.conn.commit()
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client:
        for suffix, key in (("", "chain"), ("/history", "versions")):
            response = client.get(f"/api/memory/{mid}{suffix}", params={"workspace": "w"})
            assert response.status_code == 200, response.text
            assert [row["id"] for row in response.json()[key]] == [mid]


def test_mcp_correct_uses_atomic_guard_and_typed_conflict(svc, monkeypatch):
    pytest.importorskip("mcp")
    from engraphis import mcp_server
    import json

    monkeypatch.setattr(mcp_server, "service", lambda: svc)
    mid, _ = seed(svc)
    first = json.loads(mcp_server.engraphis_correct(
        workspace="w", memory_id=mid, new_content="The cache expires after 60 days.",
    ))
    retry = json.loads(mcp_server.engraphis_correct(
        workspace="w", memory_id=mid, new_content="The cache expires after 60 days.",
    ))
    assert first["id"] == retry["id"]
    conflict = json.loads(mcp_server.engraphis_correct(
        workspace="w", memory_id=mid, new_content="The cache expires after 90 days.",
    ))
    assert conflict["code"] == "memory_conflict" and conflict["retryable"] is False


@pytest.mark.parametrize("position", [
    [float("nan"), 0, "mem_1"], [float("inf"), 0, "mem_1"],
    [1, True, "mem_1"], [1, -1, "mem_1"], [1, 2**64, "mem_1"],
])
def test_history_rejects_invalid_cursor_positions(svc, position):
    import base64
    import json
    from engraphis.service import ValidationError

    mid, _ = seed(svc)
    svc.correct(mid, "The cache expires after 60 days.", workspace="w")
    page = svc.memory_history(mid, workspace="w", limit=1)
    cursor = json.loads(base64.urlsafe_b64decode(page["next_cursor"]))
    cursor["position"] = position
    encoded = base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode()
    with pytest.raises(ValidationError, match="invalid history cursor"):
        svc.memory_history(mid, workspace="w", limit=1, cursor=encoded)
