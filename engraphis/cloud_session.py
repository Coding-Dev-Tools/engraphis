"""Private-state handling for short-lived Engraphis Cloud access tokens.

The cloud control plane returns a refresh credential once. The open client stores it in the
same owner-only state directory as other machine credentials, rotates it on every refresh, and
never writes it to project configuration or logs.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from engraphis.hosted_client import validate_cloud_base_url
from engraphis.private_state import UnsafeStateFile, atomic_private_text, read_private_text

_MAX_RESPONSE_BYTES = 64 * 1024


class CloudSessionError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validated_token_subject(value: object) -> str:
    subject = str(value or "member").strip().lower()
    if subject not in {"device", "member"}:
        raise CloudSessionError("Cloud token subject must be 'device' or 'member'.")
    return subject


def _token_subject(saved: dict) -> str:
    configured = os.environ.get("ENGRAPHIS_CLOUD_TOKEN_SUBJECT", "").strip()
    return _validated_token_subject(configured or saved.get("token_subject") or "member")


def _session_path() -> Path:
    root = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    base = Path(root).expanduser() if root else Path.home() / ".engraphis"
    return base / "cloud_session.json"


def _load() -> dict:
    try:
        raw = read_private_text(_session_path(), max_bytes=64 * 1024, allow_missing=True)
    except UnsafeStateFile as exc:
        raise CloudSessionError("The saved cloud session has unsafe filesystem permissions.") from exc
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise CloudSessionError("The saved cloud session is invalid; connect again.") from exc
    return value if isinstance(value, dict) else {}


def _save(value: dict) -> None:
    path = _session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_private_text(path, json.dumps(value, sort_keys=True, separators=(",", ":")))


def save_bootstrap(response: dict, *, control_url: str,
                   compute_url: Optional[str] = None) -> None:
    """Persist the one-time bootstrap/refresh material returned by the control plane."""

    refresh = str(response.get("refresh_credential") or "").strip()
    organization_id = str(response.get("organization_id") or "").strip()
    if not refresh or not organization_id:
        raise CloudSessionError("Cloud bootstrap did not return a refresh credential.")
    value = {
        "schema": "engraphis-cloud-session/v1",
        "control_url": validate_cloud_base_url(control_url),
        "compute_url": validate_cloud_base_url(compute_url) if compute_url else "",
        "organization_id": organization_id,
        "installation_id": str(response.get("installation_id") or ""),
        "device_id": str(response.get("device_id") or ""),
        "member_id": str(response.get("member_id") or ""),
        "refresh_credential": refresh,
        "refresh_expires_at": str(response.get("refresh_expires_at") or ""),
        "token_subject": _validated_token_subject(
            response.get("token_subject") or "member"
        ),
    }
    _save(value)


def _post_refresh(control_url: str, refresh: str, workspace_id: str,
                  token_subject: str) -> dict:
    payload = json.dumps({
        "refresh_credential": refresh,
        "workspace_id": workspace_id,
        "token_subject": token_subject,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        control_url + "/v1/tokens/refresh",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Engraphis/1.0 (+https://engraphis.com)",
        },
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=10.0
        ) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.read(_MAX_RESPONSE_BYTES + 1)
        if exc.code in {401, 403}:
            raise CloudSessionError("The cloud session expired or was revoked; connect again.")
        raise CloudSessionError("Engraphis Cloud could not refresh this session.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudSessionError("Engraphis Cloud is temporarily unreachable.") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise CloudSessionError("Engraphis Cloud returned an oversized session response.")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CloudSessionError("Engraphis Cloud returned an invalid session response.") from exc
    if not isinstance(body, dict):
        raise CloudSessionError("Engraphis Cloud returned an invalid session response.")
    return body


def configured(*, require_compute: bool = True) -> bool:
    """Return whether enough non-secret configuration exists to attempt a refresh."""

    direct_token = os.environ.get("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "").strip()
    direct_org = os.environ.get("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "").strip()
    direct_compute = os.environ.get("ENGRAPHIS_CLOUD_COMPUTE_URL", "").strip()
    if direct_token and direct_org and (direct_compute or not require_compute):
        return True
    saved = _load()
    refresh = os.environ.get("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "").strip()
    refresh = refresh or str(saved.get("refresh_credential") or "").strip()
    control = os.environ.get("ENGRAPHIS_CLOUD_CONTROL_URL", "").strip()
    control = control or str(saved.get("control_url") or "").strip()
    compute = direct_compute or str(saved.get("compute_url") or "").strip()
    if refresh and control:
        _token_subject(saved)
    return bool(refresh and control and (compute or not require_compute))


def access_for_workspace(
    workspace_id: str, *, require_compute: bool = True
) -> Tuple[str, str, str]:
    """Return ``(access_token, organization_id, compute_url)`` for a bound workspace."""

    direct_token = os.environ.get("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "").strip()
    direct_org = os.environ.get("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "").strip()
    direct_compute = os.environ.get("ENGRAPHIS_CLOUD_COMPUTE_URL", "").strip()
    if direct_token and direct_org and (direct_compute or not require_compute):
        compute_url = validate_cloud_base_url(direct_compute) if direct_compute else ""
        return direct_token, direct_org, compute_url

    saved = _load()
    refresh = os.environ.get("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "").strip()
    refresh = refresh or str(saved.get("refresh_credential") or "").strip()
    control = os.environ.get("ENGRAPHIS_CLOUD_CONTROL_URL", "").strip()
    control = control or str(saved.get("control_url") or "").strip()
    compute = direct_compute or str(saved.get("compute_url") or "").strip()
    if not refresh or not control or (require_compute and not compute):
        raise CloudSessionError("Connect this installation to Engraphis Cloud first.")
    control = validate_cloud_base_url(control)
    compute = validate_cloud_base_url(compute) if compute else ""
    token_subject = _token_subject(saved)
    body = _post_refresh(control, refresh, workspace_id, token_subject)
    access = str(body.get("access_token") or "").strip()
    organization_id = str(body.get("organization_id") or saved.get("organization_id") or "").strip()
    rotated = str(body.get("refresh_credential") or "").strip()
    if not access or not organization_id or not rotated:
        raise CloudSessionError("Engraphis Cloud returned incomplete session credentials.")
    response_subject = _validated_token_subject(
        body.get("token_subject") or token_subject
    )
    if not os.environ.get("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "").strip():
        updated = dict(saved)
        updated.update({
            "schema": "engraphis-cloud-session/v1",
            "control_url": control,
            "compute_url": compute,
            "organization_id": organization_id,
            "refresh_credential": rotated,
            "refresh_expires_at": str(body.get("refresh_expires_at") or ""),
            "token_subject": response_subject,
        })
        _save(updated)
    return access, organization_id, compute
