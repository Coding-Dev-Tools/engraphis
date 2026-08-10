import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")

from fastapi.testclient import TestClient

from engraphis.config import settings
from engraphis.read_only_api import MAX_READ_ONLY_BODY_BYTES, create_read_only_app
from engraphis.service import MemoryService
from engraphis.backends.graph_extractor import RegexGraphExtractor


def test_read_only_factory_forwards_configured_vector_backend(monkeypatch):
    captured = {}
    sentinel = object()

    def create(*args, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(MemoryService, "create", create)
    monkeypatch.setattr(settings, "vector_backend", "auto")
    monkeypatch.setattr(settings, "embed_dim", 768)

    create_read_only_app()

    assert captured["vector_backend"] == "auto"
    assert captured["embed_dim"] == 768
    assert captured["read_only"] is True


def test_read_only_api_requires_token_and_does_not_reinforce():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    pending = svc.remember("The database is SQLite.", workspace="w", scope="workspace")
    memory = svc.engine.approve_for_prompt(
        pending["id"], reviewer="test", reason="approved fixture"
    )
    before = svc.store.get_memory(memory["id"]).access_count
    receipts_before = svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_receipts"
    ).fetchone()["n"]
    client = TestClient(create_read_only_app(svc, token="secret"))
    assert client.get("/recall", params={"query": "database", "workspace": "w"}).status_code == 401
    # Receipt-derived savings are still scoped usage information, so the new
    # endpoint must stay behind the same bearer gate as recall.
    assert client.get("/context-savings", params={"workspace": "w"}).status_code == 401
    response = client.get(
        "/recall",
        params={"query": "database", "workspace": "w", "candidate_depth": "adaptive"},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200 and response.json()["count"] == 1
    assert response.json()["candidate_depth"] == "adaptive"
    assert response.json()["candidate_k_used"] >= response.json()["candidate_k_requested"]
    lowercase = client.get(
        "/recall", params={"query": "database", "workspace": "w"},
        headers={"Authorization": "bearer secret"},
    )
    assert lowercase.status_code == 200
    savings = client.get(
        "/context-savings", params={"workspace": "w"},
        headers={"Authorization": "Bearer secret"},
    )
    assert savings.status_code == 200
    assert savings.json()["format"] == "engraphis-context-savings/1"
    assert response.headers["x-frame-options"] == "DENY"
    assert svc.store.get_memory(memory["id"]).access_count == before
    assert svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_receipts"
    ).fetchone()["n"] == receipts_before
    assert client.post(
        "/remember", json={}, headers={"Authorization": "Bearer secret"}
    ).status_code == 404


def test_tokenless_read_only_factory_rejects_remote_peers():
    """The ASGI factory must retain the launcher's token-or-loopback boundary."""
    svc = MemoryService.create(":memory:", graph_extractor="none")
    client = TestClient(
        create_read_only_app(svc),
        client=("192.0.2.10", 50000),
    )

    # Health/schema probes remain safe for orchestration and discovery, but workspace
    # reads fail closed even if an operator bypasses scripts.graph_server.
    assert client.get("/health").status_code == 200
    response = client.get("/recall", params={"query": "database", "workspace": "w"})
    assert response.status_code == 403
    assert response.json() == {"detail": "remote access requires a bearer token"}


def test_read_only_api_serves_graph_and_intent_recall():
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    pending = svc.remember(
        "Alice Johnson works at Acme Corporation.",
        workspace="w", scope="workspace",
    )
    svc.engine.approve_for_prompt(pending["id"], reviewer="test", reason="approved fixture")
    client = TestClient(create_read_only_app(svc))
    omitted = client.get("/graph", params={"workspace": "w"}).json()
    assert omitted["nodes"] and omitted["edges"]
    assert client.get("/graph?workspace=w&layers=").json()["edges"] == []
    response = client.post(
        "/intent/recall",
        json={
            "query": "Alice", "intent": "explain", "workspace": "w",
            "candidate_depth": "adaptive",
        },
    )
    assert response.status_code == 200
    assert response.json()["operation"] == "recall"
    assert response.json()["candidate_depth"] == "adaptive"


@pytest.mark.parametrize("invalid_limit", [True, "2"])
def test_read_only_intent_recall_rejects_coerced_memory_type_limits(invalid_limit):
    svc = MemoryService.create(":memory:", graph_extractor="none")
    response = TestClient(create_read_only_app(svc)).post(
        "/intent/recall",
        json={"query": "anything", "mtype_limits": {"semantic": invalid_limit}},
    )

    assert response.status_code == 422


