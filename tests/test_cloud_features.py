from __future__ import annotations

import threading
import json
from concurrent.futures import ThreadPoolExecutor
import http.client
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


def _service(*, approved: bool = True) -> MemoryService:
    service = MemoryService.create(":memory:")
    service.remember(
        "A normal managed-compute memory.",
        workspace="acme",
        metadata={"subject": "  Queue   design  "},
    )
    # Seed historical rows below the new capture-time boundary. These verify cloud
    # export filtering for legacy data without weakening the public write API.
    secret = service.remember("A legacy private value.", workspace="acme")
    service.store.conn.execute(
        "UPDATE memories SET metadata=?, content=?, sensitivity='secret' WHERE id=?",
        (json.dumps({"subject": "Queue design", "api_key": "metadata-secret"}),
         "password=do-not-upload", secret["id"]),
    )
    service.store.conn.commit()
    if approved:
        service.set_managed_processing_policy("acme", enabled=True, confirmed=True, remote_revision=2)
    return service


def _cloud_session(monkeypatch, connected: bool) -> list:
    """Pin whether this installation has a cloud session, and record how it was asked."""

    calls: list = []

    def configured(**kwargs):
        calls.append(kwargs)
        return connected

    monkeypatch.setattr(cloud_features, "cloud_session_configured", configured)
    return calls


def test_missing_workspace_approval_never_uploads(monkeypatch) -> None:
    monkeypatch.delenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", raising=False)
    service = _service(approved=False)
    assert cloud_features.managed_compute_consent(service, "acme") is False
    with pytest.raises(CloudFeatureError, match="approval for this workspace") as captured:
        build_managed_snapshot(service, "acme", consent=True)
    assert captured.value.status == 409
    assert captured.value.code == "consent_required"


def test_connecting_or_environment_cannot_grant_workspace_approval(monkeypatch) -> None:
    _cloud_session(monkeypatch, connected=True)
    monkeypatch.setenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", "yes")
    service = _service(approved=False)
    assert cloud_features.managed_compute_consent() is False
    assert cloud_features.managed_compute_consent(service, "acme") is False
    with pytest.raises(CloudFeatureError, match="approval for this workspace"):
        build_managed_snapshot(service, "acme", consent=True)


def test_operator_override_can_only_deny_workspace_approval(monkeypatch) -> None:
    service = _service()
    monkeypatch.setenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", "0")
    assert cloud_features.managed_compute_consent(service, "acme") is False
    with pytest.raises(CloudFeatureError, match="approval for this workspace"):
        build_managed_snapshot(service, "acme")


@pytest.mark.parametrize("override", ["", "   ", "yes"])
def test_explicit_workspace_approval_enables_snapshot(monkeypatch, override) -> None:
    monkeypatch.setenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", override)
    service = _service()
    assert cloud_features.managed_compute_consent(service, "acme") is True
    _, snapshot = build_managed_snapshot(service, "acme")
    assert snapshot["managed_compute_consent"] is True
    assert snapshot["processing_policy_revision"] == 2


def test_policy_read_failure_denies_processing() -> None:
    class Broken:
        def _clean_ws(self, workspace):
            raise OSError("unreadable local state")
    assert cloud_features.managed_compute_consent(Broken(), "acme") is False


@pytest.mark.parametrize("status", [401, 403, 409, 503])
def test_cloud_client_preserves_cloud_session_status(monkeypatch, status) -> None:
    def fail_access(*args, **kwargs):
        raise cloud_features.CloudSessionError("private session detail", status=status)

    monkeypatch.setattr(cloud_features, "access_for_workspace", fail_access)

    with pytest.raises(CloudFeatureError) as caught:
        CloudFeatureClient.from_environment("ws_test")

    assert caught.value.status == status
    assert "private session detail" not in str(caught.value)


def test_cloud_client_marks_a_missing_session_as_trial_eligible(monkeypatch) -> None:
    def fail_access(*args, **kwargs):
        raise cloud_features.CloudSessionError("connect first", status=401)

    monkeypatch.setattr(cloud_features, "access_for_workspace", fail_access)
    monkeypatch.setattr(
        cloud_features, "cloud_session_configured", lambda require_compute=True: False,
    )

    with pytest.raises(CloudFeatureError) as caught:
        CloudFeatureClient.from_environment("ws_test")

    assert caught.value.status == 401
    assert caught.value.code == "cloud_unconfigured"


def test_cloud_client_reports_invalid_session_configuration_as_conflict(monkeypatch) -> None:
    def fail_access(*args, **kwargs):
        raise ValueError("private configuration detail")

    monkeypatch.setattr(cloud_features, "access_for_workspace", fail_access)

    with pytest.raises(CloudFeatureError) as caught:
        CloudFeatureClient.from_environment("ws_test")

    assert caught.value.status == 409
    assert "private configuration detail" not in str(caught.value)


def test_direct_cloud_client_rejects_header_control_characters(monkeypatch) -> None:
    client = CloudFeatureClient(
        base_url="https://compute.example.test",
        organization_id="org_1",
        access_token="token\r\nX-Evil: 1",
    )
    monkeypatch.setattr(
        cloud_features,
        "build_pinned_https_opener",
        lambda *_args: (_ for _ in ()).throw(AssertionError("network must not be opened")),
    )

    with pytest.raises(CloudFeatureError) as caught:
        client._request("GET", "/v1/jobs")

    assert caught.value.status == 409


