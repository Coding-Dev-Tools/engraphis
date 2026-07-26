"""Private-state handling for short-lived Engraphis Cloud access tokens.

The cloud control plane returns a refresh credential once. The open client stores it in the
same owner-only state directory as other machine credentials, rotates it on every refresh, and
never writes it to project configuration or logs.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Tuple

from engraphis.hosted_client import (
    CloudUrlUnresolved,
    build_pinned_https_opener,
    upgrade_url,
    validate_cloud_base_url,
)
from engraphis.private_state import (
    UnsafeStateFile,
    atomic_private_text,
    private_file_stat,
    read_private_text,
)

_MAX_RESPONSE_BYTES = 64 * 1024
_REFRESH_THREAD_LOCK = threading.RLock()


class CloudSessionError(RuntimeError):
    def __init__(self, message: str, *, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validated_token_subject(value: object) -> str:
    subject = str(value or "member").strip().lower()
    if subject not in {"device", "member"}:
        raise CloudSessionError(
            "Cloud token subject must be 'device' or 'member'.", status=409
        )
    return subject


def _token_subject(saved: dict) -> str:
    configured = os.environ.get("ENGRAPHIS_CLOUD_TOKEN_SUBJECT", "").strip()
    return _validated_token_subject(configured or saved.get("token_subject") or "member")


def _reachable_cloud_base_url(value: str) -> str:
    """Validate a cloud endpoint, keeping "offline" separate from "misconfigured".

    ``validate_cloud_base_url`` resolves the host, so a paying customer on a plane or
    behind a broken resolver raises the same ``ValueError`` as a genuinely bad URL.  The
    caller turns that into a permanent "your configuration is invalid", which is both
    wrong and unactionable.  Report a resolution failure as the retryable outage it is.
    """

    try:
        return validate_cloud_base_url(value)
    except CloudUrlUnresolved as exc:
        raise CloudSessionError("Engraphis Cloud is temporarily unreachable.") from exc


def _session_path() -> Path:
    root = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    base = Path(root).expanduser() if root else Path.home() / ".engraphis"
    return base / "cloud_session.json"


def _refresh_lock_path() -> Path:
    return _session_path().with_name(".cloud_session.refresh.lock")


@contextmanager
def _refresh_lock():
    """Serialize spend-and-rotate of the single-use refresh credential.

    The thread lock covers one Python process; the one-byte advisory lock covers multiple
    workers sharing the same owner-only state directory.  The lock file remains in place
    so every process coordinates on one stable filesystem object.
    """
    with _REFRESH_THREAD_LOCK:
        lock_path = _refresh_lock_path()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
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
                "The cloud session refresh lock is unavailable or unsafe.", status=409
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
                "The cloud session refresh lock is unavailable or unsafe.", status=409
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
                    "The cloud session refresh lock could not be released safely.", status=409
                ) from cleanup_error


def _load() -> dict:
    try:
        raw = read_private_text(_session_path(), max_bytes=64 * 1024, allow_missing=True)
    except UnsafeStateFile as exc:
        raise CloudSessionError(
            "The saved cloud session has unsafe filesystem permissions.", status=409
        ) from exc
    except (OSError, RuntimeError) as exc:
        # An unreadable or stale state mount (and Path.home() failing outright) must
        # surface as a structured, retryable cloud error rather than escaping as an
        # unhandled filesystem exception and becoming an opaque 500.
        raise CloudSessionError(
            "The saved cloud session is temporarily unreadable."
        ) from exc
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise CloudSessionError(
            "The saved cloud session is invalid; connect again.", status=409
        ) from exc
    return value if isinstance(value, dict) else {}


def _save(value: dict) -> None:
    path = _session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_private_text(path, json.dumps(value, sort_keys=True, separators=(",", ":")))


def preflight_save() -> Path:
    """Verify the session file can be saved, without saving anything.  Returns its path.

    :func:`save_bootstrap` runs *after* the control plane has answered, so a state
    directory that lost its permissions, or a ``cloud_session.json`` that is a symlink or
    a hard link, was only discovered once a single-use connect token had already been
    consumed: the customer was left with a spent token, no session, and a trip back to
    the portal for a new one.  Any caller about to spend a one-shot credential must run
    this first, so a storage fault costs nothing.

    The checks are the ones the write path itself applies -- :func:`private_file_stat` on
    the session leaf and on its refresh lock, plus the randomized sibling temp file
    :func:`atomic_private_text` has to create -- so the preflight cannot drift from what
    the real save will accept.  It deliberately never opens, creates or replaces the
    session leaf: an existing session survives a failed preflight untouched, and a first
    connect is not turned into a half-written file.
    """

    path = _session_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CloudSessionError(
            "The Engraphis state directory %s could not be created." % path.parent,
            status=409,
        ) from exc
    for candidate in (path, _refresh_lock_path()):
        try:
            private_file_stat(candidate, allow_missing=True)
        except UnsafeStateFile as exc:
            raise CloudSessionError(
                "%s is not a plain private file -- a symlink, hard link or directory is "
                "in its place. Remove it." % candidate,
                status=409,
            ) from exc
        except OSError as exc:
            raise CloudSessionError(
                "%s could not be inspected; check its permissions." % candidate,
                status=409,
            ) from exc
    # Probe the directory the way ``atomic_private_text`` will, rather than touching
    # ``cloud_session.json``: a read-only mount or a lost ACL fails here, while a valid
    # existing session is never created, truncated or replaced.
    try:
        descriptor, probe = tempfile.mkstemp(
            prefix=".%s.preflight." % path.name, dir=str(path.parent)
        )
    except OSError as exc:
        raise CloudSessionError(
            "The Engraphis state directory %s is not writable, so the cloud session "
            "cannot be saved there." % path.parent,
            status=409,
        ) from exc
    os.close(descriptor)
    try:
        os.unlink(probe)
    except OSError:  # pragma: no cover - the probe was just created in this directory
        pass
    return path


#: Plans that carry no paid cloud access, used only to default an absent activity flag.
_UNPAID_PLANS = ("free", "local")
#: Bounds on the entitlement fields before they are written into the session record. The
#: record is read back under a 64 KiB private-state cap, so an unbounded provider value
#: could grow the file past that cap and make the whole session permanently unreadable.
#: These are presentation strings; anything longer is not a plan name or a feature key.
_MAX_PLAN_CHARS = 64
_MAX_FEATURES = 32
_MAX_FEATURE_CHARS = 64


def _declared_entitlement(response: object) -> dict:
    """Return the entitlement fields a control-plane response carried, or ``{}``.

    ``DeviceRegistrationResponse`` — the body both ``/internal/devices/register`` and
    ``POST /v1/tokens/refresh`` answer with — carries ``plan``, ``cloud_features`` and
    ``cloud_access_active``.  They are read as *optional* on purpose: a control plane that
    has not deployed them yet returns exactly what it always did, and this client must keep
    working against it rather than requiring a newer server.  An absent or unusable ``plan``
    therefore yields ``{}``, which leaves whatever the caller already knew in place.

    Nothing here is authority.  The cloud authorizes every paid call regardless of what
    this record says; persisting it only saves the dashboard from guessing.
    """

    if not isinstance(response, dict):
        return {}
    plan = response.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        return {}
    plan = plan.strip().lower()[:_MAX_PLAN_CHARS]
    active = response.get("cloud_access_active")
    declared = {
        "plan": plan,
        # Absent is not "inactive".  Defaulting a paid plan to ``False`` would re-lock a
        # paying customer against a server that reports the plan but not the flag.
        "cloud_access_active": (
            bool(active) if isinstance(active, bool) else plan not in _UNPAID_PLANS
        ),
        "entitlement_checked_at": time.time(),
    }
    features = response.get("cloud_features")
    if isinstance(features, (list, tuple)):
        declared["cloud_features"] = sorted({
            item.strip().lower()[:_MAX_FEATURE_CHARS]
            for item in features if isinstance(item, str) and item.strip()
        })[:_MAX_FEATURES]
    return declared


def saved_entitlement() -> dict:
    """Return the entitlement persisted from the last registration or refresh, or ``{}``.

    This is the client's primary plan answer: it rides the two calls the client already
    makes, so it needs no extra request and is refreshed whenever any cloud feature is used.
    Reads state only — no network, no lock, and never raises, because the dashboard's plan
    badge is on the boot path.
    """

    try:
        saved = _load()
        declared = _declared_entitlement(saved)
        if not declared:
            return {}
        try:
            checked_at = float(saved.get("entitlement_checked_at") or 0.0)
        except (TypeError, ValueError, OverflowError):
            checked_at = 0.0
        declared["entitlement_checked_at"] = checked_at
        declared["organization_id"] = str(saved.get("organization_id") or "")
        return declared
    except Exception:  # noqa: BLE001 — an unreadable session is simply "nothing known yet"
        return {}


def record_billing_denial() -> bool:
    """Mark the saved entitlement inactive after an authoritative billing denial.

    A lapsed subscription answers ``402`` on token refresh. That is a *billing* answer, not
    a transport failure, and the saved session outranks the entitlement cache — so leaving
    ``cloud_access_active`` true kept a dashboard advertising paid features indefinitely
    while every hosted call was denied. Persisting the denial is what stops the two license
    surfaces disagreeing.

    The plan name is deliberately kept so the UI can still say which plan lapsed; only the
    access flag and the grants are cleared. Never raises: this runs on the boot path.

    A denial is also an *authoritative entitlement read*, so it stamps
    ``entitlement_checked_at`` — on the repeat denial too, which is the steady state for a
    lapsed account. Leaving the old timestamp in place kept ``saved_entitlement()``
    answering with a stale ``entitlement_checked_at``, so the caller's refresh interval
    never suppressed anything: every ``/api/license`` and ``/api/bootstrap``, in every
    worker, spent and rotated the refresh credential again against a control plane that had
    already answered 402. Advancing the clock is what bounds that.

    Returns whether this denial newly revoked access; a repeat denial returns ``False`` even
    though the timestamp was rewritten, so a caller can still tell the two apart.
    """

    try:
        # Under the same lock ``access_for_workspace`` rotates the credential with. This is a
        # load-modify-save on the shared session file: unguarded, it could read the old
        # single-use refresh credential while another worker was mid-rotation and then write
        # that stale value back over the rotated one. The next hosted call would present a
        # spent credential, which the control plane treats as replay and answers by revoking
        # the whole credential family -- turning a lapsed subscription into a forced
        # reconnect.
        with _refresh_lock():
            saved = _load()
            if not saved:
                return False
            already_denied = (
                saved.get("cloud_access_active") is False
                and not saved.get("cloud_features")
            )
            saved["cloud_access_active"] = False
            saved["cloud_features"] = []
            saved["entitlement_checked_at"] = time.time()
            # Inside the lock: a save that lands after release is exactly the race above.
            _save(saved)
            return not already_denied
    except Exception:
        return False


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
    # Whatever entitlement the control plane volunteered, so the dashboard knows the plan
    # from the first boot instead of inferring it. Absent on an older cloud; harmless.
    value.update(_declared_entitlement(response))
    with _refresh_lock():
        _save(value)


def _refresh_http_error(status: int) -> CloudSessionError:
    """Map a control-plane status to fixed, actionable public copy.

    Only the status is used: provider bodies are untrusted and may carry credentials or
    internal URLs.  Billing and authorization failures must stay distinguishable from an
    outage -- a lapsed subscription reported as "temporarily unavailable" makes a paying
    customer retry forever instead of being sent to the one page that fixes it.
    """

    if status in {401, 403}:
        return CloudSessionError(
            "The cloud session expired or was revoked; connect again.", status=status
        )
    if status == 402:
        return CloudSessionError(
            "This Engraphis Cloud subscription is not active. Update billing at %s to "
            "restore Pro and Team features." % upgrade_url(),
            status=402,
        )
    if status == 404:
        return CloudSessionError(
            "This installation is no longer registered with Engraphis Cloud; "
            "connect again.",
            status=409,
        )
    if status == 429:
        return CloudSessionError(
            "Engraphis Cloud is temporarily busy. Try again shortly.", status=429
        )
    return CloudSessionError("Engraphis Cloud could not refresh this session.")


def _post_refresh(control_url: str, refresh: str, workspace_id: Optional[str],
                  token_subject: str) -> dict:
    # An org-scoped entitlement read asks for an unbound token, so it passes no workspace.
    # Serializing that as ``"workspace_id": null`` invites a 4xx from any control plane that
    # requires the field to be a string; omit the key instead of sending an empty value.
    body = {"refresh_credential": refresh, "token_subject": token_subject}
    if workspace_id:
        body["workspace_id"] = workspace_id
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
        with build_pinned_https_opener(_NoRedirect()).open(
            request, timeout=10.0
        ) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        # Draining and closing the error body can itself time out or reset.  A sibling
        # ``except`` clause of this ``try`` does NOT cover an exception raised inside this
        # handler, so an unguarded read escapes as an unhandled traceback whenever the
        # cloud is flaky -- exactly the launch-day condition this path exists for.
        try:
            exc.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError):
            pass
        finally:
            try:
                exc.close()
            except (OSError, ValueError):
                pass
        raise _refresh_http_error(code)
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
    workspace_id: Optional[str], *, require_compute: bool = True
) -> Tuple[str, str, str]:
    """Return ``(access_token, organization_id, compute_url)`` for a bound workspace.

    ``workspace_id`` may be ``None`` for an org-scoped read that deliberately wants an
    unbound token; the refresh body then omits the field rather than sending ``null``.
    """

    direct_token = os.environ.get("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "").strip()
    direct_org = os.environ.get("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "").strip()
    direct_compute = os.environ.get("ENGRAPHIS_CLOUD_COMPUTE_URL", "").strip()
    if direct_token and direct_org and (direct_compute or not require_compute):
        compute_url = _reachable_cloud_base_url(direct_compute) if direct_compute else ""
        return direct_token, direct_org, compute_url

    # Do not create the owner-only state directory merely to report an unconnected
    # installation.  An absent session yields the normal structured "connect first"
    # response; a stale home-directory mount yields a structured, retryable error from
    # ``_load`` rather than an unhandled filesystem exception.  The authoritative session
    # record is still loaded again under the lock below before any credential is used.
    if not configured(require_compute=require_compute):
        raise CloudSessionError(
            "Connect this installation to Engraphis Cloud first.", status=401
        )

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
            raise CloudSessionError(
                "Connect this installation to Engraphis Cloud first.", status=401
            )
        control = _reachable_cloud_base_url(control)
        compute = _reachable_cloud_base_url(compute) if compute else ""
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
        # The refresh response carries the same entitlement fields as registration, so the
        # plan re-confirms itself on every token rotation. An older cloud omits them and
        # the previously persisted answer (if any) is left untouched.
        declared = _declared_entitlement(body)
        if declared and "cloud_features" not in declared:
            # A *plan change* may never inherit the previous plan's grants.
            # ``_declared_entitlement`` omits ``cloud_features`` whenever the body carried
            # no feature list, so merging it onto the saved record left a Team feature list
            # alive underneath a downgraded Pro plan and kept the Team tab unlocked
            # indefinitely. Dropping the stale key hands the answer back to this client's
            # own plan table, which is right for the plan the cloud just named. A refresh
            # that re-confirms the *same* plan still keeps the richer saved list.
            previous = str(saved.get("plan") or "").strip().lower()
            if previous != declared["plan"]:
                updated.pop("cloud_features", None)
        updated.update(declared)
        _save(updated)
        return access, organization_id, compute
