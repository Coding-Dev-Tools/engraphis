"""Release regressions for source access, connector ownership, and topic fallback."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from engraphis import factory
from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.spec_crawl import analyze_memory_nodes
from engraphis.service import MemoryService, ValidationError


@pytest.mark.parametrize("relative", [
    "secrets.json", ".aws/policy.json", ".ssh/policy.yaml",
    ".kube/policy.yml", ".engraphis/policy.toml",
])
def test_spec_source_sensitive_paths_are_rejected_before_open(tmp_path, monkeypatch, relative):
    source = tmp_path / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# Rules\nKeep private project prose local.", encoding="utf-8")
    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", str(tmp_path))
    opened = []
    original = Path.open

    def observed_open(path, *args, **kwargs):
        if path == source:
            opened.append(path)
        return original(path, *args, **kwargs)

    service = MemoryService.create(":memory:")
    try:
        monkeypatch.setattr(Path, "open", observed_open)
        with pytest.raises(ValidationError, match="sensitive"):
            service.spec_crawl(source_id=str(source), trace_claims=False)
        assert opened == []
    finally:
        service.close()


@pytest.mark.parametrize("sensitive_link", [False, True])
def test_spec_source_checks_selected_and_resolved_symlink_paths(tmp_path, monkeypatch, sensitive_link):
    private = tmp_path / ".aws" / "policy.json"
    public = tmp_path / "policy.json"
    private.parent.mkdir()
    target, link = (public, private) if sensitive_link else (private, public)
    target.write_text("# Rules\nA synthetic private policy.", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", str(tmp_path))
    service = MemoryService.create(":memory:")
    try:
        with pytest.raises(ValidationError, match="sensitive"):
            service.spec_crawl(source_id=str(link), trace_claims=False)
    finally:
        service.close()


@pytest.mark.parametrize("left,right", [("db", "database"), ("database", "db"), ("DB", "database")])
def test_database_aliases_preserve_real_same_subject_conflicts(left, right):
    report = analyze_memory_nodes([
        {"id": "old", "subject_key": "service", "content": f"{left}: postgres", "valid_from": 1},
        {"id": "new", "subject_key": "service", "content": f"{right}: sqlite", "valid_from": 2},
    ])
    assert [node["parameters"] for node in report["nodes"]] == [
        {"database": "postgres"}, {"database": "sqlite"},
    ]
    assert any({row["node_a"], row["node_b"]} == {"old", "new"} for row in report["conflicts"])


def test_database_aliases_do_not_invent_conflicts_or_cross_subject_identity():
    for right_subject, right_value in [("service", "postgres"), ("other", "sqlite")]:
        report = analyze_memory_nodes([
            {"id": "left", "subject_key": "service", "content": "db: postgres"},
            {"id": "right", "subject_key": right_subject, "content": f"database: {right_value}"},
        ])
        assert not report["conflicts"]


class _Connector:
    def __init__(self):
        self.pragma = "synthetic-key-material"
        self.connections = []
        self.close_count = 0
        self.closed_after_connections = False

    def __call__(self, path):
        connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        connection.row_factory = sqlite3.Row
        self.connections.append(connection)
        return connection

    def close(self):
        self.close_count += 1
        closed = []
        for connection in self.connections:
            try:
                connection.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                closed.append(True)
            else:
                closed.append(False)
        self.closed_after_connections = all(closed)
        self.pragma = ""


def test_factory_owned_connector_closes_after_store_exactly_once(monkeypatch):
    connector = _Connector()
    monkeypatch.setattr("engraphis.backends.encrypted_db.connector_from_env", lambda: connector)
    engine = factory.create_memory_engine()
    engine.close()
    engine.close()
    assert connector.close_count == 1
    assert connector.closed_after_connections
    assert connector.pragma == ""


@pytest.mark.parametrize("stage", ["store", "embedder", "health"])
def test_factory_releases_owned_connector_on_construction_failure(monkeypatch, stage):
    connector = _Connector()
    monkeypatch.setattr("engraphis.backends.encrypted_db.connector_from_env", lambda: connector)

    def fail(*args, **kwargs):
        raise ValueError("synthetic construction failure")

    kwargs = {}
    if stage == "store":
        kwargs["sqlite_durability"] = "invalid"
    else:
        monkeypatch.setattr(factory, "get_embedder" if stage == "embedder" else "backend_health", fail)
    with pytest.raises(ValueError):
        factory.create_memory_engine(**kwargs)
    assert connector.close_count == 1
    assert connector.closed_after_connections
    assert connector.pragma == ""


@pytest.mark.parametrize("failure", [False, True])
def test_factory_never_owns_a_caller_supplied_connector(monkeypatch, failure):
    connector = _Connector()
    if failure:
        with pytest.raises(ValueError):
            factory.create_memory_engine(connect=connector, sqlite_durability="invalid")
    else:
        factory.create_memory_engine(connect=connector).close()
    assert connector.close_count == 0
    assert connector.pragma == "synthetic-key-material"


@pytest.mark.parametrize("query,content", [("AI", "AI policy"), ("DB", "DB policy"), ("%", "20% policy"), ("_", "a_b policy")])
def test_closed_timeline_fallback_matches_short_and_literal_topics(monkeypatch, query, content):
    pytest.importorskip("fastapi")
    from engraphis.routes import v2_api

    service = MemoryService.create(":memory:")
    try:
        workspace = service.store.get_or_create_workspace("history")
        ids = []
        for text in [content, "unrelated garden bulletin"]:
            ids.append(service.store.add_memory(MemoryRecord(
                id="", content=text, workspace_id=workspace, scope=Scope.WORKSPACE,
                valid_from=1.0, valid_to=2.0, valid_to_recorded_at=3.0, ingested_at=1.0,
                provenance={"trusted": True, "review_state": "approved"},
            )))

        def unavailable(*args, **kwargs):
            raise ValueError("shapes 256 and 384 not aligned")

        monkeypatch.setattr(service, "timeline", unavailable)
        monkeypatch.setattr(v2_api, "service", lambda: service)
        result = v2_api.timeline(q=query, workspace="history", limit=20, closed_only=True)
        assert result["mode"] == "keyword"
        assert [row["id"] for row in result["history"]] == ids[:1]
        assert v2_api._keyword_search("history", "   ", include_invalid=True, closed_only=True) == []
    finally:
        service.close()
