"""Thin client protocol for private Engraphis managed-compute features.

The open package deliberately contains no analytics, dreaming, or automatic-consolidation
algorithm. It prepares a bounded workspace snapshot, excludes secret-classified memories, and
sends that snapshot to the separately operated Engraphis Cloud service. The service is
authoritative for entitlements and performs all paid computation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

from engraphis.cloud_session import CloudSessionError, access_for_workspace
from engraphis.cloud_session import configured as cloud_session_configured
from engraphis.hosted_client import account_url, build_pinned_https_opener

SNAPSHOT_SCHEMA = "engraphis-managed-snapshot/v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_MEMORIES = 100_000
MAX_TEXT_CHARS = 100_000
_AUTOMATION_BOOTSTRAP_SCHEMA = "engraphis-automation-bootstrap/v1"


def _truthy(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() in ("1", "true", "yes", "on")


def _automation_bootstrap_key(organization_id: str, workspace_id: str) -> str:
    """Return a bounded, tenant-specific key without exposing identifiers in SQLite."""

    identity = ("%s\0%s" % (organization_id, workspace_id)).encode("utf-8")
    return "managed_automation_bootstrap:" + hashlib.sha256(identity).hexdigest()


def automation_bootstrap_phase(
    service: Any, organization_id: str, workspace_id: str
) -> str:
    """Read the durable phase of first-policy provisioning for one hosted workspace."""

    key = _automation_bootstrap_key(organization_id, workspace_id)
    row = service.store.conn.execute(
        "SELECT value FROM sync_state WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        return ""
    try:
        value = json.loads(str(row["value"]))
    except (TypeError, ValueError, RecursionError):
        return ""
    if not isinstance(value, dict) or value.get("schema") != _AUTOMATION_BOOTSTRAP_SCHEMA:
        return ""
    phase = str(value.get("phase") or "")
    return phase if phase in {"snapshot_uploaded", "policy_saved"} else ""


def save_automation_bootstrap_phase(
    service: Any,
    organization_id: str,
    workspace_id: str,
    phase: str,
    *,
    generation: Optional[int] = None,
) -> None:
    """Persist bootstrap progress so a policy-save retry never re-uploads memory."""

    if phase not in {"snapshot_uploaded", "policy_saved"}:
        raise ValueError("invalid automation bootstrap phase")
    value = {
        "schema": _AUTOMATION_BOOTSTRAP_SCHEMA,
        "phase": phase,
    }
    if generation is not None:
        value["generation"] = int(generation)
    key = _automation_bootstrap_key(organization_id, workspace_id)
    conn = service.store.conn
    owns_transaction = not conn.transaction_owned_by_current_thread()
    try:
        conn.execute(
            "INSERT INTO sync_state(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, json.dumps(value, sort_keys=True, separators=(",", ":")), time.time()),
        )
        if owns_transaction:
            conn.commit()
    except BaseException:
        if owns_transaction and conn.transaction_owned_by_current_thread():
            conn.rollback()
        raise


class CloudFeatureError(RuntimeError):
    """A bounded, redacted managed-cloud failure suitable for an HTTP/UI boundary."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 transient: bool = False, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def managed_compute_consent() -> bool:
    """Return whether this installation may upload workspace content for managed work.

    Consent travels with the cloud account: connecting an installation to Engraphis Cloud
    accepts the terms that cover managed compute, so a connected installation is allowed by
    default and the customer is never asked to hand-edit an environment variable.

    A local installation with no cloud session is never allowed — there is no account, so
    there is no agreement to rely on.

    ``ENGRAPHIS_MANAGED_COMPUTE_CONSENT`` remains an explicit override for operators who want
    to force the answer either way; ``=0`` opts a connected installation back out.
    """
    override = os.environ.get("ENGRAPHIS_MANAGED_COMPUTE_CONSENT")
    if override is not None and override.strip() != "":
        return _truthy(override)
    try:
        return bool(cloud_session_configured(require_compute=False))
    except Exception:
        # Consent must never be the reason a dashboard fails to render.
        return False


