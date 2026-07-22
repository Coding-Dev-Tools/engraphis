from __future__ import annotations

import pytest

from engraphis.cloud_features import (
    CloudFeatureClient,
    CloudFeatureError,
    build_managed_snapshot,
    run_managed_job,
)
from engraphis.service import MemoryService


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


def test_snapshot_requires_explicit_consent() -> None:
    with pytest.raises(CloudFeatureError, match="Managed compute is off"):
        build_managed_snapshot(_service(), "acme", consent=False)


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


def test_run_managed_job_only_sends_the_protocol_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", "1")
    cloud = _FakeCloud()
    result = run_managed_job(
        _service(), "acme", "analytics", client=cloud, wait_seconds=0
    )
    assert cloud.uploaded is not None
    assert cloud.uploaded[1]["excluded_secret_count"] == 1
    assert result["result"]["kind"] == "analytics"