def test_explicit_false_consent_cannot_be_overridden_by_environment(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", "1")

    with pytest.raises(CloudFeatureError, match="approval for this workspace"):
        build_managed_snapshot(_service(), "acme", consent=False)


def test_snapshot_excludes_secret_rows_before_serialization() -> None:
    service = _service()
    service.store.conn.execute(
        "UPDATE memories SET valid_to=20, valid_to_recorded_at=30, "
        "subject_key='queue.worker', claim_kind='configured_value' "
        "WHERE content LIKE 'A normal%'"
    )
    service.store.conn.commit()
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
    assert snapshot["memories"][0]["valid_to"] == 20
    assert snapshot["memories"][0]["valid_to_recorded_at"] == 30
    assert snapshot["memories"][0]["subject_key"] == "queue.worker"
    assert snapshot["memories"][0]["claim_kind"] == "configured_value"
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


def test_snapshot_excludes_pending_and_quarantined_memory() -> None:
    service = MemoryService.create(":memory:")
    approved = service.remember("Approved release fact.", workspace="acme")
    service.set_managed_processing_policy("acme", enabled=True, confirmed=True)
    pending = service.remember("Pending imported fact.", workspace="acme")
    quarantined = service.remember("Quarantined imported fact.", workspace="acme")
    service.store.conn.execute(
        "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
        (
            json.dumps({"source": "import", "trusted": False, "review_state": "pending"}),
            json.dumps({
                "provenance": {
                    "source": "import", "trusted": False, "review_state": "pending",
                }
            }),
            pending["id"],
        ),
    )
    service.store.conn.execute(
        "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
        (
            json.dumps({
                "source": "import", "trusted": False, "review_state": "pending",
                "quarantined": True,
            }),
            json.dumps({
                "provenance": {
                    "source": "import", "trusted": False, "review_state": "pending",
                    "quarantined": True,
                },
                "quarantine": {"state": "quarantined"},
            }),
            quarantined["id"],
        ),
    )
    service.store.conn.commit()

    _, snapshot = build_managed_snapshot(service, "acme", consent=True)

    assert [item["id"] for item in snapshot["memories"]] == [approved["id"]]
    assert "Pending imported fact" not in repr(snapshot)
    assert "Quarantined imported fact" not in repr(snapshot)


def test_workspace_snapshot_never_uploads_session_scoped_content() -> None:
    service = MemoryService.create(":memory:")
    service.remember("shared seed", workspace="acme")
    service.set_managed_processing_policy("acme", enabled=True, confirmed=True)
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

    def get_policy(self, workspace_id: str) -> dict:
        return {"workspace_id": workspace_id, "enabled": True}

    def run_job(self, workspace_id: str, kind: str, generation: int, *,
                wait_seconds: float = 20.0) -> dict:
        return {
            "job_id": "job_1",
            "input_generation": generation,
            "result": {"kind": kind, "generation": generation},
        }


def test_run_managed_job_only_sends_the_protocol_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", "1")
    cloud = _FakeCloud()
    result = run_managed_job(
        _service(), "acme", "analytics", client=cloud, wait_seconds=0
    )
    assert cloud.uploaded is not None
    assert cloud.uploaded[1]["excluded_secret_count"] == 1
    assert result["result"]["kind"] == "analytics"


def test_run_managed_job_checks_entitlement_before_reserving_generation(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", "1")

    class _LapsedCloud(_FakeCloud):
        def get_policy(self, workspace_id: str) -> dict:
            raise CloudFeatureError("Subscription is not active.", status=402)

    service = _service()
    cloud = _LapsedCloud()
    with pytest.raises(CloudFeatureError, match="Subscription is not active"):
        run_managed_job(service, "acme", "analytics", client=cloud, wait_seconds=0)

    assert cloud.uploaded is None
    reserved = service.store.conn.execute(
        "SELECT COUNT(*) FROM sync_state WHERE key LIKE 'managed_snapshot_generation:%'"
    ).fetchone()[0]
    assert reserved == 0


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


def test_truncated_private_error_response_keeps_the_public_status(monkeypatch) -> None:
    """Provider diagnostics cannot turn a 403 into an internal-error traceback."""

    error = urllib.error.HTTPError(
        "https://compute.example.test/private",
        403,
        "denied",
        {},
        BytesIO(b'{"detail":"private"}'),
    )

    def fail_drain(*args, **kwargs):
        raise http.client.IncompleteRead(b'{"detail":"pri')

    error.read = fail_drain
    error.close = fail_drain

    class _Opener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: _Opener())
    client = CloudFeatureClient(
        "https://compute.example.test", "org_1", "access-token"
    )

    with pytest.raises(CloudFeatureError) as captured:
        client._request("GET", "/private")

    assert captured.value.status == 403
    assert captured.value.transient is False
    assert str(captured.value) == "Engraphis Cloud authorization was rejected."
