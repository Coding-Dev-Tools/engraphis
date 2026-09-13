"""User-facing status reports observed state without inventing completeness."""
import json
import time

import pytest

from engraphis.service import MemoryService


@pytest.fixture
def svc(tmp_path):
    service = MemoryService.create(str(tmp_path / "workflow.db"), extractor="none")
    yield service
    service.close()


def test_review_inbox_excludes_consumed_and_legacy_approved_sources(svc):
    source = svc.remember("Cache expires after 30 days.", workspace="w", repo="api",
                          source="web", trusted=False)["id"]
    assert svc.review_inbox(workspace="w")["count"] == 1
    successor = svc.engine.approve_for_prompt(source, reviewer="owner", reason="verified")["id"]
    assert svc.review_inbox(workspace="w")["count"] == 0
    # Simulate the pre-command approval representation in this disposable database.
    svc.store.conn.execute("DELETE FROM memory_command_sources")
    svc.store.conn.execute("DELETE FROM memory_commands")
    svc.store.conn.commit()
    assert svc.review_inbox(workspace="w")["count"] == 0
    assert svc.store.get_memory(source) is not None
    assert svc.store.get_memory(successor) is not None


def test_review_inbox_distinguishes_sample_and_scope(svc):
    for i in range(3):
        svc.remember(f"Private pending fact number {i}.", workspace="w", repo="api",
                     source="web", trusted=False, resolve_conflicts=False)
    svc.remember("Other project needs review.", workspace="w", repo="other",
                 source="web", trusted=False)
    result = svc.review_inbox(workspace="w", repo="api", limit=2)
    assert result["count"] == 2 and result["has_more"] and result["truncated"]
    assert result["count_semantics"] == "returned_sample"
    assert "Private pending" not in json.dumps(result)
    assert all(item["excerpt"] == "" for item in result["items"])


def test_content_free_diagnostics_keep_unobserved_counts_unknown(svc):
    for days in (30, 90):
        saved = svc.remember(f"Cache expires after {days} days unless recovery is active.",
                     workspace="w", repo="api", source="user", trusted=True,
                     resolve_conflicts=False)
        svc.engine.approve_for_prompt(saved["id"], reviewer="fixture owner", reason="verified")
    result = svc.recall("cache expires", workspace="w", repo="api", token_budget=0,
                        diagnostics=True)
    diagnostic = result["diagnostics"]
    assert diagnostic["schema"] == "diagnostics/1"
    assert diagnostic["counts"]["budget"] == 2
    assert diagnostic["counts"]["scope"] is None
    assert diagnostic["phase_ms"]["engine_recall"] >= 0
    assert "Cache expires" not in json.dumps(diagnostic)
    assert "mem_" not in json.dumps(diagnostic)
    answer = svc.grounded_recall("cache expires", workspace="w", repo="api", diagnostics=True)
    assert answer["answer_coverage"] == "unknown"
    assert answer["diagnostics"]["schema"] == "diagnostics/1"


def test_recall_phases_attribute_slow_backend_without_changing_results(svc, monkeypatch):
    svc.remember("Atlas uses SQLite for durable local memory.", workspace="w")
    baseline = svc.recall("Atlas SQLite", workspace="w", reinforce=False)
    search = svc.store.fts_search

    def delayed(*args, **kwargs):
        time.sleep(0.02)
        return search(*args, **kwargs)

    monkeypatch.setattr(svc.store, "fts_search", delayed)
    measured = svc.recall("Atlas SQLite", workspace="w", reinforce=False, diagnostics=True)
    assert measured["context"] == baseline["context"]
    phases = measured["diagnostics"]["phase_ms"]
    assert phases["lexical_search"] >= 15
    assert phases["packing"] >= 0
    assert abs(sum(value for key, value in phases.items() if key != "engine_recall")
               - phases["engine_recall"]) < 1
    assert "diagnostics" not in baseline
    assert "Atlas" not in json.dumps(measured["diagnostics"])


def test_empty_recall_reports_only_executed_phases(svc):
    svc.remember("Atlas durable memory.", workspace="w")
    measured = svc.recall("unknown", workspace="w", mtypes=["working"], diagnostics=True)
    phases = measured["diagnostics"]["phase_ms"]
    assert phases["packing"] >= 0
    assert "fusion_scoring" not in phases
    assert measured["count"] == 0


def test_build_and_review_routes_do_not_expose_secrets(svc, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    svc.remember("Project uses SQLite.", workspace="w")
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client:
        build = client.get("/api/build")
        assert build.status_code == 200
        value = build.json()
        assert value["database_schema_version"] == 18
        assert value["contracts"]["listing"] == "cursor/2"
        assert value["readers"]["independent_browsing"] is True
        assert len(value["package_source_sha256"]) == 64
        assert svc.store.path not in build.text
        inbox = client.get("/api/review-inbox", params={"workspace": "w"})
        assert inbox.status_code == 200 and inbox.json()["count"] == 0


@pytest.mark.parametrize("error,status,code", [
    ("ReadSnapshotBusy", 503, "read_busy"), ("ReadSnapshotTimeout", 504, "read_timeout"),
])
def test_reader_deadlines_have_safe_retryable_errors(error, status, code):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from engraphis.core import read_snapshots
    from engraphis.routes.v2_api import _run
    def fail():
        raise getattr(read_snapshots, error)("private database path must not escape")
    with pytest.raises(HTTPException) as caught:
        _run(fail)
    assert caught.value.status_code == status
    assert caught.value.detail["code"] == code
    assert caught.value.detail["retryable"] is True
    assert "private" not in json.dumps(caught.value.detail)
