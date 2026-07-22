"""Relay transport — the managed cloud-sync client.

Implements the ``SyncTransport`` protocol (``core/interfaces.py``) over HTTPS against the
customer-hosted relay (``engraphis.inspector.sync_relay``). It carries an expiring,
revocable, scoped token as a bearer credential; the server verifies its owner, role,
scope, and the account entitlement before accepting or returning bundles. A Pro
``ENGR1`` key is accepted only as input to the control-plane device-token exchange; it is
never sent in bundle authorization. The transport plugs into
``SyncEngine.sync`` exactly like ``FolderTransport``; the sync engine is unchanged and
still treats every pulled bundle as untrusted.

Dependency-light on purpose: stdlib ``urllib`` only, no ``requests``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

from engraphis.private_state import UnsafeStateFile, atomic_private_text, read_private_text

MAX_RELAY_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_RELAY_NAMES_BYTES = 1024 * 1024
# A 48 MiB raw compatibility response expands to roughly 64 MiB in base64. Keep a
# little JSON/name overhead while still bounding memory during rolling upgrades.
MAX_RELAY_LEGACY_RESPONSE_BYTES = 65 * 1024 * 1024
MAX_RELAY_NAMES = 64
MAX_BUNDLE_NAME_CHARS = 200
# How many individual bundles may fail before ``pull`` gives up on the round. Isolating
# per-bundle failures must not turn one broken relay into 64 sequential timeouts.
MAX_PULL_BUNDLE_FAILURES = 8
MAX_PULL_FAILURE_CHARS = 200
# Refusals that apply to the whole round, not to one bundle: retrying the remaining
# bundles would only mask the refusal and hammer the relay. 401/403 authentication and
# authorization, 402 unusable license, 429 backpressure.
FATAL_PULL_STATUSES = frozenset({401, 402, 403, 429})
# Refresh short-lived device credentials before a request can cross their expiry. This is
# especially important for uploads: an auth retry after transmitting a 64 MiB bundle can
# duplicate the body when an intermediary accepted it but returned a stale auth response.
DEVICE_TOKEN_REFRESH_SKEW_SECONDS = 60.0
MAX_SYNC_TOKEN_BYTES = 8192
MAX_SYNC_POLICY_BYTES = 64


class RelayError(RuntimeError):
    """A relay call failed. ``status`` is the HTTP code (402 == license rejected)."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class RelayUnreachable(RelayError):
    """The relay could not be contacted at all (DNS/TCP/TLS), so no HTTP status exists.

    Distinct from a per-bundle failure: if the host is unreachable for one bundle it is
    unreachable for all of them, so ``pull`` aborts the round instead of isolating it.
    """


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a relay bearer credential to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen_no_redirect(req, *, timeout: float):
    return urllib.request.build_opener(_NoRedirectHandler()).open(req, timeout=timeout)


def _validated_sync_token(value: str) -> str:
    value = str(value or "")
    if (len(value) < 24 or len(value) > MAX_SYNC_TOKEN_BYTES
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ValueError("sync token must be a bounded single-line ASCII bearer token")
    return value


def _unverified_device_token_claims(token: str) -> dict:
    """Decode bounded ENGRDT1 metadata for local binding checks only.

    The relay verifies the signature. These untrusted claims are never authorization;
    locally they only make the client *more* restrictive when a saved token no longer
    matches its license/account binding.
    """
    parts = str(token or "").split(".")
    if len(parts) != 3 or parts[0] != "ENGRDT1" or len(parts[1]) > 4096:
        return {}
    try:
        body = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error, RecursionError):
        return {}
    if not isinstance(payload, dict):
        return {}
    account_id = str(payload.get("account_id") or "")
    key_id = str(payload.get("key_id") or "")
    if re.fullmatch(r"org_[0-9a-f]{32}", account_id) is None:
        return {}
    if re.fullmatch(r"[0-9a-f]{12}", key_id) is None:
        return {}
    try:
        expires = float(payload.get("expires"))
    except (TypeError, ValueError):
        return {}
    if not math.isfinite(expires) or expires <= 0:
        return {}
    return {"account_id": account_id, "key_id": key_id, "expires": expires}


