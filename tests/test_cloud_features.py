from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import urllib.error
import urllib.request

import pytest

import engraphis.cloud_features as cloud_features
from engraphis.cloud_features import (
    CloudFeatureClient,
    CloudFeatureError,
    build_managed_snapshot,
    run_managed_job,
)
from engraphis.service import MemoryService, set_current_user


def _service() -> MemoryService:
    service = MemoryService.create(":memory:")
    service.remember(
        "A normal managed-compute memory.",
        workspace="acme",
        metadata={"subject": "  Queue   design  ", "api_key": "metadata-secret"},
    )
    secret = service.remember("password=do-not-upload", workspace="acme")
    service.store.conn.execute(
        "UPDATE memories SET sensitivity='secret' WHERE id=?",
        (secret["id"],),
    )
    service.store.conn.commit()
    return service


def test_snapshot_is_enabled_without_a_user_opt_in() -> None:
    _, snapshot = build_managed_snapshot(_service(), "acme", consent=False)

    assert cloud_features.managed_compute_consent() is True
    assert snapshot["managed_compute_consent"] is True


def test_snapshot_excludes_secret_rows_before_serialization() -> None:
    service = _service()
    workspace_id, snapshot = build_managed_snapshot(
        service, "acme", consent=True, generation=7
    )
    assert workspace_id == service._lookup_workspace("acme")
    assert snapshot["generation"] == 7
    assert snapshot["managed_compute_consent"] is True
    assert snapshot["excluded_secret_count"] == 1
    assert [item["content"] for item in snapshot["memories"]] == [
        "A normal managed-compute memory."
    ]
    assert snapshot["memories"][0]["metadata"] == {"subject": "Queue design"}
    assert "do-not-upload" not in repr(snapshot)
    assert "metadata-secret" not in repr(snapshot)


def test_snapshot_fails_closed_on_unknown_sensitivity() -> None:
    service = _service()
    service.store.conn.execute(
        "UPDATE memories SET sensitivity='mystery' WHERE content LIKE 'A normal%'"
    )
    service.store.conn.commit()

    _, snapshot = build_managed_snapshot(service, "acme", consent=True)

    assert snapshot["memories"] == []
    assert snapshot["excluded_secret_count"] == 2


def test_workspace_snapshot_never_uploads_session_scoped_content() -> None:
    service = MemoryService.create(":memory:")
    service.remember("shared seed", workspace="acme")
    try:
        set_current_user({
            "id": "usr_alice", "email": "alice@example.test", "role": "member",
        })
        session = service.start_session("acme", agent="codex", goal="private")
        service.remember(
            "ALICE_SESSION_SECRET",
            workspace="acme",
            session_id=session["session_id"],
            scope="session",
        )

        set_current_user({
            "id": "usr_bob", "email": "bob@example.test", "role": "member",
        })
        _, snapshot = build_managed_snapshot(service, "acme", consent=True)

        assert "ALICE_SESSION_SECRET" not in repr(snapshot)
        assert "excluded_session_count" not in snapshot
        assert all(item["scope"] != "session" for item in snapshot["memories"])
    finally:
        set_current_user(None)


def test_snapshot_enforces_aggregate_encoded_byte_limit(monkeypatch) -> None:
    service = _service()
    service.store.conn.execute(
        "UPDATE memories SET content=? WHERE content LIKE 'A normal%'",
        ("x" * 2_000,),
    )
    service.store.conn.commit()
    monkeypatch.setattr(cloud_features, "MAX_SNAPSHOT_BYTES", 512)

    with pytest.raises(CloudFeatureError, match="snapshot byte limit") as captured:
        build_managed_snapshot(service, "acme", consent=True)

    assert captured.value.status == 413


def test_snapshot_budget_uses_longer_false_consent_envelope(monkeypatch) -> None:
    service = _service()
    envelopes = []
    original_encoded_json = cloud_features._encoded_json

    def observe(value):
        if isinstance(value, dict) and value.get("memories") == []:
            envelopes.append(dict(value))
        return original_encoded_json(value)

    monkeypatch.setattr(cloud_features, "_encoded_json", observe)
    _, snapshot = build_managed_snapshot(service, "acme", consent=True)

    assert envelopes[0]["managed_compute_consent"] is False
    assert len(original_encoded_json(snapshot)) <= cloud_features.MAX_SNAPSHOT_BYTES


def test_each_snapshot_has_a_strictly_increasing_persisted_generation() -> None:
    service = _service()
    first = build_managed_snapshot(service, "acme", consent=True)[1]
    second = build_managed_snapshot(service, "acme", consent=True)[1]
    assert second["generation"] > first["generation"]

    service.remember("A new memory changes the snapshot.", workspace="acme")
    changed = build_managed_snapshot(service, "acme", consent=True)[1]
    assert changed["generation"] > second["generation"]


