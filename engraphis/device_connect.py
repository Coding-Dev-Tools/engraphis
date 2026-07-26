"""Client half of the Engraphis Cloud device-connect flow.

The account portal issues a one-time connect token (``engr_ct_...``) and shows the
customer a single command.  This module exchanges that token for the durable session
material :func:`engraphis.cloud_session.save_bootstrap` persists, so
``~/.engraphis/cloud_session.json`` -- the file :doc:`AGENT_CONNECT` and ``.env.example``
tell paying customers to prefer -- is finally created by something.  Before this existed
``save_bootstrap`` had no production caller at all and a paid installation could not be
connected without hand-writing the state file.

Design constraints, all of which have teeth:

* **The connect token is a bearer credential.**  It travels in the request body and
  nowhere else: never in a log line, never in an exception message, never in the saved
  session, never echoed back to the terminal.  Callers get a redacted summary.
* **The vetted transport only.**  Requests go through
  :func:`engraphis.hosted_client.build_pinned_https_opener` with redirects blocked and an
  explicit timeout, exactly as :mod:`engraphis.cloud_session` and
  :mod:`engraphis.update_check` do.  A bare ``urllib.request.build_opener`` would drop the
  DNS-rebinding guard on a credential-bearing call.
* **Fixed copy keyed on status.**  Provider bodies are untrusted -- they may carry
  internal URLs -- so nothing from the response is reflected into an error message.  The
  control plane deliberately makes expired / consumed / invalid tokens indistinguishable
  (all ``401``), so the client says all three at once rather than guessing.
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from engraphis import cloud_session
from engraphis.hosted_client import (
    CloudUrlUnresolved,
    build_pinned_https_opener,
    upgrade_url,
    validate_cloud_base_url,
)
from engraphis.private_state import (
    UnsafeStateFile,
    atomic_private_text,
    publish_private_text_if_absent,
    read_private_text,
)

try:  # installed distribution -> real version; source tree -> harmless fallback
    from engraphis import __version__ as CURRENT_VERSION
except Exception:  # pragma: no cover - engraphis is always importable in practice
    CURRENT_VERSION = "0"

#: Path segment of the unauthenticated connect endpoint on the control plane.
CONNECT_PATH = "/v1/devices/connect"

#: One interactive request.  Longer than the 10s refresh budget because this runs at a
#: human's prompt, once, and a spurious timeout costs the customer a fresh token.
DEFAULT_TIMEOUT_SECONDS = 15.0

#: The control plane answers with a small fixed record; anything larger is not ours.
_MAX_RESPONSE_BYTES = 64 * 1024

#: The portal always mints tokens with this prefix.  Checking it locally turns a
#: mistyped or truncated paste into an instant, free error instead of a round trip that
#: consumes rate budget and returns the same opaque 401 as a genuinely dead token.
CONNECT_TOKEN_PREFIX = "engr_ct_"
#: Shortest credible token: the prefix plus enough entropy to be a real secret.
_MIN_TOKEN_CHARS = len(CONNECT_TOKEN_PREFIX) + 16
_MAX_TOKEN_CHARS = 512
#: ``secrets.token_urlsafe`` alphabet.  Anything else is a paste accident (a shell-
#: mangled quote, a wrapped line, a whole command copied in).
_TOKEN_BODY = re.compile(r"\A[A-Za-z0-9_-]+\Z")

#: Identity file for this installation.  Kept beside ``cloud_session.json`` in the same
#: owner-only state directory and honouring the same ``ENGRAPHIS_STATE_DIR`` override.
_IDENTITY_FILENAME = "client_identity.json"
_IDENTITY_SCHEMA = "engraphis-client-identity/v1"

#: Compute endpoint for the production deployment.  ``DeviceRegistrationResponse`` does
#: not carry one, and ``cloud_session.configured()`` requires it, so a connect against the
#: shipped control plane would otherwise leave a customer "connected" but not configured.
#: Applied *only* when the control plane is the shipped one: a self-hosted or staging
#: control URL gets no guess, and the caller is told to supply the compute URL.
DEFAULT_COMPUTE_URL = "https://compute.engraphis.com"


class DeviceConnectError(RuntimeError):
    """A connect attempt failed with public, actionable copy.

    ``status`` mirrors the control-plane status where there was one, and uses ``400`` for
    a request this client refused to send at all.
    """

    def __init__(self, message: str, *, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a 3xx would replay the connect token at an unvetted host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_connect_token(value: object) -> str:
    """Return the trimmed token, or raise before any network call happens.

    Never quotes the offending value.  A rejected token is still a credential -- an
    error message containing it would land in shell history, CI logs and bug reports.
    """

    token = str(value or "").strip()
    if not token:
        raise DeviceConnectError(
            "No connect token was supplied. Copy the `engraphis connect --token ...` "
            "command shown in your Engraphis account portal.",
            status=400,
        )
    if (
        not token.startswith(CONNECT_TOKEN_PREFIX)
        or len(token) < _MIN_TOKEN_CHARS
        or len(token) > _MAX_TOKEN_CHARS
        or not _TOKEN_BODY.match(token[len(CONNECT_TOKEN_PREFIX):])
    ):
        raise DeviceConnectError(
            "That does not look like an Engraphis connect token (they start with "
            "`%s`). Copy the whole command from your account portal -- a token split "
            "across lines or missing its last characters will not work."
            % CONNECT_TOKEN_PREFIX,
            status=400,
        )
    return token


def _state_dir() -> Path:
    root = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    return Path(root).expanduser() if root else Path.home() / ".engraphis"


def _identity_path() -> Path:
    return _state_dir() / _IDENTITY_FILENAME


def _new_client_id(prefix: str) -> str:
    # The package's own ULID minter, so these ids sort chronologically and read the same
    # way as every other prefixed id in the client.
    from engraphis.core import ids

    return "%s_%s" % (prefix, ids.ulid())


def _read_identity() -> dict:
    try:
        raw = read_private_text(_identity_path(), max_bytes=8 * 1024, allow_missing=True)
    except UnsafeStateFile as exc:
        raise DeviceConnectError(
            "The saved client identity has unsafe filesystem permissions. Remove %s and "
            "connect again." % _identity_path(),
            status=409,
        ) from exc
    except (OSError, RuntimeError):
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def client_identity() -> Tuple[str, str]:
    """Return ``(installation_client_id, device_client_id)`` for this machine.

    Minted once and persisted at ``<state dir>/client_identity.json`` with owner-only
    permissions, so reconnecting the same machine re-presents the same pair and the
    control plane recognises the installation instead of accumulating a new phantom
    device on every connect.

    They are random ULIDs rather than a hardware fingerprint on purpose: a MAC- or
    hostname-derived id is a stable cross-account identifier the customer never agreed to
    ship, and it changes under exactly the conditions (a new NIC, a rename) where
    stability was the point.  "Stable per installation" is what the server needs, and a
    minted-once file in the state directory is precisely that.  A second state directory
    (``ENGRAPHIS_STATE_DIR``, a container, another user account) is a second installation,
    which is the correct reading.
    """

    saved = _read_identity()
    installation = str(saved.get("installation_client_id") or "").strip()
    device = str(saved.get("device_client_id") or "").strip()
    if installation and device:
        return installation, device

    minted = {
        "schema": _IDENTITY_SCHEMA,
        "installation_client_id": installation or _new_client_id("inst"),
        "device_client_id": device or _new_client_id("dev"),
    }
    payload = json.dumps(minted, sort_keys=True, separators=(",", ":"))
    path = _identity_path()
    try:
        if not publish_private_text_if_absent(path, payload):
            # Another process won the create, or a half-written file already existed.
            # Re-read: the winner's ids are the ones the control plane will see.
            existing = _read_identity()
            installation = str(existing.get("installation_client_id") or "").strip()
            device = str(existing.get("device_client_id") or "").strip()
            if installation and device:
                return installation, device
            atomic_private_text(path, payload)
    except UnsafeStateFile as exc:
        raise DeviceConnectError(
            "The client identity file at %s could not be written safely." % path,
            status=409,
        ) from exc
    except OSError as exc:
        raise DeviceConnectError(
            "Could not write the client identity file at %s." % path, status=409
        ) from exc
    return minted["installation_client_id"], minted["device_client_id"]


def default_control_url() -> str:
    """Resolve the control plane: explicit env override, else the shipped manifest."""

    configured = os.environ.get("ENGRAPHIS_CLOUD_CONTROL_URL", "").strip()
    if configured:
        return configured
    try:
        from engraphis.commercial import manifest

        return str(manifest().get("control_plane") or "").strip()
    except Exception:  # pragma: no cover - manifest ships with the package
        return ""


def default_compute_url(control_url: str) -> str:
    """Resolve the compute plane, guessing only for the shipped control plane."""

    configured = os.environ.get("ENGRAPHIS_CLOUD_COMPUTE_URL", "").strip()
    if configured:
        return configured
    shipped = ""
    try:
        from engraphis.commercial import manifest

        shipped = str(manifest().get("control_plane") or "").strip()
    except Exception:  # pragma: no cover - manifest ships with the package
        return ""
    if shipped and control_url.rstrip("/") == shipped.rstrip("/"):
        return DEFAULT_COMPUTE_URL
    return ""


def _validated_control_url(value: str) -> str:
    if not str(value or "").strip():
        raise DeviceConnectError(
            "No Engraphis Cloud control URL is configured. Set "
            "ENGRAPHIS_CLOUD_CONTROL_URL or pass --control-url.",
            status=400,
        )
    try:
        return validate_cloud_base_url(value)
    except CloudUrlUnresolved as exc:
        # "Offline" is not "misconfigured": validate_cloud_base_url resolves the host, so
        # a customer on a plane would otherwise be told their URL is invalid forever.
        raise DeviceConnectError(
            "Engraphis Cloud is temporarily unreachable. Check your network and try "
            "again.",
            status=503,
        ) from exc
    except ValueError as exc:
        raise DeviceConnectError(
            "The Engraphis Cloud control URL is not a valid HTTPS endpoint.", status=400
        ) from exc


def _default_device_name() -> str:
    try:
        name = socket.gethostname().strip()
    except OSError:
        name = ""
    return name[:100]


def _default_platform() -> str:
    try:
        return ("%s %s" % (platform.system(), platform.machine())).strip()[:100]
    except Exception:  # pragma: no cover - platform is stdlib and total
        return ""


def _connect_http_error(status: int) -> DeviceConnectError:
    """Map a control-plane status to fixed, actionable copy.

    Only the status is used.  ``401`` deliberately covers expired, already-consumed and
    never-valid tokens with one indistinguishable answer, so the copy names all three
    instead of asserting one -- the fix is the same in every case.
    """

    if status == 401:
        return DeviceConnectError(
            "That connect token has expired, was already used, or is not valid. "
            "Generate a new one in your Engraphis account portal and run "
            "`engraphis connect --token ...` again.",
            status=401,
        )
    if status == 402:
        return DeviceConnectError(
            "This Engraphis Cloud subscription is not active, so no new device can be "
            "connected. Update billing at %s and try again." % upgrade_url(),
            status=402,
        )
    if status == 403:
        return DeviceConnectError(
            "Engraphis Cloud refused this connect request. Check with the organization "
            "owner that your account may still add devices.",
            status=403,
        )
    if status == 404:
        return DeviceConnectError(
            "This Engraphis Cloud control plane has no device-connect endpoint. Check "
            "ENGRAPHIS_CLOUD_CONTROL_URL points at the URL shown in your account portal.",
            status=404,
        )
    if status == 422:
        return DeviceConnectError(
            "Engraphis Cloud rejected this connect request as malformed. Upgrade the "
            "client (`pip install -U engraphis`) and try again.",
            status=422,
        )
    if status == 429:
        return DeviceConnectError(
            "Too many connect attempts. Wait a minute and try again.", status=429
        )
    if status == 503:
        return DeviceConnectError(
            "Engraphis Cloud is not accepting new device activations right now. Try "
            "again shortly; your connect token is unaffected.",
            status=503,
        )
    return DeviceConnectError(
        "Engraphis Cloud could not connect this device. Try again shortly.", status=503
    )


def post_connect(control_url: str, token: str, *, installation_client_id: str,
                 device_client_id: str, installation_label: Optional[str] = None,
                 device_name: Optional[str] = None, app_platform: Optional[str] = None,
                 app_version: Optional[str] = None, workspace_id: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """POST the connect token and return the ``DeviceRegistrationResponse`` body.

    *control_url* must already be validated.  The endpoint rejects unknown fields with a
    ``422``, so optional values are omitted rather than sent empty, and there is
    deliberately no ``organization_id``: the token carries the organization.
    """

    body = {
        "connect_token": token,
        "installation_client_id": installation_client_id,
        "device_client_id": device_client_id,
    }
    for key, value in (
        ("installation_label", installation_label),
        ("device_name", device_name),
        ("platform", app_platform),
        ("app_version", app_version),
        ("workspace_id", workspace_id),
    ):
        cleaned = str(value or "").strip()
        if cleaned:
            body[key] = cleaned

    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        control_url + CONNECT_PATH,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Engraphis/%s device-connect" % CURRENT_VERSION,
        },
        method="POST",
    )
    try:
        with build_pinned_https_opener(_NoRedirect()).open(
            request, timeout=timeout
        ) as response:  # nosec B310 - scheme validated by validate_cloud_base_url
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        # Draining the error body can itself raise; a sibling ``except`` of this ``try``
        # would not cover it, so an unguarded read escapes as a raw traceback exactly
        # when the cloud is flaky. Same shape as cloud_session._post_refresh.
        try:
            exc.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError):
            pass
        finally:
            try:
                exc.close()
            except (OSError, ValueError):
                pass
        raise _connect_http_error(status)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # ``exc`` may quote an internal host or a proxy URL; never reflect it.
        raise DeviceConnectError(
            "Engraphis Cloud is temporarily unreachable. Check your network and try "
            "again.",
            status=503,
        ) from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise DeviceConnectError(
            "Engraphis Cloud returned an oversized connect response.", status=502
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise DeviceConnectError(
            "Engraphis Cloud returned an invalid connect response.", status=502
        ) from exc
    if not isinstance(parsed, dict):
        raise DeviceConnectError(
            "Engraphis Cloud returned an invalid connect response.", status=502
        )
    return parsed


#: Fields worth echoing back to the customer.  Deliberately excludes
#: ``refresh_credential`` and ``access_token``: the summary is printed.
_SUMMARY_FIELDS = (
    "organization_id",
    "installation_id",
    "device_id",
    "member_id",
    "workspace_id",
    "token_subject",
    "plan",
    "cloud_access_active",
    "cloud_features",
    "entitlement_version",
    "expires_in_seconds",
)


def summarize(response: dict) -> dict:
    """Return the non-secret fields of a registration response, for display."""

    summary = {}
    for key in _SUMMARY_FIELDS:
        if key in response:
            summary[key] = response[key]
    return summary


def _preflight_session_storage() -> Path:
    """Refuse a connect *before* the token is spent when the session cannot be saved.

    The exchange is the point of no return: the control plane consumes the single-use
    connect token as it answers, so any storage fault discovered afterwards costs the
    customer a fresh token from the portal.  Delegated to :mod:`engraphis.cloud_session`
    because that module owns the paths, the lock and the atomic write this is checking --
    a private copy of those rules here would drift from the save it is meant to predict.
    """

    try:
        return cloud_session.preflight_save()
    except cloud_session.CloudSessionError as exc:
        raise DeviceConnectError(
            "%s Your connect token has not been used, so you can fix this and run "
            "`engraphis connect --token ...` again with the same token." % exc,
            status=getattr(exc, "status", 409),
        ) from exc


def connect(token: object, *, control_url: Optional[str] = None,
            compute_url: Optional[str] = None, workspace_id: Optional[str] = None,
            installation_label: Optional[str] = None, device_name: Optional[str] = None,
            timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Exchange a connect token for a saved cloud session.

    Returns the redacted summary -- it is safe to print.  Raises
    :class:`DeviceConnectError` for every failure, with copy the customer can act on and
    never containing the token.  Nothing is written unless the exchange succeeded.
    """

    normalized = normalize_connect_token(token)
    resolved_control = _validated_control_url(
        control_url if control_url is not None else default_control_url()
    )
    resolved_compute = (
        compute_url if compute_url is not None else default_compute_url(resolved_control)
    )
    if resolved_compute:
        try:
            resolved_compute = validate_cloud_base_url(resolved_compute)
        except CloudUrlUnresolved as exc:
            raise DeviceConnectError(
                "The Engraphis Cloud compute endpoint is temporarily unreachable.",
                status=503,
            ) from exc
        except ValueError as exc:
            raise DeviceConnectError(
                "The Engraphis Cloud compute URL is not a valid HTTPS endpoint.",
                status=400,
            ) from exc

    installation_client_id, device_client_id = client_identity()
    # Last check before the point of no return.  ``client_identity`` may have written its
    # file minutes or months ago, so a writable state directory then is no evidence of one
    # now; prove the session can land *before* the POST spends the token, not after.
    session_path = _preflight_session_storage()
    response = post_connect(
        resolved_control,
        normalized,
        installation_client_id=installation_client_id,
        device_client_id=device_client_id,
        installation_label=installation_label,
        device_name=device_name if device_name is not None else _default_device_name(),
        app_platform=_default_platform(),
        app_version=str(CURRENT_VERSION),
        workspace_id=workspace_id,
        timeout=timeout,
    )
    if not str(response.get("refresh_credential") or "").strip():
        raise DeviceConnectError(
            "Engraphis Cloud accepted the token but returned no session credential. "
            "Try again, and contact support if it repeats.",
            status=502,
        )
    try:
        cloud_session.save_bootstrap(
            response, control_url=resolved_control, compute_url=resolved_compute or None
        )
    except cloud_session.CloudSessionError as exc:
        raise DeviceConnectError(str(exc), status=getattr(exc, "status", 503)) from exc

    summary = summarize(response)
    summary["control_url"] = resolved_control
    summary["compute_url"] = resolved_compute
    summary["session_path"] = str(session_path)
    return summary