def _same_device_token_binding(first: dict, second: dict) -> bool:
    """Compare account identity without treating normal expiry rotation as a switch."""
    return bool(first and second) and all(
        first.get(name) == second.get(name) for name in ("account_id", "key_id"))


def _sync_token_meta_path() -> Path:
    return _sync_token_path().with_name("sync.token.meta")


def _saved_sync_token(relay_origin: str) -> str:
    """Return a relay-bound saved bearer, never a token for another origin/account."""
    configured = os.environ.get("ENGRAPHIS_SYNC_TOKEN")
    if configured is not None and configured != "":
        try:
            configured = _validated_sync_token(configured)
        except ValueError:
            raise RelayError(
                "configured relay credential is malformed; replace or unset it",
                status=409,
            ) from None
        claims = _unverified_device_token_claims(configured)
        if configured.startswith("ENGRDT1."):
            if not claims:
                raise RelayError(
                    "configured relay device credential has invalid expiry or binding",
                    status=409,
                )
            try:
                from engraphis import licensing
                material = licensing._read_key_material()
                current = licensing.parse_key(material, now=0) if material else None
            except Exception:
                current = None
            if current is not None and current.key_id != claims["key_id"]:
                raise RelayError(
                    "ENGRAPHIS_SYNC_TOKEN belongs to another license; replace or unset it",
                    status=409,
                )
        return configured
    try:
        raw = read_private_text(
            _sync_token_path(), max_bytes=MAX_SYNC_TOKEN_BYTES + 2,
            allow_missing=True,
        )
    except (OSError, UnsafeStateFile):
        raise RelayError(
            "saved sync credential is unsafe or unreadable; reconfigure it",
            status=409,
        ) from None
    if raw is None:
        return ""
    if raw.endswith("\r\n"):
        raw = raw[:-2]
    elif raw.endswith("\n"):
        raw = raw[:-1]
    try:
        stored = _validated_sync_token(raw)
    except ValueError:
        raise RelayError(
            "saved sync credential is malformed; reconfigure it", status=409,
        ) from None
    try:
        metadata = read_private_text(
            _sync_token_meta_path(), max_bytes=16 * 1024, allow_missing=False)
        binding = json.loads(metadata or "")
    except (OSError, UnsafeStateFile, ValueError, RecursionError):
        raise RelayError(
            "saved sync credential has no valid relay binding; reconfigure it",
            status=409,
        ) from None
    expected_hash = hashlib.sha256(stored.encode("utf-8")).hexdigest()
    if (not isinstance(binding, dict) or binding.get("v") != 1
            or binding.get("relay_origin") != relay_origin
            or binding.get("token_sha256") != expected_hash):
        raise RelayError(
            "saved sync credential belongs to another relay; reconfigure it",
            status=409,
        )
    claims = _unverified_device_token_claims(stored)
    if stored.startswith("ENGRDT1."):
        if (not claims or binding.get("key_id") != claims["key_id"]
                or binding.get("account_id") != claims["account_id"]
                or ("expires" in binding
                    and binding.get("expires") != claims["expires"])):
            raise RelayError("saved relay device credential has an invalid binding",
                             status=409)
        try:
            from engraphis import licensing
            material = licensing._read_key_material()
            current = licensing.parse_key(material, now=0) if material else None
        except Exception:
            current = None
        if current is not None and current.key_id != claims["key_id"]:
            # A successful activation/reissue deliberately supersedes the old short-lived
            # bearer. Remove only credential material (preserve the restrictive device
            # read-only policy), then let _current_key exchange the newly installed key.
            clear_cached_sync_credential()
            return ""
        if "expires" not in binding:
            # Upgrade a cache written by the first ENGRDT1 client rather than stranding
            # a valid installation. The expiry comes from the token that the relay will
            # still verify cryptographically; locally it is used only to refresh sooner.
            save_sync_token(stored, relay_origin=relay_origin)
    return stored


