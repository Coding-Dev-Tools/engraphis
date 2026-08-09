"""Regression coverage for the dashboard's process-wide v2 service binding."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")

from engraphis.routes import v2_api  # noqa: E402


class _Store:
    def __init__(self, error: Optional[Exception] = None) -> None:
        self.error = error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.error is not None:
            raise self.error


class _Service:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def close(self) -> None:
        self.store.close()


def test_service_binding_keeps_the_prior_service_when_close_fails(monkeypatch) -> None:
    prior_store = _Store(OSError("database handle is busy"))
    prior = _Service(prior_store)
    replacement = _Service(_Store())
    monkeypatch.setattr(v2_api, "_service", prior)

    with pytest.raises(RuntimeError, match="prior memory service could not be closed"):
        v2_api.set_service(replacement)

    assert prior_store.close_calls == 1
    assert v2_api._service is prior


def test_service_binding_closes_before_clearing(monkeypatch) -> None:
    prior_store = _Store()
    prior = _Service(prior_store)
    monkeypatch.setattr(v2_api, "_service", prior)

    v2_api.set_service(None)

    assert prior_store.close_calls == 1
    assert v2_api._service is None


def test_rebinding_the_same_service_is_a_noop(monkeypatch) -> None:
    store = _Store()
    bound = _Service(store)
    monkeypatch.setattr(v2_api, "_service", bound)

    v2_api.set_service(bound)

    assert store.close_calls == 0
    assert v2_api._service is bound


def test_releasing_an_old_app_service_does_not_clear_a_new_binding(monkeypatch) -> None:
    old_store = _Store()
    current_store = _Store()
    old = _Service(old_store)
    current = _Service(current_store)
    monkeypatch.setattr(v2_api, "_service", current)

    v2_api.release_service(old)

    assert old_store.close_calls == 1
    assert current_store.close_calls == 0
    assert v2_api._service is current


def test_code_routes_forward_explicit_capacity(monkeypatch) -> None:
    calls = {}

    class _CodeService:
        def code_path(self, *args, **kwargs):
            calls["path"] = kwargs
            return {"capacity": kwargs["capacity"], "truncated": True}

        def code_impact(self, *args, **kwargs):
            calls["impact"] = kwargs
            return {"capacity": kwargs["capacity"], "truncated": True}

        def export_code_graph(self, **kwargs):
            calls["export"] = kwargs
            return {"graph": {"limit": kwargs["capacity"], "truncated": True}}

    monkeypatch.setattr(v2_api, "_service", _CodeService())

    path = v2_api.code_path(v2_api._CodePathReq(
        workspace="acme", repo="repo", source="a", target="b", capacity=321,
    ))
    impact = v2_api.code_impact(v2_api._CodeImpactReq(
        workspace="acme", repo="repo", changed_files=["a.py"], capacity=654,
    ))
    exported = v2_api.code_export("acme", "repo", capacity=987)

    assert path == {"capacity": 321, "truncated": True}
    assert impact == {"capacity": 654, "truncated": True}
    assert exported["graph"] == {"limit": 987, "truncated": True}
    assert calls["path"]["capacity"] == 321
    assert calls["impact"]["capacity"] == 654
    assert calls["export"]["capacity"] == 987


def test_automation_get_does_not_bootstrap_or_write(monkeypatch) -> None:
    class _AutomationService:
        @staticmethod
        def _clean_ws(workspace):
            return workspace

        @staticmethod
        def _lookup_workspace(workspace):
            assert workspace == "acme"
            return "ws_1"

    class _Cloud:
        organization_id = "org_1"

        @staticmethod
        def get_policy(workspace_id):
            assert workspace_id == "ws_1"
            return {"version": 0}

        @staticmethod
        def list_jobs(workspace_id, *, limit):
            assert (workspace_id, limit) == ("ws_1", 10)
            return {"jobs": []}

    class _CloudFactory:
        @staticmethod
        def from_environment(workspace_id):
            assert workspace_id == "ws_1"
            return _Cloud()

    from engraphis import cloud_features

    monkeypatch.setattr(v2_api, "_service", _AutomationService())
    monkeypatch.setattr(cloud_features, "CloudFeatureClient", _CloudFactory)
    monkeypatch.setattr(v2_api, "_managed_call", lambda fn, *args, **kwargs: fn(*args, **kwargs))

    result = v2_api.automation_get("acme")

    assert result["bootstrap_required"] is True
    assert result["version"] == 0


def test_automation_bootstrap_is_explicit_and_resumable(monkeypatch) -> None:
    calls = {"snapshot": 0, "upload": 0, "policy": 0}
    phase = {"value": ""}

    class _AutomationService:
        @staticmethod
        def _clean_ws(workspace):
            return workspace

        @staticmethod
        def _lookup_workspace(workspace):
            assert workspace == "acme"
            return "ws_1"

    class _Cloud:
        organization_id = "org_1"

        @staticmethod
        def get_policy(workspace_id):
            assert workspace_id == "ws_1"
            return {"version": 0}

        @staticmethod
        def list_jobs(workspace_id, *, limit):
            return {"jobs": []}

        @staticmethod
        def upload_snapshot(workspace_id, snapshot):
            calls["upload"] += 1
            assert workspace_id == "ws_1"
            return {"generation": snapshot["generation"]}

        @staticmethod
        def save_policy(workspace_id, policy):
            calls["policy"] += 1
            assert workspace_id == "ws_1"
            return {**policy, "version": 1}

    class _CloudFactory:
        @staticmethod
        def from_environment(workspace_id):
            return _Cloud()

    def build_snapshot(service, workspace):
        calls["snapshot"] += 1
        assert workspace == "acme"
        return "ws_1", {"generation": 7}

    def save_phase(service, organization_id, workspace_id, value, **kwargs):
        phase["value"] = value

    from engraphis import cloud_features

    monkeypatch.setattr(v2_api, "_service", _AutomationService())
    monkeypatch.setattr(v2_api, "_AUTOMATION_BOOTSTRAP_LOCKS", {})
    monkeypatch.setattr(v2_api, "_managed_call", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(cloud_features, "CloudFeatureClient", _CloudFactory)
    monkeypatch.setattr(cloud_features, "build_managed_snapshot", build_snapshot)
    monkeypatch.setattr(
        cloud_features, "automation_bootstrap_phase",
        lambda service, organization_id, workspace_id: phase["value"],
    )
    monkeypatch.setattr(cloud_features, "save_automation_bootstrap_phase", save_phase)

    first = v2_api.automation_bootstrap("acme")
    second = v2_api.automation_bootstrap("acme")

    assert first["bootstrap_required"] is True
    assert second["bootstrap_required"] is True
    assert phase["value"] == "policy_saved"
    assert calls == {"snapshot": 1, "upload": 1, "policy": 1}


def test_entitlement_refresh_keeps_saved_refresh_on_its_bound_control_url(
    monkeypatch, tmp_path,
) -> None:
    from engraphis import cloud_session, hosted_client

    saved_control = "https://saved-control.example"
    hostile_control = "https://attacker.invalid"
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", raising=False)
    monkeypatch.setattr(
        cloud_session, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )
    monkeypatch.setattr(
        cloud_session, "_reachable_cloud_base_url", lambda value: value.rstrip("/")
    )
    monkeypatch.setattr(
        hosted_client, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )
    cloud_session.save_bootstrap(
        {
            "organization_id": "org_saved",
            "refresh_credential": "saved-refresh",
            "token_subject": "member",
        },
        control_url=saved_control,
        compute_url="https://saved-compute.example",
    )
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", hostile_control)
    monkeypatch.setenv(
        "ENGRAPHIS_CLOUD_COMPUTE_URL", "https://attacker-compute.invalid"
    )
    calls = []

    def refresh(control_url, credential, workspace_id, token_subject):
        calls.append((control_url, credential, workspace_id, token_subject))
        return {
            "access_token": "short-lived-access",
            "organization_id": "org_saved",
            "refresh_credential": "rotated-refresh",
            "token_subject": "member",
            "plan": "pro",
            "cloud_access_active": True,
            "cloud_features": ["automation"],
        }

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)

    assert v2_api._fetch_authoritative_entitlement() is None
    assert calls == [(saved_control, "saved-refresh", None, "member")]
    assert all(hostile_control not in str(item) for call in calls for item in call)
    assert cloud_session._load()["control_url"] == saved_control


def test_sync_summary_marks_incomplete_round_without_losing_good_counts(
    monkeypatch,
) -> None:
    from engraphis.backends import sync_relay
    from engraphis.backends import sync_folder
    from engraphis.core.sync import SyncEngine
    from engraphis.service import MemoryService

    service = MemoryService.create(":memory:", graph_extractor="none")
    service.store.get_or_create_workspace("acme")
    monkeypatch.setattr(sync_relay, "has_sync_token", lambda: True)
    monkeypatch.setattr(sync_relay, "sync_read_only", lambda: False)
    monkeypatch.setattr(sync_folder, "get_transport", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        SyncEngine,
        "sync",
        lambda self, transport, workspace_id, **kwargs: {
            "complete": False,
            "errors": [{"error": "transport failure", "error_type": "OSError"}],
            "applied": [
                {"from_device": "dev_good", "added": 2},
                {"error": "transport failure"},
            ],
            "totals": {
                "added": 2,
                "updated": 0,
                "unchanged": 0,
                "links_added": 0,
            },
            "exported_memories": 3,
        },
    )

    try:
        summary = v2_api._sync_all(service)
    finally:
        service.close()

    assert summary["attempted"] == 1
    assert summary["succeeded"] == 0
    assert summary["exported"] == 3
    assert summary["peers"] == 1
    assert summary["added"] == 2
    assert summary["errors"] == [{
        "workspace": "acme",
        "error": "sync round incomplete",
        "failed_items": 1,
    }]


def test_sync_run_returns_ok_false_for_partial_result(monkeypatch) -> None:
    from engraphis.backends import sync_relay
    from engraphis import cloud_session

    summary = {
        "workspaces": 1,
        "attempted": 1,
        "succeeded": 0,
        "errors": [{
            "workspace": "acme",
            "error": "sync round incomplete",
            "failed_items": 1,
        }],
    }
    monkeypatch.setattr(sync_relay, "has_sync_token", lambda: True)
    monkeypatch.setattr(cloud_session, "configured", lambda **kwargs: False)
    monkeypatch.setattr(v2_api, "service", lambda: object())
    monkeypatch.setattr(v2_api, "_sync_all", lambda service: summary)
    v2_api._SYNC_STATE.clear()

    result = asyncio.run(v2_api.sync_run())

    assert result == {"ok": False, "summary": summary}
    assert v2_api._SYNC_STATE["last"] == summary


def test_multipart_import_offloads_synchronous_service_tail(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _Upload:
        filename = "note.txt"

        async def read(self, size):
            del size
            return b"bounded upload"

    class _ImportService:
        def import_files(self, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test did not release blocked import")
            return {"imported": len(kwargs["files"])}

    monkeypatch.setattr(v2_api, "service", lambda: _ImportService())

    async def exercise():
        started = time.monotonic()
        pending = asyncio.create_task(v2_api._import_uploaded_files(
            workspace="acme",
            memory_type="semantic",
            derive_facts=False,
            files=[_Upload()],
        ))
        try:
            await asyncio.sleep(0)
            assert time.monotonic() - started < 0.5
            assert await asyncio.to_thread(entered.wait, 1)
        finally:
            release.set()
        return await pending

    assert asyncio.run(exercise()) == {"imported": 1}


def test_multipart_parser_rejects_excess_files_before_service(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from engraphis.service import MAX_IMPORT_FILES

    calls = []

    class _ImportService:
        def import_files(self, **kwargs):
            calls.append(kwargs)
            return {"imported": len(kwargs["files"])}

    monkeypatch.setattr(v2_api, "service", lambda: _ImportService())
    app = FastAPI()
    app.include_router(v2_api.router)
    files = [
        ("files", (f"{index}.txt", b"x", "text/plain"))
        for index in range(MAX_IMPORT_FILES + 1)
    ]

    response = TestClient(app).post(
        "/api/workspaces/import-files",
        data={
            "workspace": "acme",
            "memory_type": "semantic",
            "derive_facts": "false",
        },
        files=files,
    )

    assert response.status_code == 400
    assert calls == []

    accepted = TestClient(app).post(
        "/api/workspaces/import-files",
        data={
            "workspace": "acme",
            "memory_type": "procedural",
            "derive_facts": "true",
        },
        files=[("files", ("runbook.txt", b"bounded", "text/plain"))],
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"imported": 1}
    assert calls == [{
        "workspace": "acme",
        "files": [{"name": "runbook.txt", "data": b"bounded"}],
        "memory_type": "procedural",
        "derive_facts": True,
    }]


def test_packaged_route_smoke_client_sends_configured_bearer(monkeypatch) -> None:
    from scripts import test_routes

    token = "local-smoke-token"
    captured = {}

    class _Response:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, path, **kwargs):
            del kwargs
            return _Response({
                "/api/health": {"engine": "v2"},
                "/api/recall": {"memories": [{"id": "mem_smoke"}]},
                "/api/memories": {"memories": [{"id": "mem_smoke"}]},
                "/api/stats": {"memories": 1},
            }[path])

        def post(self, path, **kwargs):
            del kwargs
            return _Response({
                "/api/remember": {"id": "mem_smoke"},
                "/api/forget": {"status": "forgotten"},
            }[path])

    monkeypatch.setattr(test_routes.settings, "api_token", token)
    monkeypatch.setattr(test_routes.httpx, "Client", _Client)
    test_routes.PASS = 0
    test_routes.FAIL = 0

    test_routes.run()

    assert captured["headers"] == {"Authorization": f"Bearer {token}"}


def test_merge_route_forwards_explicit_target_scope(monkeypatch) -> None:
    captured = {}

    class _MergeService:
        def merge(self, *args, **kwargs):
            captured.update(kwargs)
            return {"id": "mem_merged"}

    monkeypatch.setattr(v2_api, "_service", _MergeService())
    result = v2_api.merge(v2_api._MergeReq(
        ids=["mem_one", "mem_two"],
        content="Combined evidence.",
        workspace="acme",
        scope="workspace",
    ))

    assert result == {"id": "mem_merged"}
    assert captured["scope"] == "workspace"