def test_snapshot_capture_and_generation_are_one_write_transaction(monkeypatch) -> None:
    service = _service()
    entered_serialization = threading.Event()
    release_serialization = threading.Event()
    writer_started = threading.Event()
    original_encoded_json = cloud_features._encoded_json
    blocked = False

    def delayed_encoded_json(value):
        nonlocal blocked
        if isinstance(value, dict) and value.get("content") and not blocked:
            blocked = True
            entered_serialization.set()
            assert release_serialization.wait(timeout=10)
        return original_encoded_json(value)

    def write_newer_state():
        writer_started.set()
        return service.remember("newer local state", workspace="acme")

    monkeypatch.setattr(cloud_features, "_encoded_json", delayed_encoded_json)
    with ThreadPoolExecutor(max_workers=2) as pool:
        older_future = pool.submit(
            build_managed_snapshot, service, "acme", consent=True
        )
        assert entered_serialization.wait(timeout=10)
        writer_future = pool.submit(write_newer_state)
        assert writer_started.wait(timeout=10)
        assert not writer_future.done()
        release_serialization.set()
        older = older_future.result(timeout=10)[1]
        writer_future.result(timeout=10)

    newer = build_managed_snapshot(service, "acme", consent=True)[1]
    assert newer["generation"] > older["generation"]
    assert "newer local state" not in repr(older)
    assert "newer local state" in repr(newer)


class _FakeCloud(CloudFeatureClient):
    def __init__(self) -> None:
        super().__init__("https://compute.example.test", "org_1", "token")
        object.__setattr__(self, "uploaded", None)

    def upload_snapshot(self, workspace_id: str, snapshot: dict) -> dict:
        object.__setattr__(self, "uploaded", (workspace_id, snapshot))
        return {"generation": snapshot["generation"]}

    def run_job(self, workspace_id: str, kind: str, generation: int, *,
                wait_seconds: float = 20.0) -> dict:
        return {
            "job_id": "job_1",
            "input_generation": generation,
            "result": {"kind": kind, "generation": generation},
        }


def test_run_managed_job_only_sends_the_protocol_snapshot() -> None:
    cloud = _FakeCloud()
    result = run_managed_job(
        _service(), "acme", "analytics", client=cloud, wait_seconds=0
    )
    assert cloud.uploaded is not None
    assert cloud.uploaded[1]["excluded_secret_count"] == 1
    assert result["result"]["kind"] == "analytics"


def test_response_loss_retry_reuses_one_cost_bearing_job() -> None:
    class _ResponseLossCloud(CloudFeatureClient):
        def __init__(self) -> None:
            super().__init__("https://compute.example.test", "org_1", "token")
            object.__setattr__(self, "jobs", {})
            object.__setattr__(self, "lost_once", False)

        def _request(self, method, path, payload=None):
            key = payload["idempotency_key"]
            if key not in self.jobs:
                self.jobs[key] = {"job_id": "job_1"}
            if not self.lost_once:
                object.__setattr__(self, "lost_once", True)
                raise CloudFeatureError("response lost", transient=True)
            return self.jobs[key]

    cloud = _ResponseLossCloud()
    with pytest.raises(CloudFeatureError, match="response lost"):
        cloud.submit_job("ws_1", "analytics", 42, operation_id="one-run")
    result = cloud.submit_job("ws_1", "analytics", 42, operation_id="one-run")

    assert result == {"job_id": "job_1"}
    assert len(cloud.jobs) == 1


def test_intentional_jobs_at_same_generation_get_distinct_operation_ids() -> None:
    class _CaptureCloud(CloudFeatureClient):
        def __init__(self) -> None:
            super().__init__("https://compute.example.test", "org_1", "token")
            object.__setattr__(self, "keys", [])

        def _request(self, method, path, payload=None):
            self.keys.append(payload["idempotency_key"])
            return {"job_id": "job-%d" % len(self.keys)}

    cloud = _CaptureCloud()
    cloud.submit_job("ws_1", "analytics", 42)
    cloud.submit_job("ws_1", "analytics", 42)

    assert len(set(cloud.keys)) == 2


@pytest.mark.parametrize("operation_id", ["x" * 129, "snowman-\u2603", "has space"])
def test_operation_id_matches_private_job_contract(operation_id) -> None:
    client = CloudFeatureClient(
        "https://compute.example.test", "org_1", "access-token"
    )
    with pytest.raises(ValueError, match="operation_id"):
        client.submit_job("ws", "analytics", 1, operation_id=operation_id)


@pytest.mark.parametrize(
    ("status", "expected", "transient"),
    [
        (403, "Engraphis Cloud authorization was rejected.", False),
        (429, "Engraphis Cloud is temporarily busy. Try again shortly.", True),
        (503, "Engraphis Cloud is temporarily unavailable.", True),
    ],
)
def test_private_service_error_body_is_never_reflected(
        monkeypatch, status, expected, transient) -> None:
    secret = "provider-secret https://internal.service/trace"
    error = urllib.error.HTTPError(
        "https://compute.example.test/private",
        status,
        "failure",
        {},
        BytesIO(("{\"detail\":\"%s\"}" % secret).encode("utf-8")),
    )

    class _Opener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: _Opener())
    client = CloudFeatureClient(
        "https://compute.example.test", "org_1", "access-token"
    )

    with pytest.raises(CloudFeatureError) as captured:
        client._request("GET", "/private")

    assert str(captured.value) == expected
    assert captured.value.status == status
    assert captured.value.transient is transient
    assert secret not in str(captured.value)