def _exchange_license_for_device_token(
        key: str, relay_origin: str, *, persist: bool = True) -> str:
    """Use a raw license once at the control plane; return a scoped relay bearer.

    A long-lived ``ENGR1`` key must never become the Authorization header on ordinary
    relay bundle requests. The exchange binds a short-lived token to this machine and
    persists it with owner-only permissions for later sync rounds.
    """
    from engraphis import cloud_license, licensing
    from engraphis.config import resolve_license_server_url

    material = str(key or "").strip()
    if not material:
        return ""
    try:
        lic = licensing.parse_key(material)
        base = resolve_license_server_url(lic.cloud_url)
        token = cloud_license.request_relay_device_token(
            base, material, cloud_license.machine_id())
    except licensing.LicenseError:
        raise RelayError(
            "the installed license cannot be exchanged for a relay credential",
            status=402,
        ) from None
    except cloud_license.Revoked:
        raise RelayError(
            "relay credential exchange rejected the license (upgrade/renew required)",
            status=402,
        ) from None
    except cloud_license.RelayCredentialExchangeError as exc:
        if exc.status is None:
            raise RelayUnreachable(str(exc)) from None
        raise RelayError(str(exc), status=exc.status) from None
    except (OSError, ValueError):
        raise RelayError(
            "relay credential exchange failed because local license state is invalid",
            status=400,
        ) from None
    if not token:
        raise RelayError(
            "license service returned no relay credential; retry later", status=503)
    if token and persist:
        save_sync_token(token, relay_origin=relay_origin)
        return token
    return token


def _current_key(relay_origin: str) -> str:
    """Return a scoped relay bearer, obtaining one without exposing the raw license."""
    token = _saved_sync_token(relay_origin)
    if token:
        return token
    from engraphis import licensing
    return _exchange_license_for_device_token(
        licensing._read_key_material(), relay_origin)


def _sync_token_path() -> Path:
    state = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    root = Path(state).expanduser() if state else Path.home() / ".engraphis"
    return root / "sync.token"


def _sync_read_only_path() -> Path:
    return _sync_token_path().with_name("sync.read_only")


def _atomic_private_text(path: Path, value: str) -> None:
    """Atomically write one owner-only state value next to the sync credential."""
    atomic_private_text(path, value + "\n")


def save_sync_token(token: str, *, relay_origin: Optional[str] = None) -> None:
    """Persist a bearer plus a non-secret relay/account binding beside it."""
    value = _validated_sync_token(str(token or ""))
    if relay_origin is None:
        from engraphis.config import settings
        relay_origin = settings.relay_url
    origin = _validated_base_url(str(relay_origin or ""))
    claims = _unverified_device_token_claims(value)
    if value.startswith("ENGRDT1.") and not claims:
        raise ValueError("relay device token has invalid metadata")
    binding = {
        "v": 1,
        "relay_origin": origin,
        "token_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "key_id": claims.get("key_id", ""),
        "account_id": claims.get("account_id", ""),
        "expires": claims.get("expires"),
    }
    _atomic_private_text(_sync_token_path(), value)
    _atomic_private_text(
        _sync_token_meta_path(), json.dumps(binding, separators=(",", ":"), sort_keys=True))


def save_sync_read_only(enabled: bool) -> None:
    """Persist the no-upload policy beside the token, independent of project ``.env``.

    This state is deliberately separate from the token so it can be inspected without
    touching credential material. The API writes the restrictive value before replacing
    a token and the permissive value afterwards, so a partial update fails read-only.
    """
    _atomic_private_text(_sync_read_only_path(), "1" if enabled else "0")