def _public_http_error(status: int) -> tuple[str, bool]:
    """Map a private-service status to fixed public copy.

    Provider bodies are untrusted and may contain credentials, internal URLs, stack traces,
    or implementation details. They must never cross the public package's HTTP/UI boundary.
    """
    if status in {401, 403}:
        return "Engraphis Cloud authorization was rejected.", False
    if status == 402:
        # 402 is the control plane's "no active paid entitlement", which a lapsed or
        # past_due subscription reaches just as often as a genuine free plan. Name the
        # billing page so a paying customer can fix it instead of reading a dead end.
        return (
            "This hosted feature needs an active Engraphis Cloud subscription. Check "
            # Plan-neutral: upgrade_url() with no argument resolves plan="pro", which
            # sends a lapsed Team subscriber to the Pro checkout for a product they
            # already hold at a higher tier.
            "billing or upgrade at %s." % account_url(),
            False,
        )
    if status == 404:
        return "The hosted workspace or feature was not found.", False
    if status == 409:
        return "Engraphis Cloud could not accept the current workspace state.", False
    if status == 413:
        return "Engraphis Cloud rejected the request size.", False
    if status == 429:
        return "Engraphis Cloud is temporarily busy. Try again shortly.", True
    if status >= 500:
        return "Engraphis Cloud is temporarily unavailable.", True
    return "Engraphis Cloud rejected the request.", False


def _public_session_error(status: int) -> tuple[str, bool]:
    """Map a session-acquisition failure to fixed, actionable public copy.

    ``CloudSessionError`` text is never forwarded across this boundary (it can quote local
    state paths), but the bare status alone reads as an outage for every cause.  A customer
    whose subscription lapsed, whose session was revoked, or who is simply offline each
    need a different next step, and only the transient ones are worth retrying.
    """

    if status == 401:
        return (
            "Connect this installation to Engraphis Cloud to use hosted features.",
            False,
        )
    if status == 402:
        return (
            "This hosted feature needs an active Engraphis Cloud subscription. Check "
            # Plan-neutral: upgrade_url() with no argument resolves plan="pro", which
            # sends a lapsed Team subscriber to the Pro checkout for a product they
            # already hold at a higher tier.
            "billing or upgrade at %s." % account_url(),
            False,
        )
    if status == 403:
        return ("Engraphis Cloud authorization was rejected.", False)
    if status == 409:
        return (
            "The saved cloud session is unusable; connect this installation again.",
            False,
        )
    if status == 429:
        return ("Engraphis Cloud is temporarily busy. Try again shortly.", True)
    if status >= 500:
        return (
            "Engraphis Cloud is unreachable; hosted features resume once it responds.",
            True,
        )
    return ("The cloud session is unavailable.", False)


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


def _encoded_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _reserve_snapshot_generation(service: Any, workspace_id: str,
                                 requested: Optional[int] = None) -> int:
    """Persist a strictly increasing generation for one local workspace.

    A content hash is useful as an equality fingerprint but is not an ordered version;
    sending a numerically smaller hash after a newer snapshot violates the hosted
    compare-and-swap contract.  Persisting the high-water mark also survives process
    restarts and protects against a wall-clock adjustment.
    """
    key = "managed_snapshot_generation:%s" % workspace_id
    conn = service.store.conn
    owns_transaction = not conn.transaction_owned_by_current_thread()
    try:
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        try:
            previous = int(row["value"]) if row is not None else 0
        except (TypeError, ValueError, OverflowError):
            previous = 0
        if requested is None:
            generation = max(previous + 1, time.time_ns())
        else:
            generation = int(requested)
            if generation <= previous:
                raise CloudFeatureError(
                    "Managed snapshot generation must advance.", status=409
                )
        if generation > 9_223_372_036_854_775_807:
            raise CloudFeatureError(
                "The managed snapshot generation is exhausted.", status=409
            )
        conn.execute(
            "INSERT INTO sync_state(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, str(generation), time.time()),
        )
        if owns_transaction:
            conn.commit()
        return generation
    except BaseException:
        if owns_transaction and conn.transaction_owned_by_current_thread():
            conn.rollback()
        raise


def build_managed_snapshot(service: Any, workspace: str, *,
                           consent: Optional[bool] = None,
                           generation: Optional[int] = None) -> tuple[str, dict]:
    """Build and version one transactionally consistent workspace snapshot."""
    conn = service.store.conn
    owns_transaction = not conn.transaction_owned_by_current_thread()
    try:
        if owns_transaction:
            # This blocks local writers before any snapshot row is read. Generation is
            # reserved inside the same transaction, so an older capture cannot receive a
            # newer generation and overwrite a later local state in the hosted service.
            conn.execute("BEGIN IMMEDIATE")
        result = _build_managed_snapshot_locked(
            service, workspace, consent=consent, generation=generation
        )
        if owns_transaction:
            conn.commit()
        return result
    except BaseException:
        if owns_transaction and conn.transaction_owned_by_current_thread():
            conn.rollback()
        raise


