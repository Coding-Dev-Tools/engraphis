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

import http.client
import json
import math
import os
import platform
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from typing import Optional, Tuple

from engraphis import cloud_session
from engraphis.hosted_client import (
    CloudUrlUnresolved,
    account_url,
    build_pinned_https_opener,
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

#: Upper bound on ``--timeout``.  Not a policy number -- a platform one.  ``socket``
#: converts a timeout into an absolute deadline, so a large but *finite* value raises
#: ``OverflowError`` ("timeout doesn't fit into C timeval", "timestamp out of range for
#: platform time_t") from inside ``urllib``: ``1e9`` already overflows on CPython 3.12.
#: One hour is orders of magnitude more than a single interactive POST can need and orders
#: of magnitude below the first value any supported platform rejects.
_MAX_TIMEOUT_SECONDS = 3600.0

#: The control plane answers with a small fixed record; anything larger is not ours.
_MAX_RESPONSE_BYTES = 64 * 1024

#: Copy for a reply that began but never completed.  Every other failure in this module can
#: promise the connect token is untouched; this one cannot, because the request reached a
#: control plane that started to answer, and a 200 consumes the token as it is written.
#: Saying "try again" here would be a lie that costs the customer a second failed attempt,
#: so the copy sends them to the one place that shows whether the device landed.
_TRUNCATED_REPLY = (
    "Engraphis Cloud started answering this connect request but the connection closed "
    "before the reply was complete. Your connect token may already have been used: check "
    "your account portal, and generate a new one if this device is not listed there."
)

#: Appended to every failure that can only happen once the control plane has answered
#: successfully.  ``urllib`` raises ``HTTPError`` for every status >= 400, and ``_NoRedirect``
#: turns a 3xx into one too, so any fault past ``opener.open()`` follows a 2xx -- and a
#: successful connect consumes the single-use token as it is written.  Telling the customer
#: to "try again" there would send them into a guaranteed 401 with no session; the only
#: action that can work is a fresh token from the portal.
_SPENT_TOKEN_SUFFIX = (
    " This connect token has now been used -- generate a new one in your Engraphis account "
    "portal and try again, and contact support if it repeats."
)

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

#: Compute endpoint for the production deployment.  the hosted registration response does
#: not carry one, and ``cloud_session.configured()`` requires it, so a connect against the
#: shipped control plane would otherwise leave a customer "connected" but not configured.
#: Applied *only* when the control plane is the shipped one: a self-hosted or staging
#: control URL gets no guess, and the caller is told to supply the compute URL.
#:
#: This is the *fallback*, not the authority: ``default_compute_url`` prefers the
#: manifest's ``compute_plane`` whenever it carries one.  The shipped
#: ``engraphis-commercial/v2`` manifest does not declare that key, so without a constant
#: here a production connect would resolve an empty compute URL and save an unconfigured
#: session.
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
    try:
        return _state_dir() / _IDENTITY_FILENAME
    except RuntimeError as exc:
        # ``Path.home()`` raises ``RuntimeError("Could not determine home directory")`` on a
        # service account or container with no resolvable home.  This module promises every
        # failure is a ``DeviceConnectError``, and the CLI catches only that, so the bare
        # ``RuntimeError`` reached the terminal as a traceback -- from a command whose whole
        # selling point is being non-interactive and scriptable.  ``ENGRAPHIS_STATE_DIR`` is
        # the documented answer, so the copy names it.
        raise DeviceConnectError(
            "Engraphis could not determine a home directory for its state files. Set "
            "ENGRAPHIS_STATE_DIR to a writable directory and run `engraphis connect "
            "--token ...` again.",
            status=400,
        ) from exc


def _new_client_id(prefix: str) -> str:
    # The package's own ULID minter, so these ids sort chronologically and read the same
    # way as every other prefixed id in the client.
    from engraphis.core import ids

    return "%s_%s" % (prefix, ids.ulid())


def _read_identity() -> dict:
    try:
        raw = read_private_text(_identity_path(), max_bytes=8 * 1024, allow_missing=True)
    except DeviceConnectError:
        # Already actionable copy (an unresolvable home directory). Must come first:
        # ``DeviceConnectError`` is a ``RuntimeError``, so the broad clause below would
        # otherwise swallow it and let the *next*, unguarded ``_identity_path()`` call
        # raise the raw error instead.
        raise
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
            atomic_private_text(path, payload, harden_parent=True)
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


def _canonical_endpoint(value: object) -> str:
    """Canonical ``scheme://host:port/path`` for comparing two endpoint URLs.

    ``validate_cloud_base_url`` returns ``urlunsplit((scheme, parts.netloc, ...))``: it
    lower-cases the *scheme* but passes the netloc through verbatim, and it never removes a
    redundant default port.  So ``HTTPS://API.ENGRAPHIS.COM`` normalises to
    ``https://API.ENGRAPHIS.COM``, and ``https://api.engraphis.com:443`` keeps its ``:443``
    -- both are the shipped control plane, and both compared unequal to it as raw strings.
    The consequence was silent and expensive: :func:`default_compute_url` returned ``""``,
    so a customer who typed the production URL with a different case or an explicit port
    connected successfully and then had every hosted feature switched off, because
    ``cloud_session.configured()`` needs a compute endpoint.

    Returns ``""`` for anything unparseable, which never equals another canonical form.
    """

    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    if port is None:
        port = 443 if scheme == "https" else 80
    return "%s://%s:%d%s" % (scheme, host, port, parts.path.rstrip("/"))


def default_compute_url(control_url: str) -> str:
    """Resolve the compute plane, guessing only for the shipped control plane.

    The manifest is the authority for shipped endpoint metadata, so a ``compute_plane``
    key wins if one is ever published -- a manifest-only endpoint change then needs no
    code change here.  The current ``engraphis-commercial/v2`` manifest declares only
    ``control_plane``, so :data:`DEFAULT_COMPUTE_URL` is what production actually
    resolves; reading the absent key alone would yield ``""`` and save a session
    ``cloud_session.configured()`` rejects.
    """

    configured = os.environ.get("ENGRAPHIS_CLOUD_COMPUTE_URL", "").strip()
    if configured:
        return configured
    shipped = ""
    shipped_compute = ""
    try:
        from engraphis.commercial import manifest

        data = manifest()
        shipped = str(data.get("control_plane") or "").strip()
        shipped_compute = str(data.get("compute_plane") or "").strip()
    except Exception:  # pragma: no cover - manifest ships with the package
        return ""
    if shipped and _canonical_endpoint(control_url) == _canonical_endpoint(shipped):
        return shipped_compute or DEFAULT_COMPUTE_URL
    return ""


def _validated_timeout(value: object) -> float:
    """Reject a timeout the socket layer cannot use, before any request is started.

    ``float("nan")`` and ``float("inf")`` are values ``argparse``'s ``type=float``
    accepts happily, but they reach the pinned opener's deadline arithmetic and raise
    ``ValueError``/``OverflowError`` from inside ``urllib`` -- neither of which
    :func:`post_connect` catches.  That breaks this module's contract that every failure
    is a :class:`DeviceConnectError` with actionable copy, so ``--timeout nan`` printed a
    traceback instead of an error and an exit code.  Non-positive values are refused for
    the same reason: a ``0`` or negative socket timeout is not a shorter wait, it is a
    different (non-blocking) mode the caller did not ask for.

    ``math.isfinite`` is necessary but *not sufficient*: ``socket.settimeout`` turns the
    value into an absolute deadline, so a merely large finite number
    (``--timeout 10000000000``, and in fact anything from about ``1e9`` up) raises
    ``OverflowError`` from the same uncaught place.  Hence the explicit
    :data:`_MAX_TIMEOUT_SECONDS` ceiling rather than a finiteness check alone.
    """

    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise DeviceConnectError(
            "The connect timeout must be a number of seconds.", status=400
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise DeviceConnectError(
            "The connect timeout must be a positive, finite number of seconds.",
            status=400,
        )
    if timeout > _MAX_TIMEOUT_SECONDS:
        raise DeviceConnectError(
            "The connect timeout must be at most %d seconds." % int(_MAX_TIMEOUT_SECONDS),
            status=400,
        )
    return timeout


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
            "connected. Update billing at %s and try again." % account_url(),
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
    """POST the connect token and return the the hosted registration response body.

    *control_url* must already be validated.  The endpoint rejects unknown fields with a
    ``422``, so optional values are omitted rather than sent empty, and there is
    deliberately no ``organization_id``: the token carries the organization.
    """

    timeout = _validated_timeout(timeout)
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
    # The open and the body read are deliberately separate ``try`` blocks.  They look like
    # one operation but they sit on opposite sides of the point of no return: once ``open``
    # returns, urllib has already parsed a success status line, so the control plane
    # answered and the single-use token is spent.  A ``TimeoutError`` or
    # ``ConnectionResetError`` is an ``OSError`` in both phases, and while they shared one
    # ``try`` the body-read case inherited the connection-phase copy and told the customer
    # to retry a token that was already gone.
    try:
        response = build_pinned_https_opener(_NoRedirect()).open(
            request, timeout=timeout
        )  # nosec B310 - scheme validated by validate_cloud_base_url
    except urllib.error.HTTPError as exc:
        status = exc.code
        # Draining the error body can itself raise, and this runs *inside* an ``except``
        # block, so the sibling ``except http.client.HTTPException`` clause below cannot
        # cover it -- an unguarded read escapes as a raw traceback exactly when the cloud
        # is flaky, replacing the status copy the customer needs.
        #
        # ``HTTPException`` has to be named explicitly: a truncated chunked error body
        # raises ``http.client.IncompleteRead``, whose MRO is ``(IncompleteRead,
        # HTTPException, Exception, BaseException, object)`` -- it is neither an ``OSError``
        # nor a ``ValueError``, so an ``(OSError, ValueError)`` guard let it straight
        # through.  ``tests/test_device_connect.py`` pins that MRO so the mistake cannot
        # come back. Same shape as cloud_session._post_refresh.
        _DRAIN_FAILURES = (OSError, ValueError, http.client.HTTPException)
        try:
            exc.read(_MAX_RESPONSE_BYTES + 1)
        except _DRAIN_FAILURES:
            pass
        finally:
            try:
                exc.close()
            except _DRAIN_FAILURES:
                pass
        raise _connect_http_error(status)
    except urllib.error.URLError as exc:
        # ``exc`` may quote an internal host or a proxy URL; never reflect it.
        #
        # ``URLError`` is the honest "nothing was consumed" case, and the only one.
        # ``AbstractHTTPHandler.do_open`` wraps failures from establishing the connection
        # and from ``h.request(...)`` -- DNS, connection refused, TLS, a write that never
        # completed -- in ``URLError``, but lets anything raised by ``h.getresponse()``
        # propagate unwrapped. So reaching *this* clause means the request never finished
        # going out, and "try again" with the same token is correct.
        raise DeviceConnectError(
            "Engraphis Cloud is temporarily unreachable. Check your network and try "
            "again.",
            status=503,
        ) from exc
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        # Everything else in this phase comes out of ``h.getresponse()``, which runs only
        # after the request has been written in full. The control plane may therefore have
        # received and processed it -- and a processed connect spends the token -- so this
        # is ambiguous, not a clean miss.
        #
        # This clause used to claim the opposite for ``RemoteDisconnected`` ("the peer
        # closed without answering at all. Nothing was consumed"). That was an overclaim:
        # ``RemoteDisconnected`` is raised when ``getresponse()`` reads zero bytes for the
        # status line, which is *after* the POST went out. Telling that customer to retry
        # sent them into a 401 that reads as "the token I just generated is invalid".
        # ``_TRUNCATED_REPLY`` is the right copy under ambiguity: it asks them to check the
        # portal first rather than asserting either outcome.
        #
        # ``LineTooLong``/``BadStatusLine`` from a mangled status line, and ``IncompleteRead``
        # from a truncated body, land here for the same reason -- none of them are a
        # ``URLError``, so before this clause existed they escaped as a raw traceback.
        raise DeviceConnectError(_TRUNCATED_REPLY, status=502) from exc

    # Past this line the control plane has answered with a success status, so the token is
    # spent no matter what goes wrong next.  ``OSError`` covers a socket that times out or
    # resets mid-body, ``HTTPException`` a truncated chunked body, ``ValueError`` a read
    # from an already-closed response; all three mean the same thing to the customer.
    try:
        with response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise DeviceConnectError(_TRUNCATED_REPLY, status=502) from exc

    # Everything below here runs only after a 2xx, so the token is already spent -- see
    # ``_SPENT_TOKEN_SUFFIX``.  A bare "invalid response" would leave the customer retrying
    # a consumed token forever.
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise DeviceConnectError(
            "Engraphis Cloud returned an oversized connect response, so no session was "
            "saved." + _SPENT_TOKEN_SUFFIX,
            status=502,
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise DeviceConnectError(
            "Engraphis Cloud returned an invalid connect response, so no session was "
            "saved." + _SPENT_TOKEN_SUFFIX,
            status=502,
        ) from exc
    if not isinstance(parsed, dict):
        raise DeviceConnectError(
            "Engraphis Cloud returned an invalid connect response, so no session was "
            "saved." + _SPENT_TOKEN_SUFFIX,
            status=502,
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

    # Argument checks first: a bad ``--timeout`` must be reported as a bad timeout, not
    # masked by whatever the identity or storage pre-flight happens to hit on the way to
    # the same rejection inside ``post_connect``.
    timeout = _validated_timeout(timeout)
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
    # ``text_field`` and not ``str(... or "")``: a JSON array or object arrives as a Python
    # ``list``/``dict`` whose ``repr`` is truthy and non-empty, so the coercion accepted a
    # credential that is not a credential, wrote it, and reported a connection that could
    # never refresh.  Checked with the same helper the writer uses so the two cannot
    # disagree about what counts as present.
    if not cloud_session.text_field(response, "refresh_credential"):
        # Reaching here means a 200 was parsed, and the control plane consumes the
        # single-use connect token as it writes one.  So "try again" would be actively
        # wrong: re-running the same command deterministically returns 401 and still leaves
        # no session.  Point at the portal, the same way the truncated-reply copy does.
        raise DeviceConnectError(
            "Engraphis Cloud accepted the token but returned no session credential, so "
            "no session was saved." + _SPENT_TOKEN_SUFFIX,
            status=502,
        )
    try:
        cloud_session.save_bootstrap(
            response, control_url=resolved_control, compute_url=resolved_compute or None
        )
    except cloud_session.CloudSessionError as exc:
        # Also post-redemption. The pre-flight proved this path writable moments ago, so
        # arriving here is a race -- typically the refresh lock disappearing or turning
        # unsafe, which ``cloud_session`` wraps in a ``CloudSessionError``, which is exactly
        # why it does not reach the ``OSError`` clause below that carries the spent-token
        # warning. Forwarding the bare lock message left the customer retrying a consumed
        # token. ``str(exc)`` is this package's own fixed copy, never provider text.
        raise DeviceConnectError(
            "Engraphis Cloud accepted the token, but the session could not be saved: "
            "%s%s" % (exc, _SPENT_TOKEN_SUFFIX),
            status=getattr(exc, "status", 503),
        ) from exc
    except OSError as exc:
        # The pre-flight cleared this exact path moments ago, so arriving here means the
        # state directory changed underneath the exchange. ``UnsafeStateFile`` is an
        # ``OSError`` and not a ``CloudSessionError``, so without this clause it escapes
        # as a raw traceback at the worst possible moment -- the token is already spent,
        # and the customer needs to be told that plainly rather than shown a stack.
        raise DeviceConnectError(
            "Engraphis Cloud accepted the token, but the session could not be written to "
            "%s. Fix that path, then connect again with a new token from your account "
            "portal -- this one has been used." % session_path,
            status=409,
        ) from exc
    except ValueError as exc:
        # ``save_bootstrap`` re-runs ``validate_cloud_base_url`` on both endpoints, and
        # that helper *resolves* the host. A resolver that dies between the pre-POST check
        # and this line raises ``CloudUrlUnresolved``; an endpoint that starts resolving to
        # a rejected address raises a bare ``ValueError``. Both are ``ValueError``, so
        # neither the ``CloudSessionError`` nor the ``OSError`` clause above covered them
        # and they escaped as a traceback -- again after the token was already spent.
        raise DeviceConnectError(
            "Engraphis Cloud accepted the token, but its endpoints could not be verified "
            "in time to save the session, so nothing was written. Check your network, "
            "then connect again with a new token from your account portal -- this one "
            "has been used.",
            status=409,
        ) from exc

    summary = summarize(response)
    summary["control_url"] = resolved_control
    summary["compute_url"] = resolved_compute
    summary["session_path"] = str(session_path)
    return summary