def sync_read_only() -> bool:
    """Return the durable upload policy; malformed/unreadable saved state fails closed."""
    configured = os.environ.get("ENGRAPHIS_SYNC_READ_ONLY")
    if configured is not None and configured.strip():
        raw = configured.strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        return True
    try:
        value = read_private_text(
            _sync_read_only_path(), max_bytes=MAX_SYNC_POLICY_BYTES,
            allow_missing=True,
        )
        raw = "" if value is None else value.strip().lower()
    except (OSError, UnsafeStateFile):
        return True
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off", ""):
        return False
    # Corrupt policy files must never turn uploads back on.
    return True


def clear_cached_sync_credential() -> None:
    """Best-effort removal of the cached bearer while preserving device policy."""
    for path in (_sync_token_path(), _sync_token_meta_path()):
        try:
            path.unlink()
        except OSError:
            pass


def clear_sync_token() -> None:
    clear_cached_sync_credential()
    try:
        _sync_read_only_path().unlink()
    except OSError:
        pass


def has_sync_token() -> bool:
    configured = os.environ.get("ENGRAPHIS_SYNC_TOKEN")
    if configured is not None and configured != "":
        try:
            _validated_sync_token(configured)
            return True
        except ValueError:
            return False
    try:
        from engraphis.config import settings
        origin = _validated_base_url(settings.relay_url)
        return bool(_saved_sync_token(origin))
    except (OSError, RelayError, ValueError):
        return False


