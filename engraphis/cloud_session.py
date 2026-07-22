"""Private-state handling for short-lived Engraphis Cloud access tokens.

The cloud control plane returns a refresh credential once. The open client stores it in the
same owner-only state directory as other machine credentials, rotates it on every refresh, and
never writes it to project configuration or logs.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Tuple

from engraphis.hosted_client import validate_cloud_base_url
from engraphis.private_state import (
    UnsafeStateFile,
    atomic_private_text,
    private_file_stat,
    read_private_text,
)

_MAX_RESPONSE_BYTES = 64 * 1024
_REFRESH_THREAD_LOCK = threading.RLock()


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


@contextmanager
def _refresh_lock():
    """Serialize spend-and-rotate of the single-use refresh credential.

    The thread lock covers one Python process; the one-byte advisory lock covers multiple
    workers sharing the same owner-only state directory.  The lock file remains in place
    so every process coordinates on one stable filesystem object.
    """
    with _REFRESH_THREAD_LOCK:
        lock_path = _session_path().with_name(".cloud_session.refresh.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            expected = private_file_stat(lock_path, allow_missing=True)
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            if expected is None:
                try:
                    descriptor = os.open(
                        str(lock_path), flags | os.O_CREAT | os.O_EXCL, 0o600
                    )
                except FileExistsError:
                    expected = private_file_stat(lock_path)
                    descriptor = os.open(str(lock_path), flags)
            else:
                descriptor = os.open(str(lock_path), flags)
            try:
                opened = os.fstat(descriptor)
                current = private_file_stat(lock_path)
                expected_identity = (
                    None if expected is None else (expected.st_dev, expected.st_ino)
                )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or getattr(opened, "st_nlink", 1) != 1
                    or (expected_identity is not None
                        and expected_identity != (opened.st_dev, opened.st_ino))
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                ):
                    raise UnsafeStateFile("cloud session refresh lock changed while opening")
            except BaseException:
                os.close(descriptor)
                raise
        except (OSError, UnsafeStateFile) as exc:
            raise CloudSessionError(
                "The cloud session refresh lock is unavailable or unsafe."
            ) from exc

        handle = os.fdopen(descriptor, "r+b")
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            current = private_file_stat(lock_path)
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise UnsafeStateFile("cloud session refresh lock changed while locking")
        except (OSError, UnsafeStateFile) as exc:
            handle.close()
            raise CloudSessionError(
                "The cloud session refresh lock is unavailable or unsafe."
            ) from exc

        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            cleanup_error = None
            try:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                cleanup_error = exc
            finally:
                try:
                    handle.close()
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
            if cleanup_error is not None and not body_failed:
                raise CloudSessionError(
                    "The cloud session refresh lock could not be released safely."
                ) from cleanup_error


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
    with _refresh_lock():
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
        try:
            exc.read(_MAX_RESPONSE_BYTES + 1)
            if exc.code in {401, 403}:
                raise CloudSessionError(
                    "The cloud session expired or was revoked; connect again."
                )
            raise CloudSessionError("Engraphis Cloud could not refresh this session.")
        finally:
            exc.close()
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
    # A configured environment value is bootstrap material. After its first successful
    # use, the server-returned rotation is persisted and must take precedence; otherwise
    # every subsequent call would replay the now-invalid bootstrap credential.
    refresh = str(saved.get("refresh_credential") or "").strip()
    refresh = refresh or os.environ.get("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "").strip()
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

    with _refresh_lock():
        # Load only after acquiring both locks. The saved rotation is the current
        # single-use credential; reading it before the lock lets two workers spend the
        # same value and causes one request to fail as a replay.
        saved = _load()
        refresh = str(saved.get("refresh_credential") or "").strip()
        refresh = refresh or os.environ.get(
            "ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", ""
        ).strip()
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
        organization_id = str(
            body.get("organization_id") or saved.get("organization_id") or ""
        ).strip()
        rotated = str(body.get("refresh_credential") or "").strip()
        if not access or not organization_id or not rotated:
            raise CloudSessionError("Engraphis Cloud returned incomplete session credentials.")
        response_subject = _validated_token_subject(
            body.get("token_subject") or token_subject
        )
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
