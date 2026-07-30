"""Private-state handling for short-lived Engraphis Cloud access tokens.

The cloud control plane returns a refresh credential once. The open client stores it in the
same owner-only state directory as other machine credentials, rotates it on every refresh, and
never writes it to project configuration or logs.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
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
    account_url,
    build_pinned_https_opener,
    validate_cloud_base_url,
)
from engraphis.private_state import (
    UnsafeStateFile,
    atomic_private_text,
    ensure_private_dir,
    private_file_stat,
    read_private_text,
)

_MAX_RESPONSE_BYTES = 64 * 1024
_REFRESH_THREAD_LOCK = threading.RLock()
_UNUSABLE_REFRESHES: set[tuple[str, str]] = set()


class CloudSessionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int = 503,
        refresh_unusable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.refresh_unusable = refresh_unusable


def _refresh_digest(value: str) -> str:
    """Return the non-secret identity used to retire a spent refresh credential."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _refresh_identity(value: str) -> tuple[str, str]:
    return str(_session_path()), _refresh_digest(value)


def _refresh_is_unusable(saved: dict, value: str) -> bool:
    if not value:
        return False
    persisted = str(saved.get("refresh_unusable_digest") or "")
    return bool(
        (persisted and hmac.compare_digest(persisted, _refresh_digest(value)))
        or _refresh_identity(value) in _UNUSABLE_REFRESHES
    )


def _mark_refresh_unusable(saved: dict, value: str) -> None:
    """Prevent replay after a response that may have spent the one-time credential."""

    if value:
        _UNUSABLE_REFRESHES.add(_refresh_identity(value))
    updated = dict(saved)
    updated.pop("refresh_credential", None)
    updated["refresh_unusable"] = True
    updated["refresh_unusable_at"] = time.time()
    # Retire only the credential that may have been spent. Keeping the raw secret out of
    # state lets an operator safely replace a failed environment bootstrap credential.
    updated["refresh_unusable_digest"] = _refresh_digest(value)
    _save(updated)


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
    """Return the immutable subject bound to the current credential family.

    ``ENGRAPHIS_CLOUD_TOKEN_SUBJECT`` selects the subject for an environment-only
    bootstrap credential.  Once a successful bootstrap has persisted a credential,
    its subject is part of that credential's server-side contract.  Letting a later
    environment override win causes the next refresh to present (for example) a
    member credential as ``device``; the service must reject that mismatch, and the
    client then has to retire a credential that was never actually spent.  Persisted
    state therefore wins whenever it carries a subject.
    """

    persisted = saved.get("token_subject")
    if persisted is not None and str(persisted).strip():
        return _validated_token_subject(persisted)
    configured = os.environ.get("ENGRAPHIS_CLOUD_TOKEN_SUBJECT", "").strip()
    return _validated_token_subject(configured or "member")


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


#: Exactly the flags ``_refresh_lock`` opens the lock with, so ``preflight_save`` cannot
#: drift from the access the real save needs.
_LOCK_OPEN_FLAGS = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


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
            ensure_private_dir(lock_path.parent)
            expected = private_file_stat(lock_path, allow_missing=True)
            flags = _LOCK_OPEN_FLAGS
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
    ensure_private_dir(path.parent)
    atomic_private_text(
        path, json.dumps(value, sort_keys=True, separators=(",", ":")), harden_parent=True,
    )


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
        ensure_private_dir(path.parent)
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
    # ``private_file_stat`` above is an ``lstat``: it proves the lock is a plain, private,
    # unlinked file, not that this process can *open* it.  ``_refresh_lock`` opens it
    # ``O_RDWR``, so a lock left behind by another UID -- or one whose mode changed --
    # passes the stat and fails the save, after the token is spent.  Open it here with the
    # same flags when it already exists.  When it does not, ``_refresh_lock`` creates it in
    # a directory the probe below proves writable, so there is nothing to pre-check and
    # nothing is created here.
    lock_path = _refresh_lock_path()
    if lock_path.exists():
        try:
            descriptor = os.open(str(lock_path), _LOCK_OPEN_FLAGS)
        except OSError as exc:
            raise CloudSessionError(
                "The cloud session refresh lock %s exists but cannot be opened; check its "
                "ownership and permissions." % lock_path,
                status=409,
            ) from exc
        os.close(descriptor)
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
    except OSError as exc:
        # Not cosmetic, and not "the probe was just created so of course it can be
        # removed": creating a file and removing one are separate rights.  A directory ACL
        # that grants add-file but denies delete lets ``mkstemp`` succeed and ``unlink``
        # fail, and ``atomic_private_text`` finishes with ``os.replace`` over the session
        # leaf -- which needs exactly the delete/replace right that just failed.  Swallowing
        # this let the preflight pass on a directory the real save cannot use, redeeming the
        # single-use token before failing, and left the probe behind on every attempt.  That
        # is precisely the drift the preflight exists to prevent.
        raise CloudSessionError(
            "The Engraphis state directory %s does not allow files to be replaced, so the "
            "cloud session cannot be saved there. Check its permissions; a leftover %s may "
            "need removing." % (path.parent, Path(probe).name),
            status=409,
        ) from exc
    return path