def test_read_only_api_serves_content_free_context_savings():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    pending = svc.remember("Context savings test.", workspace="w", scope="workspace")
    svc.engine.approve_for_prompt(pending["id"], reviewer="test", reason="approved fixture")
    svc.recall("context savings", workspace="w", token_budget=64)

    response = TestClient(create_read_only_app(svc)).get(
        "/context-savings",
        params={"workspace": "w", "from_ts": 0, "to_ts": 9_999_999_999},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "engraphis-context-savings/1"
    assert body["period"] == {"from_ts": 0, "to_ts": 9_999_999_999}
    assert body["savings_receipt_count"] == 1
    assert body["by_token_counter"][0]["source_tokens"] >= body["by_token_counter"][0]["saved_tokens"]


def test_read_only_code_search_forwards_bitemporal_anchors():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Code search anchor.", workspace="w", repo="repo")
    observed = {}
    original = svc.search_code

    def observe(*args, **kwargs):
        observed.update(kwargs)
        return original(*args, **kwargs)

    svc.search_code = observe
    response = TestClient(create_read_only_app(svc)).get(
        "/code/search",
        params={
            "query": "missing", "workspace": "w", "repo": "repo",
            "as_of": 10.0, "valid_at": 10.0, "known_at": 20.0,
        },
    )

    assert response.status_code == 200
    assert observed["as_of"] == observed["valid_at"] == 10.0
    assert observed["known_at"] == 20.0


@pytest.mark.parametrize("path", ["/graph", "/code/export"])
def test_read_only_graph_surfaces_forward_bitemporal_anchors(path):
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Temporal adapter anchor.", workspace="w", repo="repo")
    observed = {}
    method_name = "graph" if path == "/graph" else "export_code_graph"
    original = getattr(svc, method_name)

    def observe(*args, **kwargs):
        observed.update(kwargs)
        return original(*args, **kwargs)

    setattr(svc, method_name, observe)
    response = TestClient(create_read_only_app(svc)).get(
        path,
        params={
            "workspace": "w", "repo": "repo",
            "as_of": 10.0, "valid_at": 10.0, "known_at": 20.0,
        },
    )

    assert response.status_code == 200
    assert observed["as_of"] == observed["valid_at"] == 10.0
    assert observed["known_at"] == 20.0


def test_read_only_graph_does_not_lazy_backfill():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember(
        "Alice Johnson works at Acme Corporation.",
        workspace="w", scope="workspace",
    )
    svc.engine.graph_extractor = RegexGraphExtractor()
    before = svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM entities"
    ).fetchone()["n"]

    response = TestClient(create_read_only_app(svc)).get("/graph", params={"workspace": "w"})

    assert response.status_code == 200
    assert response.json()["nodes"] == []
    assert svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM entities"
    ).fetchone()["n"] == before


def test_factory_owned_read_only_service_is_immutable_and_closed(monkeypatch, tmp_path):
    db_path = tmp_path / "readonly.db"
    writable = MemoryService.create(str(db_path), embed_model="", graph_extractor="none")
    writable.remember("Immutable inspector fixture.", workspace="w")
    writable.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    writable.close()
    watched = [db_path, tmp_path / "readonly.db-wal", tmp_path / "readonly.db-shm"]
    before = {
        path.name: path.read_bytes() if path.exists() else None
        for path in watched
    }

    monkeypatch.setattr(settings, "db_path", str(db_path))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "vector_backend", "numpy")
    monkeypatch.setattr(settings, "extractor", "none")
    app = create_read_only_app()

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get(
            "/recall", params={"query": "immutable", "workspace": "w"}
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1

    after = {
        path.name: path.read_bytes() if path.exists() else None
        for path in watched
    }
    assert after == before
    assert app.state.service._closed is True


def test_injected_read_only_service_remains_caller_owned():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    app = create_read_only_app(svc)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert svc._closed is False
    assert svc.store.conn.execute("SELECT 1").fetchone()[0] == 1
    svc.close()


def test_read_only_code_routes_validate_and_forward_capacity():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    observed = {}

    def code_path(*args, **kwargs):
        observed["path"] = kwargs["capacity"]
        return {"capacity": kwargs["capacity"], "truncated": True}

    def code_impact(*args, **kwargs):
        observed["impact"] = kwargs["capacity"]
        return {"capacity": kwargs["capacity"], "truncated": True}

    def code_export(**kwargs):
        observed["export"] = kwargs["capacity"]
        return {"graph": {"limit": kwargs["capacity"], "truncated": True}}

    svc.code_path = code_path
    svc.code_impact = code_impact
    svc.export_code_graph = code_export
    client = TestClient(create_read_only_app(svc))

    path = client.post("/code/path", json={
        "workspace": "w", "repo": "r", "source": "a", "target": "b",
        "capacity": 321,
    })
    impact = client.post("/code/impact", json={
        "workspace": "w", "repo": "r", "changed_files": ["a.py"],
        "capacity": 654,
    })
    exported = client.get(
        "/code/export",
        params={"workspace": "w", "repo": "r", "capacity": 987},
    )
    invalid = client.post("/code/path", json={
        "workspace": "w", "repo": "r", "source": "a", "target": "b",
        "capacity": 50_001,
    })

    assert path.json() == {"capacity": 321, "truncated": True}
    assert impact.json() == {"capacity": 654, "truncated": True}
    assert exported.json()["graph"] == {"limit": 987, "truncated": True}
    assert observed == {"path": 321, "impact": 654, "export": 987}
    assert invalid.status_code == 422
    svc.close()


def test_read_only_unhandled_error_is_json_and_redacted(caplog):
    secret = "https://provider.invalid/?token=do-not-return-or-log"

    class _ExplodingService:
        def recall(self, *args, **kwargs):
            raise RuntimeError(secret)

    app = create_read_only_app(_ExplodingService())
    with caplog.at_level("ERROR", logger="engraphis.read_only"):
        response = TestClient(app).get(
            "/recall", params={"query": "trigger", "workspace": "w"}
        )

    assert response.status_code == 500
    assert response.json() == {"error": "internal server error"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert secret not in response.text
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_read_only_api_rejects_declared_oversized_body_with_fixed_detail():
    app = create_read_only_app(object())
    response = TestClient(app).post(
        "/intent/recall",
        content=b"{}",
        headers={"content-length": str(MAX_READ_ONLY_BODY_BYTES + 1)},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


def test_read_only_api_rejects_streamed_oversized_body_with_413():
    # Chunked/streamed requests carry no Content-Length, so the middleware must
    # translate the over-limit receive itself instead of relying on the declared
    # length check or a ValueError that FastAPI's parser swallows as a 400.
    app = create_read_only_app(object())
    response = TestClient(app).post(
        "/intent/recall",
        content=b"x" * (MAX_READ_ONLY_BODY_BYTES + 1),
        headers={"transfer-encoding": "chunked"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}
