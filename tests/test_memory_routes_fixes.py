"""Regressions for v1 memory-route / store correctness bugs:

- list_documents(offset=..) with no limit generated invalid SQL (OFFSET without LIMIT).
- GET /memory/documents/{id} without ?namespace looked up a nonexistent '_global' ns.
- POST /memory/prune coerced an explicit minRetention=0.0 to 0.05 and over-pruned.
- POST /memory/conversations crashed (500) on a user message missing 'content'.
- POST /memory/interactions recorded signals that never reinforced any memory.
"""
import threading

import numpy as np
import pytest

from engraphis.config import settings
from engraphis.stores import get_conn, init_db, now_ts
from engraphis.stores import vectors as mem_store
from engraphis.engines import reweight


def _setup_store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "mem.db"))
    monkeypatch.setattr("engraphis.stores._local", threading.local())
    init_db()


def test_list_documents_offset_without_limit_is_valid(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    for i in range(3):
        mem_store.upsert_memory(namespace="ns", document_id="d%d" % i, title="t",
                                content="c%d" % i)
    # offset with no limit must not raise "OFFSET without LIMIT" (previously a 500).
    rest = mem_store.list_documents(namespace="ns", offset=1)
    assert len(rest) == 2


def test_find_document_without_namespace(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    mem_store.upsert_memory(namespace="vault", document_id="doc1", title="t", content="hi")
    # No namespace: still found (old code queried a nonexistent '_global' ns and 404'd).
    assert mem_store.find_document("doc1") is not None
    assert mem_store.find_document("doc1", "vault") is not None
    assert mem_store.find_document("nope") is None


def test_recall_master_none_namespace_recalls_across_all(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    import numpy as np
    from engraphis.engines import recall as recall_engine
    vec = np.ones(8, dtype=np.float32)
    mem_store.upsert_memory(namespace="ns1", document_id="a", title="t", content="alpha",
                            vector=vec)
    mem_store.upsert_memory(namespace="ns2", document_id="b", title="t", content="beta",
                            vector=vec)
    # namespace=None must recall across ALL namespaces, not a nonexistent '_global' (which
    # made the consciousness loop's thought synthesis silently no-op).
    out = recall_engine.recall_master(namespace=None, max_chunks=10)
    assert out["count"] >= 2


def test_interactions_reinforce_matching_memories(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    mem_store.upsert_memory(namespace="ns", document_id="d1", title="About Alice",
                            content="Alice ships the release")
    mem_store.upsert_memory(namespace="ns", document_id="d2", title="About Bob",
                            content="Bob reviews code")
    before = get_conn().execute(
        "SELECT stability FROM memories WHERE document_id='d1'").fetchone()["stability"]
    n = reweight.boost_entity_memories("ns", "Alice", "engage")
    assert n == 1                                   # only the Alice memory matched
    after = get_conn().execute(
        "SELECT stability FROM memories WHERE document_id='d1'").fetchone()["stability"]
    assert after > before                           # it was actually reinforced
    # Bob's memory is untouched.
    bob = get_conn().execute(
        "SELECT stability FROM memories WHERE document_id='d2'").fetchone()["stability"]
    assert bob == 1.0


# ── app-level route regressions ────────────────────────────────────────────────
pytest.importorskip("fastapi", reason="full-stack extra not installed")
from fastapi.testclient import TestClient  # noqa: E402


def _client(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "embed_model", "")
    from engraphis.app import create_legacy_reference_app
    return TestClient(create_legacy_reference_app(legacy_db_path=tmp_path / "mem-v1.db"))


def test_prune_honors_explicit_zero_threshold(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        # A memory last accessed long ago has near-zero retention.
        mem_store.upsert_memory(namespace="ns", document_id="old", title="t",
                                content="stale", created_at=now_ts() - 100 * 86400)
        get_conn().execute(
            "UPDATE memories SET last_access=?, stability=0.5 WHERE document_id='old'",
            (now_ts() - 100 * 86400,))
        get_conn().commit()
        # threshold 0.0 => delete only retention < 0 => nothing (old code coerced to 0.05).
        r = c.post("/memory/prune",
                   json={"namespace": "ns", "minRetention": 0.0, "dryRun": True})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data.get("candidates", data.get("wouldDelete", 0)) == 0 or \
            data.get("count", 0) == 0


def test_conversations_missing_content_is_400_not_500(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/memory/conversations", json={"messages": [{"role": "user"}]})
        assert r.status_code == 400


def test_query_context_does_not_echo_llm_exception_text(monkeypatch, tmp_path):
    secret = "https://provider.example/?api_key=do-not-return-this"

    class _FailingLLM:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def chat_with_context(self, **kwargs):
            raise RuntimeError(secret)

    monkeypatch.setattr("engraphis.routes.memory.LLMClient", _FailingLLM)
    monkeypatch.setattr(
        "engraphis.routes.memory.recall_engine.recall",
        lambda **kwargs: {"llmContextMessage": "", "count": 0, "chunks": []},
    )
    with _client(monkeypatch, tmp_path) as c:
        response = c.post("/memory/queries", json={"query": "what is stored?"})

    assert response.status_code == 200
    result = response.json()["data"]
    assert result["llm_error"] == "LLM service unavailable"
    assert secret not in repr(result)


@pytest.mark.parametrize(
    ("path", "extra_form"),
    [
        ("/memory/vaults/upload-folder", {}),
        (
            "/memory/vaults/upload-folder-smart",
            {"auto_categorize_flag": "false"},
        ),
    ],
)
def test_upload_batch_limit_remains_an_http_413(
    monkeypatch,
    tmp_path,
    path,
    extra_form,
):
    from engraphis.routes import vault as vault_routes

    monkeypatch.setattr(vault_routes, "MAX_IMPORT_RESOURCE_BYTES", 4)
    monkeypatch.setattr(vault_routes, "MAX_IMPORT_TOTAL_BYTES", 1)
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            path,
            data={"namespace": "ns", "memory_type": "semantic", **extra_form},
            files={"files": ("memory.md", b"xy", "text/markdown")},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == {"error": "upload batch exceeds 1 bytes"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/workspaces/import-files",
        "/api/workspaces/import-files/",
        "/memory/vaults/upload-folder",
        "/memory/vaults/upload-folder/",
        "/memory/vaults/upload-folder-smart",
        "/memory/vaults/upload-folder-smart/",
    ],
)
def test_vault_upload_rejects_declared_oversize_before_multipart_parse(
    monkeypatch,
    tmp_path,
    path,
):
    import engraphis.app as app_module

    monkeypatch.setattr(app_module, "VAULT_UPLOAD_REQUEST_BYTES", 16)
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            path,
            content=b"not parsed",
            headers={
                "content-type": "multipart/form-data; boundary=unused",
                "content-length": "17",
            },
        )

    assert response.status_code == 413
    assert response.json() == {
        "error": "request body too large",
        "max_bytes": 16,
    }


def test_vault_upload_counts_chunked_body_without_content_length():
    import asyncio
    import json

    from engraphis.app import _VaultUploadLimitMiddleware

    messages = iter([
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"67890", "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def drain_body(_scope, receive_body, send_response):
        while True:
            message = await receive_body()
            if not message.get("more_body"):
                break
        await send_response({
            "type": "http.response.start",
            "status": 204,
            "headers": [],
        })
        await send_response({"type": "http.response.body", "body": b""})

    middleware = _VaultUploadLimitMiddleware(drain_body, max_bytes=8)
    asyncio.run(middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/memory/vaults/upload-folder",
            "headers": [(b"transfer-encoding", b"chunked")],
        },
        receive,
        send,
    ))

    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["max_bytes"] == 8


def test_vault_upload_limits_valid_streamed_multipart_in_whole_app(
    monkeypatch,
    tmp_path,
):
    import asyncio
    import json

    import engraphis.app as app_module

    _setup_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(app_module, "VAULT_UPLOAD_REQUEST_BYTES", 96)
    body = (
        b"--vault\r\n"
        b'Content-Disposition: form-data; name="namespace"\r\n\r\n'
        b"ns\r\n"
        b"--vault\r\n"
        b'Content-Disposition: form-data; name="files"; filename="memory.md"\r\n'
        b"Content-Type: text/markdown\r\n\r\n"
        + b"x" * 64
        + b"\r\n--vault--\r\n"
    )
    chunks = [
        {
            "type": "http.request",
            "body": body[offset:offset + 32],
            "more_body": offset + 32 < len(body),
        }
        for offset in range(0, len(body), 32)
    ]
    messages = iter(chunks)
    sent = []

    async def receive():
        return next(messages, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    app = app_module.create_legacy_reference_app(
        legacy_db_path=tmp_path / "streamed-upload-v1.db"
    )
    asyncio.run(app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/memory/vaults/upload-folder",
            "raw_path": b"/memory/vaults/upload-folder",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=vault"),
                (b"transfer-encoding", b"chunked"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
        send,
    ))

    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert response_start["status"] == 413
    assert json.loads(response_body)["max_bytes"] == 96


@pytest.mark.parametrize(
    ("path", "extra_form"),
    [
        ("/memory/vaults/upload-folder", {}),
        (
            "/memory/vaults/upload-folder-smart",
            {"auto_categorize_flag": "false"},
        ),
    ],
)
def test_vault_upload_stops_multipart_parse_at_file_count_limit(
    monkeypatch,
    tmp_path,
    path,
    extra_form,
):
    from engraphis.routes import vault as vault_routes

    monkeypatch.setattr(vault_routes, "MAX_IMPORT_FILES", 2)
    files = [
        ("files", (f"memory-{index}.md", b"# Memory", "text/markdown"))
        for index in range(3)
    ]
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            path,
            data={"namespace": "ns", "memory_type": "semantic", **extra_form},
            files=files,
        )

    assert response.status_code == 413
    assert response.json()["detail"] == {"error": "too many files (max 2)"}
    assert vault_routes.vault_store.get_vault("ns") is None


def test_vault_upload_instantiates_multipart_parser_with_bounded_file_cap(
    monkeypatch,
    tmp_path,
):
    """The route handler, not a post-parse endpoint check, owns the parser cap."""
    import starlette.requests

    from engraphis.routes import vault as vault_routes

    monkeypatch.setattr(vault_routes, "MAX_IMPORT_FILES", 2)
    parser_calls = []
    parser_type = starlette.requests.MultiPartParser

    class RecordingMultiPartParser(parser_type):
        def __init__(self, headers, stream, *, max_files=1000, max_fields=1000,
                     max_part_size=1024 * 1024):
            parser_calls.append((max_files, max_fields))
            super().__init__(
                headers,
                stream,
                max_files=max_files,
                max_fields=max_fields,
                max_part_size=max_part_size,
            )

    monkeypatch.setattr(starlette.requests, "MultiPartParser", RecordingMultiPartParser)
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/memory/vaults/upload-folder",
            data={"namespace": "ns", "memory_type": "semantic"},
            files=[
                ("files", (f"memory-{index}.md", b"# Memory", "text/markdown"))
                for index in range(3)
            ],
        )

    assert response.status_code == 413
    assert parser_calls == [(2, vault_routes._UPLOAD_FORM_FIELDS)]


def test_duplicate_candidate_query_uses_indexed_ordering_without_temp_sort(monkeypatch, tmp_path):
    from engraphis.routes import vault as vault_routes

    _setup_store(monkeypatch, tmp_path)
    vector = np.ones(8, dtype=np.float32)
    mem_store.upsert_memory(
        namespace="first", document_id="older", title="t", content="c", vector=vector,
        updated_at=10,
    )
    mem_store.upsert_memory(
        namespace="first", document_id="newer", title="t", content="c", vector=vector,
        updated_at=20,
    )
    mem_store.upsert_memory(
        namespace="second", document_id="latest-id", title="t", content="c", vector=vector,
        updated_at=5,
    )

    conn = get_conn()
    scoped_sql, scoped_params = vault_routes._duplicate_candidate_query("first")
    global_sql, global_params = vault_routes._duplicate_candidate_query(None)
    scoped_plan = " ".join(
        row["detail"] for row in conn.execute(
            "EXPLAIN QUERY PLAN " + scoped_sql + " LIMIT ?", (*scoped_params, 10)
        )
    )
    global_plan = " ".join(
        row["detail"] for row in conn.execute(
            "EXPLAIN QUERY PLAN " + global_sql + " LIMIT ?", (*global_params, 10)
        )
    )

    assert "idx_mem_updated" in scoped_plan
    assert "TEMP B-TREE" not in scoped_plan.upper()
    assert "TEMP B-TREE" not in global_plan.upper()
    assert [row["document_id"] for row in conn.execute(scoped_sql, scoped_params)] == [
        "newer", "older"
    ]
    assert conn.execute(global_sql, global_params).fetchone()["document_id"] == "latest-id"


def test_duplicate_health_bounds_candidates_results_and_uses_worker(
    monkeypatch,
    tmp_path,
):
    import numpy as np

    from engraphis.routes import vault as vault_routes

    _setup_store(monkeypatch, tmp_path)
    vector = np.ones(8, dtype=np.float32) / np.sqrt(8)
    for index in range(4):
        mem_store.upsert_memory(
            namespace="ns",
            document_id=f"d{index}",
            title=f"Memory {index}",
            content=f"duplicate content {index}",
            vector=vector,
        )

    monkeypatch.setattr(vault_routes, "_DUPLICATE_CANDIDATE_LIMIT", 3)
    monkeypatch.setattr(vault_routes, "_DUPLICATE_RESULT_LIMIT", 2)
    monkeypatch.setattr(
        vault_routes.mem_store,
        "all_vectors",
        lambda **_kwargs: pytest.fail("route must not load every vector"),
    )
    worker_calls = []
    real_worker = vault_routes.asyncio.to_thread

    async def tracked_worker(function, *args):
        worker_calls.append(function)
        return await real_worker(function, *args)

    monkeypatch.setattr(vault_routes.asyncio, "to_thread", tracked_worker)
    # The records above intentionally live in the v1 reference database.  Rebind
    # through the explicit factory only after presenting a distinct v2 database path.
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "current-v2.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "embed_model", "")
    from engraphis.app import create_legacy_reference_app

    with TestClient(create_legacy_reference_app(legacy_db_path=tmp_path / "mem.db")) as client:
        response = client.get("/memory/health/duplicates?namespace=ns")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["candidate_count"] == 3
    assert data["candidate_limit"] == 3
    assert data["matches_considered"] == 3
    assert data["count"] == 2
    assert len(data["duplicates"]) == 2
    assert data["result_limit"] == 2
    assert data["truncated"] is True
    assert worker_calls == [vault_routes._duplicate_pairs]