#: Plans that carry no paid cloud access, used only to default an absent activity flag.
_UNPAID_PLANS = ("free", "local")
#: Statuses that contradict "still active". Used only to refuse the optimistic default
#: for an *absent* ``cloud_access_active`` -- never as an allow-list, and never to
#: override an explicit boolean the control plane did send.
_TERMINAL_STATUSES = frozenset({"canceled", "cancelled", "expired", "revoked", "inactive"})
#: Entitlement status vocabulary the control plane can persist or compute
#: (``the hosted entitlement contract`` the hosted status contract, plus every provider status
#: the hosted subscription endpoint accepts). Kept as a bound, not an allow-list: an
#: unrecognised value is stored verbatim so a future server release is not mistranslated,
#: it is only length-capped like every other presentation string here.
_MAX_STATUS_CHARS = 32
#: An ISO-8601 timestamp is at most a few dozen characters; anything longer is not one.
_MAX_TIMESTAMP_CHARS = 64
#: Bounds on the entitlement fields before they are written into the session record. The
#: record is read back under a 64 KiB private-state cap, so an unbounded provider value
#: could grow the file past that cap and make the whole session permanently unreadable.
#: These are presentation strings; anything longer is not a plan name or a feature key.
_MAX_PLAN_CHARS = 64
_MAX_FEATURES = 32
_MAX_FEATURE_CHARS = 64
#: Every entitlement key ``_declared_entitlement`` can put on the session record. The
#: record is deliberately shaped like the wire response so one reader serves both, which
#: is what keeps a saved answer and a fresh one from being parsed by different rules.
_ENTITLEMENT_KEYS = (
    "plan",
    "cloud_access_active",
    "cloud_features",
    "status",
    "is_trial",
    "trial_consumed",
    "trial_ends_at",
)