def _current_machine_id() -> str:
    """This device's stable id, best-effort. Sent to the relay so Team seat enforcement
    can bind the caller to a seat; harmless for Pro (the relay ignores it there)."""
    try:
        from engraphis import cloud_license
        return cloud_license.machine_id()
    except Exception:
        return ""


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_base_url(value: str) -> str:
    parts = urlsplit(str(value or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("relay URL must be an absolute http(s) URL")
    try:
        parts.port
    except ValueError:
        raise ValueError("relay URL has an invalid port") from None
    if parts.username is not None or parts.password is not None:
        raise ValueError("relay URL must not contain embedded credentials")
    if "\\" in parts.netloc or any(char.isspace() for char in parts.netloc):
        raise ValueError("relay URL contains an invalid host")
    if parts.query or parts.fragment:
        raise ValueError("relay URL must not contain a query string or fragment")
    hostname = parts.hostname.lower()
    if scheme != "https" and not _is_loopback_host(hostname):
        raise ValueError("relay URL must use HTTPS unless it targets loopback")
    # SSRF protection: block private/reserved IP ranges on HTTPS too. Prevents
    # targeting cloud metadata endpoints (169.254.169.254), corporate networks, etc.
    if not _is_loopback_host(hostname):
        import socket as _socket
        try:
            addrinfos = _socket.getaddrinfo(
                hostname, None, _socket.AF_UNSPEC, _socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in addrinfos:
                ip = sockaddr[0]
                try:
                    ip_obj = ipaddress.ip_address(ip)
                except ValueError:
                    continue  # sockaddr wasn't a parseable IP; skip
                if (ip_obj.is_private or ip_obj.is_reserved or ip_obj.is_link_local
                        or ip_obj.is_multicast or ip_obj.is_unspecified):
                    raise ValueError(
                        "relay URL must not target private/reserved IP ranges")
        except (_socket.gaierror, OSError):
            pass  # DNS resolution failure; let the actual request fail later
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _safe_bundle_name(name: object) -> str:
    value = str(name or "").strip()
    if (
        len(value) > MAX_BUNDLE_NAME_CHARS
        or not value.endswith(".json")
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None
    ):
        return ""
    return value


class RelayTransport:
    """A ``SyncTransport`` backed by the customer sync relay.

    ``base_url`` is the relay root (e.g. ``https://team.engraphis.com``). ``workspace_id``
    scopes bundles to one workspace. The compatibility parameter ``license_key`` accepts
    a scoped token; if a caller supplies an ``ENGR1`` Pro license, it is exchanged at the
    control plane before any relay request and is never used as bundle authorization.
    With no parameter, the token defaults to
    ``ENGRAPHIS_SYNC_TOKEN`` or the locally saved credential, then to the same one-time
    exchange. All protocol calls send ``Authorization: Bearer <scoped-token>``.
    """

    def __init__(self, base_url: str, workspace_id: str, *,
                 license_key: Optional[str] = None, timeout: float = 30.0) -> None:
        self.base = _validated_base_url(base_url)
        workspace = str(workspace_id or "").strip()
        if (
            not workspace
            or len(workspace) > 200
            or "/" in workspace
            or "\\" in workspace
            or any(ord(char) < 32 or ord(char) == 127 for char in workspace)
        ):
            raise ValueError("relay workspace_id must be a non-empty bounded string")
        self.workspace_id = workspace
        key = str(
            (license_key if license_key is not None else _current_key(self.base)) or ""
        ).strip()
        if (
            len(key) > 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in key)
        ):
            raise ValueError("relay bearer token must be a bounded single-line value")
        self._license_material = key if key.startswith("ENGR1.") else ""
        if self._license_material:
            key = _exchange_license_for_device_token(
                self._license_material, self.base)
        if not key:
            raise RelayError(
                "a scoped relay credential is required; configure a user token or an "
                "active Pro license",
                status=401,
            )
        self.key = key
        self._device_claims = _unverified_device_token_claims(key)
        if key.startswith("ENGRDT1.") and not self._device_claims:
            raise RelayError(
                "relay device credential has invalid expiry or account metadata",
                status=409,
            )
        machine_id = str(_current_machine_id() or "").strip()
        self.machine_id = machine_id if (
            len(machine_id) <= 200
            and not any(ord(char) < 32 or ord(char) == 127 for char in machine_id)
        ) else ""
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            raise ValueError("relay timeout must be a number") from None
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("relay timeout must be a positive finite number")
        self.timeout = min(timeout_value, 300.0)

    # ── HTTP plumbing ────────────────────────────────────────────────────────────────
    def _url(self, suffix: str) -> str:
        return "%s/relay/v1/%s/%s" % (self.base, quote(self.workspace_id, safe=""), suffix)

    def _refresh_device_token(self) -> bool:
        """Refresh once without crossing relay, license, or account boundaries."""
        material = self._license_material
        if not material:
            try:
                from engraphis import licensing
                material = licensing._read_key_material()
            except Exception:
                material = ""
        if not material:
            return False
        try:
            from engraphis import licensing
            current_license = licensing.parse_key(material, now=0)
        except Exception:
            return False
        old_claims = self._device_claims
        if old_claims and current_license.key_id != old_claims.get("key_id"):
            clear_cached_sync_credential()
            raise RelayError(
                "the installed license changed during sync; start a new round only after "
                "the new relay credential is exchanged",
                status=409,
            )
        token = _exchange_license_for_device_token(
            material, self.base, persist=False)
        if not token:
            return False
        new_claims = _unverified_device_token_claims(token)
        if (not new_claims or new_claims["expires"] <= time.time()
                or (old_claims and not _same_device_token_binding(old_claims, new_claims))):
            clear_cached_sync_credential()
            raise RelayError(
                "refreshed relay credential changed account binding; sync was stopped",
                status=409,
            )
        save_sync_token(token, relay_origin=self.base)
        self._license_material = material
        self.key = token
        self._device_claims = new_claims
        return True

    def _ensure_fresh_device_token(self) -> None:
        """Refresh near expiry before any request body is transmitted to the relay."""
        if not self.key.startswith("ENGRDT1."):
            return
        expires = self._device_claims.get("expires")
        if not isinstance(expires, (int, float)):
            raise RelayError("relay device credential has no usable expiry", status=409)
        now = time.time()
        if expires - now > DEVICE_TOKEN_REFRESH_SKEW_SECONDS:
            return
        if not self._refresh_device_token():
            raise RelayError(
                "relay device credential is expiring and could not be refreshed",
                status=401,
            )

    def _request(self, url: str, *, method: str, data: Optional[bytes] = None,
                  max_response_bytes: int = MAX_RELAY_BUNDLE_BYTES,
                  _retry_auth: bool = True) -> bytes:
        self._ensure_fresh_device_token()
        headers = {"Authorization": "Bearer %s" % self.key}
        if self.machine_id:
            headers["X-Engraphis-Machine-Id"] = self.machine_id
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            # URL scheme/host safety is enforced by _validated_base_url().
            with _urlopen_no_redirect(req, timeout=self.timeout) as resp:
                body = resp.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise RelayError("relay response exceeded the client safety limit")
                return body
        except urllib.error.HTTPError as exc:
            # Never propagate an untrusted relay response body or the HTTPError's
            # request URL. Either can contain PII, signed query data, or reflected
            # credentials and these errors are surfaced by sync APIs and CLIs.
            if (exc.code in (401, 402) and _retry_auth
                    and self.key.startswith("ENGRDT1.")
                    and self._refresh_device_token()):
                if data is None and method.upper() in ("GET", "HEAD"):
                    return self._request(
                        url, method=method, data=data,
                        max_response_bytes=max_response_bytes, _retry_auth=False,
                    )
                # The relay or an intermediary may have consumed the upload before
                # returning an auth response. Keep the refreshed credential for the next
                # round, but never replay a potentially 64 MiB POST automatically.
                raise RelayError(
                    "relay credential was refreshed after the upload was rejected; "
                    "the upload was not replayed, so retry sync",
                    status=exc.code,
                ) from None
            if exc.code == 402:
                raise RelayError(
                    "relay rejected the license (upgrade/renew required)", status=402
                ) from None
            raise RelayError("relay request failed (HTTP %s)" % exc.code,
                             status=exc.code) from None
        except urllib.error.URLError:
            raise RelayUnreachable("could not reach the relay") from None

    # ── SyncTransport protocol ───────────────────────────────────────────────────────
    def push(self, name: str, data: bytes) -> None:
        safe = _safe_bundle_name(name)
        if not safe:
            raise RelayError("relay bundle name is invalid")
        if not isinstance(data, (bytes, bytearray)):
            raise RelayError("relay bundle data must be bytes")
        if len(data) > MAX_RELAY_BUNDLE_BYTES:
            raise RelayError("relay bundle exceeded the client upload safety limit")
        self._request(self._url("bundles/%s" % quote(safe, safe="")),
                      method="POST", data=bytes(data))

    def pull(self) -> Iterable[Tuple[str, bytes]]:
        """Fetch bundles one at a time so peak memory is bounded by one snapshot.

        Per-bundle failures are **isolated**: a bundle that 404s (deleted mid-round) or
        blows the client size limit is skipped and the round keeps going, so one poisoned
        or truncated bundle can no longer starve every peer queued behind it and stall
        sync indefinitely. ``SyncEngine.sync`` already records a raising transport as a
        per-bundle error, but a generator that raises is closed and cannot be resumed —
        which is why the isolation has to happen here, at the source.

        Skipped bundles are *not* swallowed. They are collected and re-raised as one
        ``RelayError`` after the round is exhausted, i.e. after every good bundle has been
        yielded; ``sync()`` records that as a transport error and reports
        ``complete: False``, so a round that dropped bundles never reads as a success.

        Fail-closed behaviour is unchanged. Nothing is substituted for a skipped bundle,
        every delivered bundle still goes through ``apply_bundle``'s signature,
        authorization and confinement checks untouched, and round-level refusals — an
        unreachable relay, or a ``FATAL_PULL_STATUSES`` response — abort immediately
        rather than being isolated, because they apply to every bundle in the round.

        A bounded fallback keeps rolling upgrades compatible with the first relay server,
        which exposed only the base64 bulk endpoint.
        """
        failures: List[str] = []
        for index, name in enumerate(self.list_names()):
            try:
                data = self._request(
                    self._url("bundles/%s" % quote(name, safe="")),
                    method="GET",
                    max_response_bytes=MAX_RELAY_BUNDLE_BYTES,
                )
            except RelayError as exc:
                if index == 0 and exc.status == 404:
                    yield from self._pull_legacy()
                    return
                if isinstance(exc, RelayUnreachable) or exc.status in FATAL_PULL_STATUSES:
                    raise
                failures.append(("%s: %s" % (name, exc))[:MAX_PULL_FAILURE_CHARS])
                if len(failures) >= MAX_PULL_BUNDLE_FAILURES:
                    break
                continue
            yield name, data
        if failures:
            raise RelayError("relay skipped %d bundle(s) this round: %s"
                             % (len(failures), "; ".join(failures)))

    def _pull_legacy(self) -> Iterable[Tuple[str, bytes]]:
        """The first-generation bulk endpoint, with the same per-bundle isolation.

        A structurally unusable *response* still fails the whole call (there is nothing
        to salvage), but a single malformed *entry* is skipped rather than discarding
        every other peer's bundle in the batch — same contract as ``pull``: the good
        entries are yielded first, then one summarizing ``RelayError`` marks the round
        incomplete.
        """
        try:
            raw = self._request(
                self._url("bundles"), method="GET",
                max_response_bytes=MAX_RELAY_LEGACY_RESPONSE_BYTES,
            )
            body = json.loads(raw.decode("utf-8"))
            bundles = body.get("bundles") if isinstance(body, dict) else None
            if not isinstance(bundles, list) or len(bundles) > MAX_RELAY_NAMES:
                raise ValueError("bundles is not a bounded list")
        except (
            UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError,
        ) as exc:
            raise RelayError("relay returned an invalid legacy bundle response") from exc

        out: List[Tuple[str, bytes]] = []
        seen = set()
        skipped = 0
        for bundle in bundles:
            try:
                if not isinstance(bundle, dict):
                    raise ValueError("bundle entry is not an object")
                name = _safe_bundle_name(bundle.get("name"))
                encoded = bundle.get("data")
                if not name or name in seen or not isinstance(encoded, str):
                    raise ValueError("bundle entry is invalid")
                # Reject impossible-to-fit base64 before allocating the decoded bytes.
                if len(encoded) > ((MAX_RELAY_BUNDLE_BYTES + 2) // 3) * 4:
                    raise ValueError("bundle entry is too large")
                data = base64.b64decode(encoded, validate=True)
                if len(data) > MAX_RELAY_BUNDLE_BYTES:
                    raise ValueError("bundle entry is too large")
            except (ValueError, binascii.Error):
                skipped += 1
                continue
            seen.add(name)
            out.append((name, data))
        yield from out
        if skipped:
            raise RelayError(
                "relay returned %d invalid legacy bundle entr%s"
                % (skipped, "y" if skipped == 1 else "ies"))

    def list_names(self) -> List[str]:
        try:
            raw = self._request(
                self._url("names"), method="GET",
                max_response_bytes=MAX_RELAY_NAMES_BYTES,
            )
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("response is not an object")
            names = body.get("names", [])
            if not isinstance(names, list) or not all(
                isinstance(name, str) for name in names
            ):
                raise ValueError("names is not a string list")
            if len(names) > MAX_RELAY_NAMES:
                raise ValueError("too many bundle names")
            safe_names = [_safe_bundle_name(name) for name in names]
            if any(not name for name in safe_names) or len(set(safe_names)) != len(safe_names):
                raise ValueError("invalid or duplicate bundle name")
            return safe_names
        except (
            UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError
        ) as exc:
            raise RelayError("relay returned an invalid name response") from exc