def _build_managed_snapshot_locked(service: Any, workspace: str, *,
                                    consent: Optional[bool] = None,
                                    generation: Optional[int] = None) -> tuple[str, dict]:
    """Build the bounded client-side transport document for one local workspace.

    Secret-classified rows are omitted before serialization. ``consent`` allows an
    already-confirmed caller to pass its decision explicitly; otherwise
    :func:`managed_compute_consent` decides, which allows cloud-connected installations and
    denies purely local ones.
    """

    clean_workspace = service._clean_ws(workspace)
    workspace_id = service._lookup_workspace(clean_workspace)
    if not workspace_id:
        raise CloudFeatureError("The selected workspace does not exist.", status=404)
    allowed = managed_compute_consent() if consent is None else bool(consent)
    if not allowed:
        raise CloudFeatureError(
            "Managed compute is turned off for this installation, so no workspace content "
            "was uploaded. Connect this installation to Engraphis Cloud to use it.",
            status=409,
            code="consent_required",
        )
    snapshot_generation = _reserve_snapshot_generation(
        service, workspace_id, requested=generation
    )
    count = service.store.conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE workspace_id=? "
        "AND COALESCE(scope, 'workspace')!='session'",
        (workspace_id,),
    ).fetchone()["n"]
    if count > MAX_MEMORIES:
        raise CloudFeatureError("The workspace exceeds the managed snapshot memory limit.",
                                status=413)
    rows = service.store.conn.execute(
        "SELECT id, title, content, mtype, scope, ingested_at, last_access, valid_from, "
        "valid_to, valid_to_recorded_at, expired_at, subject_key, claim_kind, "
        "stability, importance, pinned, sensitivity, metadata "
        "FROM memories WHERE workspace_id=? AND COALESCE(scope, 'workspace')!='session' "
        "ORDER BY ingested_at, id",
        (workspace_id,),
    )
    memories = []
    excluded_secrets = 0
    # Use the widest possible generation value when budgeting so the final envelope can
    # never exceed the cap after its monotonic generation is inserted.
    snapshot_bytes = len(_encoded_json({
        "schema": SNAPSHOT_SCHEMA,
        "generation": 9_223_372_036_854_775_807,
        # ``false`` is one byte longer than ``true``. Budget the larger encoding so
        # protocol variants cannot cross the client cap at the exact boundary.
        "managed_compute_consent": False,
        "excluded_secret_count": MAX_MEMORIES,
        "memories": [],
    }))
    for row in rows:
        item = dict(row)
        sensitivity = str(item.get("sensitivity") or "normal").strip().casefold()
        metadata = _metadata(item.get("metadata"))
        metadata_sensitivity = str(metadata.get("sensitivity") or "").strip().casefold()
        allowed = {"", "normal", "sensitive"}
        if sensitivity not in allowed - {""} or metadata_sensitivity not in allowed:
            excluded_secrets += 1
            continue
        if metadata_sensitivity == "sensitive":
            sensitivity = "sensitive"
        content = str(item.get("content") or "")
        title = str(item.get("title") or "")
        if len(content) > MAX_TEXT_CHARS or len(title) > 500:
            raise CloudFeatureError(
                "A memory exceeds the managed snapshot text limit; it was not uploaded.",
                status=413,
            )
        memory = {
            "id": str(item["id"]),
            "title": title,
            "content": content,
            "mtype": str(item.get("mtype") or "semantic"),
            "scope": str(item.get("scope") or "workspace"),
            "ingested_at": float(item.get("ingested_at") or 0),
            "last_access": float(item.get("last_access") or item.get("ingested_at") or 0),
            "valid_from": float(item.get("valid_from") or 0),
            "valid_to": item.get("valid_to"),
            "valid_to_recorded_at": item.get("valid_to_recorded_at"),
            "expired_at": item.get("expired_at"),
            "subject_key": str(item.get("subject_key") or ""),
            "claim_kind": str(item.get("claim_kind") or ""),
            "stability": float(item.get("stability") or 1),
            "importance": float(item.get("importance") or 0.5),
            "pinned": bool(item.get("pinned")),
            "sensitivity": sensitivity,
            "metadata": _managed_metadata(metadata),
        }
        encoded_memory = _encoded_json(memory)
        projected = snapshot_bytes + len(encoded_memory) + (1 if memories else 0)
        if projected > MAX_SNAPSHOT_BYTES:
            raise CloudFeatureError(
                "The workspace exceeds the managed snapshot byte limit; it was not uploaded.",
                status=413,
            )
        snapshot_bytes = projected
        memories.append(memory)
    if not 0 < snapshot_generation <= 9_223_372_036_854_775_807:
        raise CloudFeatureError("Managed snapshot generation is invalid.", status=409)
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
    # A dataclass ``__repr__`` prints every field, so the default would put a live bearer
    # token into any traceback, log line, or debugger frame that renders this client.
    access_token: str = field(repr=False)
    timeout_seconds: float = 15.0

    @classmethod
    def from_environment(cls, workspace_id: str) -> "CloudFeatureClient":
        try:
            access_token, organization_id, base_url = access_for_workspace(workspace_id)
        except CloudSessionError as exc:
            status = exc.status if 400 <= exc.status <= 599 else 503
            message, transient = _public_session_error(status)
            # A missing local session is the one 401 that can lead directly to the
            # Cloud trial.  Keep it distinguishable from a revoked/expired session:
            # its owner may already have an entitlement and must not be offered a
            # second trial.  The UI still combines this code with the authoritative
            # ``trial.available`` flag before drawing the signup surface.
            try:
                unconfigured = not cloud_session_configured(require_compute=True)
            except Exception:  # noqa: BLE001 - preserve the original bounded error
                unconfigured = False
            raise CloudFeatureError(
                message,
                status=status,
                transient=transient,
                code="cloud_unconfigured" if status == 401 and unconfigured else None,
            ) from exc
        except ValueError as exc:
            raise CloudFeatureError(
                "The cloud session configuration is invalid.", status=409
            ) from exc
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
            encoded = _encoded_json(payload)
            if len(encoded) > MAX_SNAPSHOT_BYTES:
                raise CloudFeatureError(
                    "The managed-cloud request exceeds the upload byte limit.", status=413,
                )
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=encoded,
                                         headers=headers, method=method)
        try:
            with build_pinned_https_opener(_NoRedirect()).open(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            message, transient = _public_http_error(exc.code)
            exc.close()
            raise CloudFeatureError(
                message,
                status=exc.code,
                transient=transient,
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

    def submit_job(self, workspace_id: str, kind: str, generation: int, *,
                   operation_id: Optional[str] = None) -> dict:
        operation = str(operation_id or uuid.uuid4().hex).strip()
        if len(operation) > 128 or re.fullmatch(r"[A-Za-z0-9._:-]+", operation) is None:
            raise ValueError("operation_id must match [A-Za-z0-9._:-]+ (max 128 characters)")
        payload = {
            "kind": kind,
            "expected_generation": generation,
            "idempotency_key": operation,
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
        operation_id = uuid.uuid4().hex
        try:
            submitted = self.submit_job(
                workspace_id, kind, generation, operation_id=operation_id
            )
        except CloudFeatureError as exc:
            if not exc.transient:
                raise
            # One bounded transport retry reuses this run's operation id. A later
            # intentional run mints a new id even when kind/generation are unchanged.
            submitted = self.submit_job(
                workspace_id, kind, generation, operation_id=operation_id
            )
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
    # Authorize before touching the store.  ``build_managed_snapshot`` takes an exclusive
    # BEGIN IMMEDIATE write lock, *commits* a monotonic generation row, and serializes up
    # to ``MAX_MEMORIES`` rows -- so a lapsed subscriber clicking a paid tab stalled every
    # local writer and durably advanced state before being told 402.  The consent
    # pre-check does not cover this: a lapsed account still has a saved cloud session.
    # ``automation_set`` already gates in this order; match it.
    resolved_id = service._lookup_workspace(service._clean_ws(workspace))
    if not resolved_id:
        raise CloudFeatureError("The selected workspace does not exist.", status=404)
    cloud = client or CloudFeatureClient.from_environment(resolved_id)
    workspace_id, snapshot = build_managed_snapshot(service, workspace)
    receipt = cloud.upload_snapshot(workspace_id, snapshot)
    generation = int(receipt.get("generation", snapshot["generation"]))
    return cloud.run_job(workspace_id, kind, generation, wait_seconds=wait_seconds)