def _declared_entitlement(response: object) -> dict:
    """Return the entitlement fields a control-plane response carried, or ``{}``.

    the hosted registration response — the body both the hosted registration endpoint and
    ``POST /v1/tokens/refresh`` answer with — carries ``plan``, ``cloud_features``,
    ``cloud_access_active``, and the trial facts (``status``, ``is_trial``,
    ``trial_consumed``, ``trial_ends_at``).  They are read as *optional* on purpose: a
    control plane that has not deployed them yet returns exactly what it always did, and
    this client must keep working against it rather than requiring a newer server.  An
    absent or unusable ``plan`` therefore yields ``{}``, which leaves whatever the caller
    already knew in place.

    The trial fields are each optional *individually* as well, because they shipped after
    the plan fields did: a server that answers ``plan`` but not ``is_trial`` simply leaves
    the key absent, and the caller keeps treating the customer as a non-trialist rather
    than claiming a trial nobody declared.

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
    # Compatibility is for an *omitted* field from a control plane that predates this
    # disclosure.  An explicitly malformed field is not an older-server response: treating
    # ``"false"`` (or ``0``) as absent would take the optimistic compatibility path and
    # render a paid entitlement live.  Keep the last good persisted answer instead.
    if "cloud_access_active" in response and not isinstance(active, bool):
        return {}
    named = response.get("status")
    named = named.strip().lower() if isinstance(named, str) else ""
    declared = {
        "plan": plan,
        # Absent is not "inactive".  Defaulting a paid plan to ``False`` would re-lock a
        # paying customer against a server that reports the plan but not the flag.  But
        # the optimistic default must not survive a body that says, in the same breath,
        # that the entitlement is over: ``{"plan": "team", "status": "revoked"}`` was
        # being read as active team access.
        "cloud_access_active": (
            bool(active) if isinstance(active, bool)
            else plan not in _UNPAID_PLANS and named not in _TERMINAL_STATUSES
        ),
        "entitlement_checked_at": time.time(),
    }
    features = response.get("cloud_features")
    if isinstance(features, (list, tuple)):
        declared["cloud_features"] = sorted({
            item.strip().lower()[:_MAX_FEATURE_CHARS]
            for item in features if isinstance(item, str) and item.strip()
        })[:_MAX_FEATURES]
    # The trial half of the same answer.  Absent stays absent so a field-less older server
    # cannot erase what a newer one already recorded, exactly as ``cloud_features`` behaves.
    status = response.get("status")
    if isinstance(status, str) and status.strip():
        declared["status"] = status.strip().lower()[:_MAX_STATUS_CHARS]
    for key in ("is_trial", "trial_consumed"):
        value = response.get(key)
        if isinstance(value, bool):
            declared[key] = value
    ends_at = response.get("trial_ends_at")
    if isinstance(ends_at, str) and ends_at.strip():
        declared["trial_ends_at"] = ends_at.strip()[:_MAX_TIMESTAMP_CHARS]
    elif ends_at is None and "is_trial" in declared and not declared["is_trial"]:
        # An explicit ``null`` from a server that *did* answer the trial question is
        # meaningful: it is how a converted paying customer is told they have no live trial
        # boundary. Clear a stale one rather than letting a past trial end survive forever.
        declared["trial_ends_at"] = ""
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
            # The last status the server named ("active", "trialing", …) is now known to
            # contradict this denial, so it must not survive as renderable copy. The trial
            # facts are *not* invalidated by a lapse -- whether this was a trial, when it
            # ended, and whether one was consumed are all still true -- and they are what
            # lets the dashboard say "your free trial ended" rather than the generic
            # "your subscription lapsed".
            saved.pop("status", None)
            saved["entitlement_checked_at"] = time.time()
            # Inside the lock: a save that lands after release is exactly the race above.
            _save(saved)
            return not already_denied
    except Exception:
        return False


def text_field(response: dict, key: str) -> str:
    """Return ``response[key]`` when it is a string, else ``""``.  Never a ``repr``.

    ``str(response.get(key) or "")`` looks like a coercion but is not a validation: JSON
    arrays and objects arrive as Python ``list``/``dict``, and ``str()`` renders their
    *repr*, which is both truthy and non-empty.  A control-plane reply carrying
    ``"refresh_credential": ["tok"]`` was therefore stored as the literal text ``['tok']``;
    ``configured()`` read that back as a usable session and connect reported success, while
    the next refresh submitted the junk and failed -- after the single-use connect token had
    already been spent, so the customer could not simply retry.

    Provider bodies are untrusted, so a field that must be a string is required to be one.
    """

    value = response.get(key)
    return value.strip() if isinstance(value, str) else ""


def save_bootstrap(response: dict, *, control_url: str,
                   compute_url: Optional[str] = None) -> None:
    """Persist the one-time bootstrap/refresh material returned by the control plane."""

    refresh = text_field(response, "refresh_credential")
    organization_id = text_field(response, "organization_id")
    if not refresh or not organization_id:
        raise CloudSessionError("Cloud bootstrap did not return a refresh credential.")
    value = {
        "schema": "engraphis-cloud-session/v1",
        "control_url": validate_cloud_base_url(control_url),
        "compute_url": validate_cloud_base_url(compute_url) if compute_url else "",
        "organization_id": organization_id,
        "installation_id": text_field(response, "installation_id"),
        "device_id": text_field(response, "device_id"),
        "member_id": text_field(response, "member_id"),
        "refresh_credential": refresh,
        "refresh_expires_at": text_field(response, "refresh_expires_at"),
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
            "The cloud session expired or was revoked; connect again.",
            status=status,
            refresh_unusable=True,
        )
    if status == 402:
        return CloudSessionError(
            "This Engraphis Cloud subscription is not active. Update billing at %s to "
            "restore Pro and Team features." % account_url(),
            status=402,
        )
    if status == 404:
        return CloudSessionError(
            "This installation is no longer registered with Engraphis Cloud; "
            "connect again.",
            status=409,
            refresh_unusable=True,
        )
    if status == 429:
        return CloudSessionError(
            "Engraphis Cloud is temporarily busy. Try again shortly.", status=429
        )
    return CloudSessionError("Engraphis Cloud could not refresh this session.")


#: What a best-effort drain of an error body is allowed to fail with.  See the comment in
#: :func:`_post_refresh`; ``engraphis.device_connect`` guards its drain with the same tuple.
_DRAIN_FAILURES = (OSError, ValueError, http.client.HTTPException)


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
    # Split for the same reason as ``device_connect.post_connect``, and with sharper
    # consequences here.  Once ``open`` returns, a success status line has been parsed, so
    # the control plane processed the refresh and the single-use credential it was given is
    # spent -- but the rotated replacement only reaches disk after the body parses, below.
    try:
        response = build_pinned_https_opener(_NoRedirect()).open(request, timeout=10.0)
    except urllib.error.HTTPError as exc:
        code = exc.code
        # Draining and closing the error body can itself time out or reset.  A sibling
        # ``except`` clause of this ``try`` does NOT cover an exception raised inside this
        # handler, so an unguarded read escapes as an unhandled traceback whenever the
        # cloud is flaky -- exactly the launch-day condition this path exists for.
        #
        # ``HTTPException`` is named explicitly: a truncated chunked error body raises
        # ``http.client.IncompleteRead``, whose MRO is ``(IncompleteRead, HTTPException,
        # Exception, BaseException, object)`` -- neither an ``OSError`` nor a ``ValueError``,
        # so the pair alone let it through.
        try:
            exc.read(_MAX_RESPONSE_BYTES + 1)
        except _DRAIN_FAILURES:
            pass
        finally:
            try:
                exc.close()
            except _DRAIN_FAILURES:
                pass
        raise _refresh_http_error(code)
    # urllib wraps failures before or while sending the request in URLError.  That is the one
    # distinguishable pre-send path, so the credential was not spent and retry remains safe.
    except urllib.error.URLError as exc:
        raise CloudSessionError("Engraphis Cloud is temporarily unreachable.") from exc
    except (TimeoutError, http.client.RemoteDisconnected, OSError) as exc:
        # These escape directly from getresponse() after urllib wrote the POST.  The control
        # plane may have spent the single-use credential even though no status line arrived;
        # retrying the unchanged on-disk value risks revoking its entire credential family.
        raise CloudSessionError(
            "Engraphis Cloud did not complete this refresh response, so the rotated "
            "credential could not be saved. Connect this installation again.",
            status=409,
            refresh_unusable=True,
        ) from exc
    except (http.client.BadStatusLine, http.client.LineTooLong) as exc:
        # ``getresponse()`` raises these only after urllib has sent the POST.  The control
        # plane may therefore have consumed the single-use refresh credential, while its
        # replacement never reached disk.  Retrying would replay the stale credential and can
        # revoke its whole family, so prefer reconnecting over an unsafe transient retry.
        # ``RemoteDisconnected`` is also a ``BadStatusLine`` but reaches the earlier OSError
        # transport clause through its ``ConnectionResetError`` base.
        raise CloudSessionError(
            "Engraphis Cloud returned a malformed refresh response, so the rotated "
            "credential could not be saved. Connect this installation again.",
            status=409,
            refresh_unusable=True,
        ) from exc
    except http.client.HTTPException as exc:
        # Other malformed HTTP replies have no useful protocol status, but unlike a malformed
        # status line they do not establish that this request reached the control plane.
        raise CloudSessionError("Engraphis Cloud is temporarily unreachable.") from exc

    try:
        with response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # Post-response, and therefore NOT a transient outage. The server answered, so the
        # credential just submitted is spent, but the rotation it returned never reached
        # ``_save`` -- the stale value is still on disk. ``_public_session_error`` maps 503
        # to ``transient=True``, which ``CloudFeatureClient.run_job`` acts on by retrying;
        # that retry would resubmit the spent credential, and this module already documents
        # (see ``_note_denied``) that the control plane treats replay by revoking the whole
        # credential family. 409 is the existing "saved session is unusable; connect this
        # installation again" bucket, which is the honest answer here.
        #
        # This deliberately prefers a false "reconnect" over a replay: if the truncation
        # happened before the server committed the rotation the old credential was still
        # good and the reconnect was unnecessary, but the opposite mistake revokes every
        # credential in the family and forces the same reconnect anyway, from a worse state.
        raise CloudSessionError(
            "Engraphis Cloud answered this session refresh but the reply was incomplete, "
            "so the rotated credential could not be saved. Connect this installation "
            "again.",
            status=409,
            refresh_unusable=True,
        ) from exc

    # These are post-response too, so they carry the same replay hazard as the truncated
    # body above and take the same non-transient status: the server consumed the credential
    # it was given, and a body this client cannot parse means the rotation never landed.
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise CloudSessionError(
            "Engraphis Cloud returned an oversized session response, so the rotated "
            "credential could not be saved. Connect this installation again.",
            status=409,
            refresh_unusable=True,
        )
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CloudSessionError(
            "Engraphis Cloud returned an invalid session response, so the rotated "
            "credential could not be saved. Connect this installation again.",
            status=409,
            refresh_unusable=True,
        ) from exc
    if not isinstance(body, dict):
        raise CloudSessionError(
            "Engraphis Cloud returned an invalid session response, so the rotated "
            "credential could not be saved. Connect this installation again.",
            status=409,
            refresh_unusable=True,
        )
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
    if _refresh_is_unusable(saved, refresh):
        refresh = ""
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
        if _refresh_is_unusable(saved, refresh):
            raise CloudSessionError(
                "The saved cloud refresh credential cannot be reused; connect this "
                "installation again.",
                status=409,
            )
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
        try:
            body = _post_refresh(control, refresh, workspace_id, token_subject)
        except CloudSessionError as exc:
            if exc.refresh_unusable:
                _mark_refresh_unusable(saved, refresh)
            raise
        # Same untrusted-provider boundary as ``save_bootstrap``: a non-string credential
        # would otherwise be stored as its ``repr`` and submitted on the next refresh.
        access = text_field(body, "access_token")
        organization_id = (
            text_field(body, "organization_id") or text_field(saved, "organization_id")
        )
        rotated = text_field(body, "refresh_credential")
        if not access or not organization_id or not rotated:
            # Also post-response: the submitted credential is spent and no rotation was
            # saved, so this must not be reported as a retryable outage either.
            _mark_refresh_unusable(saved, refresh)
            raise CloudSessionError(
                "Engraphis Cloud returned incomplete session credentials, so the rotated "
                "credential could not be saved. Connect this installation again.",
                status=409,
                refresh_unusable=True,
            )
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
            "refresh_expires_at": text_field(body, "refresh_expires_at"),
            "token_subject": response_subject,
        })
        updated.pop("refresh_unusable", None)
        updated.pop("refresh_unusable_at", None)
        updated.pop("refresh_unusable_digest", None)
        # The refresh response carries the same entitlement fields as registration, so the
        # plan re-confirms itself on every token rotation. An older cloud omits them and
        # the previously persisted answer (if any) is left untouched.
        declared = _declared_entitlement(body)
        if declared:
            # A *plan change* may never inherit the previous plan's state.
            # ``_declared_entitlement`` omits any field the body did not carry, so merging
            # it onto the saved record left a Team feature list alive underneath a
            # downgraded Pro plan and kept the Team tab unlocked indefinitely. Dropping
            # every entitlement key the new answer did not restate hands those back to this
            # client's own defaults, which are right for the plan the cloud just named --
            # and stops a finished trial's ``is_trial``/``trial_ends_at`` surviving under
            # the paid plan it converted into. A refresh that re-confirms the *same* plan
            # still keeps the richer saved answer.
            previous = str(saved.get("plan") or "").strip().lower()
            if previous != declared["plan"]:
                for key in _ENTITLEMENT_KEYS:
                    if key not in declared:
                        updated.pop(key, None)
        updated.update(declared)
        try:
            _save(updated)
        except (OSError, RuntimeError) as exc:
            # The control plane has already consumed ``refresh``.  Leaving that stale
            # value usable after a local write fault makes the next request replay it,
            # which can revoke the credential family.  Retire it in memory first (so this
            # process cannot replay it even when the state mount remains broken), then
            # make a best-effort persisted retirement for a fault that was transient.
            # The original write is deliberately never retried: it contains a replacement
            # credential that may have reached disk only partially on an exotic mount.
            _UNUSABLE_REFRESHES.add(_refresh_identity(refresh))
            try:
                _mark_refresh_unusable(saved, refresh)
            except Exception:  # noqa: BLE001 - the state store is already failing
                pass
            raise CloudSessionError(
                "Engraphis Cloud refreshed this session but the rotated credential "
                "could not be saved. Connect this installation again.",
                status=409,
                refresh_unusable=True,
            ) from exc
        return access, organization_id, compute
