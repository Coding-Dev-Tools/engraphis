"""Thin client protocol for private Engraphis managed-compute features.

The open package deliberately contains no analytics, dreaming, or automatic-consolidation
algorithm. It can prepare an explicitly consented workspace snapshot, exclude secret-classified
memories, and send that snapshot to the separately operated Engraphis Cloud service. The service
is authoritative for entitlements and performs all paid computation.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

from engraphis.cloud_session import CloudSessionError, access_for_workspace

SNAPSHOT_SCHEMA = "engraphis-managed-snapshot/v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_MEMORIES = 100_000
MAX_TEXT_CHARS = 100_000


class CloudFeatureError(RuntimeError):
    """A bounded, redacted managed-cloud failure suitable for an HTTP/UI boundary."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def managed_compute_consent() -> bool:
    """Return the explicit opt-in flag; an entitlement alone is never consent."""

    return _truthy("ENGRAPHIS_MANAGED_COMPUTE_CONSENT")


def _detail(raw: bytes, fallback: str) -> str:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return fallback
    if not isinstance(body, dict):
        return fallback
    value = body.get("detail") or body.get("error")
    if isinstance(value, dict):
        value = value.get("message") or value.get("error")
    return str(value)[:500] if value else fallback


def _metadata(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > 1_000_000:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _managed_metadata(value: Any) -> dict:
    """Return only metadata fields required by the managed algorithms.

    Memory metadata is an extensibility bag and can contain connector state or credentials.
    Consent to process memory content must not silently become consent to upload that bag.
    """

    source = _metadata(value)
    subject = source.get("subject")
    if not isinstance(subject, str):
        return {}
    subject = " ".join(subject.split())
    return {"subject": subject[:200]} if subject else {}


def build_managed_snapshot(service: Any, workspace: str, *,
                           consent: Optional[bool] = None,
                           generation: Optional[int] = None) -> tuple[str, dict]:
    """Build the bounded client-side transport document for one local workspace.

    Secret-classified rows are omitted before serialization. Sensitive (but not secret) content
    is included only after the same explicit managed-compute consent as normal content.
    """

    allowed = managed_compute_consent() if consent is None else bool(consent)
    if not allowed:
        raise CloudFeatureError(
            "Managed compute is off. Opt in before uploading workspace content by setting "
            "ENGRAPHIS_MANAGED_COMPUTE_CONSENT=1.",
            status=409,
        )
    clean_workspace = service._clean_ws(workspace)
    workspace_id = service._lookup_workspace(clean_workspace)
    if not workspace_id:
        raise CloudFeatureError("The selected workspace does not exist.", status=404)
    rows = service.store.conn.execute(
        "SELECT id, title, content, mtype, scope, ingested_at, last_access, valid_from, "
        "valid_to, expired_at, stability, importance, pinned, sensitivity, metadata "
        "FROM memories WHERE workspace_id=? "
        "ORDER BY ingested_at, id LIMIT ?",
        (workspace_id, MAX_MEMORIES + 1),
    ).fetchall()
    if len(rows) > MAX_MEMORIES:
        raise CloudFeatureError("The workspace exceeds the managed snapshot memory limit.",
                                status=413)
    memories = []
    excluded_secrets = 0
    for row in rows:
        item = dict(row)
        sensitivity = str(item.get("sensitivity") or "normal").casefold()
        metadata = _metadata(item.get("metadata"))
        metadata_sensitivity = str(metadata.get("sensitivity") or "").casefold()
        if sensitivity == "secret" or metadata_sensitivity in {
            "secret", "private-secret", "credential", "credentials",
        }:
            excluded_secrets += 1
            continue
        content = str(item.get("content") or "")
        title = str(item.get("title") or "")
        if len(content) > MAX_TEXT_CHARS or len(title) > 500:
            raise CloudFeatureError(
                "A memory exceeds the managed snapshot text limit; it was not uploaded.",
                status=413,
            )
        memories.append({
            "id": str(item["id"]),
            "title": title,
            "content": content,
            "mtype": str(item.get("mtype") or "semantic"),
            "scope": str(item.get("scope") or "workspace"),
            "ingested_at": float(item.get("ingested_at") or 0),
            "last_access": float(item.get("last_access") or item.get("ingested_at") or 0),
            "valid_from": float(item.get("valid_from") or 0),
            "valid_to": item.get("valid_to"),
            "expired_at": item.get("expired_at"),
            "stability": float(item.get("stability") or 1),
            "importance": float(item.get("importance") or 0.5),
            "pinned": bool(item.get("pinned")),
            "sensitivity": sensitivity,
            "metadata": _managed_metadata(metadata),
        })
    snapshot_generation = generation if generation is not None else time.time_ns()
    return workspace_id, {
        "schema": SNAPSHOT_SCHEMA,
        "generation": int(snapshot_generation),
        "managed_compute_consent": True,
        "excluded_secret_count": excluded_secrets,
        "memories": memories,
    }


@dataclass(frozen=True)
class CloudFeatureClient:
    base_url: str
    organization_id: str
    access_token: str
    timeout_seconds: float = 15.0

    @classmethod
    def from_environment(cls, workspace_id: str) -> "CloudFeatureClient":
        try:
            access_token, organization_id, base_url = access_for_workspace(workspace_id)
        except (CloudSessionError, ValueError) as exc:
            raise CloudFeatureError(str(exc), status=503) from exc
        return cls(base_url=base_url, organization_id=organization_id,
                   access_token=access_token)

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        encoded = None
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.access_token,
            "User-Agent": "Engraphis/1.0 (+https://engraphis.com)",
        }
        if payload is not None:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=encoded,
                                         headers=headers, method=method)
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(64 * 1024 + 1)
            raise CloudFeatureError(
                _detail(raw[:64 * 1024], "Engraphis Cloud rejected the request."),
                status=exc.code,
                transient=exc.code >= 500,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CloudFeatureError(
                "Engraphis Cloud is temporarily unreachable.", transient=True,
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CloudFeatureError("Engraphis Cloud returned an oversized response.",
                                    transient=True)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise CloudFeatureError("Engraphis Cloud returned an invalid response.",
                                    transient=True) from exc
        if not isinstance(body, dict):
            raise CloudFeatureError("Engraphis Cloud returned an invalid response.",
                                    transient=True)
        return body

    def _workspace_path(self, workspace_id: str) -> str:
        return "/v1/organizations/%s/workspaces/%s" % (
            quote(self.organization_id, safe=""), quote(workspace_id, safe=""))

    def upload_snapshot(self, workspace_id: str, snapshot: dict) -> dict:
        return self._request("POST", self._workspace_path(workspace_id) + "/snapshot", snapshot)

    def get_policy(self, workspace_id: str) -> dict:
        return self._request("GET", self._workspace_path(workspace_id) + "/automation-policy")

    def save_policy(self, workspace_id: str, policy: dict) -> dict:
        return self._request("PUT", self._workspace_path(workspace_id) + "/automation-policy",
                             policy)

    def submit_job(self, workspace_id: str, kind: str, generation: int) -> dict:
        payload = {
            "kind": kind,
            "expected_generation": generation,
            "idempotency_key": "%s:%s:%s" % (kind, generation, uuid.uuid4().hex[:12]),
        }
        return self._request("POST", self._workspace_path(workspace_id) + "/jobs", payload)

    def get_job(self, workspace_id: str, job_id: str) -> dict:
        return self._request(
            "GET", self._workspace_path(workspace_id) + "/jobs/" + quote(job_id, safe=""))

    def list_jobs(self, workspace_id: str, *, limit: int = 10) -> dict:
        bounded_limit = min(50, max(1, int(limit)))
        return self._request(
            "GET",
            self._workspace_path(workspace_id) + "/jobs?limit=" + str(bounded_limit),
        )

    def get_result(self, workspace_id: str, job_id: str) -> dict:
        return self._request(
            "GET",
            self._workspace_path(workspace_id) + "/jobs/" + quote(job_id, safe="") + "/result",
        )

    def run_job(self, workspace_id: str, kind: str, generation: int, *,
                wait_seconds: float = 20.0) -> dict:
        submitted = self.submit_job(workspace_id, kind, generation)
        job_id = str(submitted.get("job_id") or "")
        if not job_id:
            raise CloudFeatureError("Engraphis Cloud did not return a job identifier.",
                                    transient=True)
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            state = self.get_job(workspace_id, job_id)
            status = str(state.get("state") or "")
            if status in {"succeeded", "stale"}:
                return self.get_result(workspace_id, job_id)
            if status in {"failed", "canceled"}:
                raise CloudFeatureError(
                    "Managed %s did not complete (%s)." % (kind, status),
                    transient=status == "failed",
                )
            if time.monotonic() >= deadline:
                return {"job_id": job_id, "state": status or "queued", "pending": True}
            time.sleep(0.25)


def run_managed_job(service: Any, workspace: str, kind: str, *,
                    client: Optional[CloudFeatureClient] = None,
                    wait_seconds: float = 20.0) -> dict:
    workspace_id, snapshot = build_managed_snapshot(service, workspace)
    cloud = client or CloudFeatureClient.from_environment(workspace_id)
    receipt = cloud.upload_snapshot(workspace_id, snapshot)
    generation = int(receipt.get("generation", snapshot["generation"]))
    return cloud.run_job(workspace_id, kind, generation, wait_seconds=wait_seconds)
