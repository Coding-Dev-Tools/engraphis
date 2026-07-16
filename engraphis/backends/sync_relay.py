"""Relay transport — the managed cloud-sync client (headline Pro upsell).

Implements the ``SyncTransport`` protocol (``core/interfaces.py``) over HTTPS against the
vendor-hosted relay (``engraphis.inspector.sync_relay``). It carries the device's license
key as a bearer token; the server verifies that key *server-side* before accepting or
returning bundles, so — unlike a purely local feature check — patching the client cannot
unlock sync. It plugs into ``SyncEngine.sync`` exactly like ``FolderTransport``; the sync
engine is unchanged and still treats every pulled bundle as untrusted.

Dependency-light on purpose: stdlib ``urllib`` only, no ``requests``.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import urllib.error
import urllib.request
from typing import List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

MAX_RELAY_RESPONSE_BYTES = 512 * 1024 * 1024
MAX_RELAY_BUNDLE_BYTES = 256 * 1024 * 1024


class RelayError(RuntimeError):
    """A relay call failed. ``status`` is the HTTP code (402 == license rejected)."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _current_key() -> str:
    """The license key configured on this device (env or ~/.engraphis/license.key)."""
    from engraphis import licensing
    return licensing._read_key_material()


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
    if parts.username is not None or parts.password is not None:
        raise ValueError("relay URL must not contain embedded credentials")
    if parts.query or parts.fragment:
        raise ValueError("relay URL must not contain a query string or fragment")
    if scheme != "https" and not _is_loopback_host(parts.hostname.lower()):
        raise ValueError("relay URL must use HTTPS unless it targets loopback")
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class RelayTransport:
    """A ``SyncTransport`` backed by the vendor relay.

    ``base_url`` is the relay root (e.g. ``https://sync.engraphis.app``). ``workspace_id``
    scopes bundles to one workspace. ``license_key`` defaults to this device's configured
    key. All three protocol calls send ``Authorization: Bearer <key>``.
    """

    def __init__(self, base_url: str, workspace_id: str, *,
                 license_key: Optional[str] = None, timeout: float = 30.0) -> None:
        self.base = _validated_base_url(base_url)
        self.workspace_id = workspace_id
        self.key = (license_key if license_key is not None else _current_key()) or ""
        self.machine_id = _current_machine_id()
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("relay timeout must be a number") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("relay timeout must be a positive finite number")
        self.timeout = min(timeout_value, 300.0)

    # ── HTTP plumbing ────────────────────────────────────────────────────────────────
    def _url(self, suffix: str) -> str:
        return "%s/relay/v1/%s/%s" % (self.base, quote(self.workspace_id, safe=""), suffix)

    def _request(self, url: str, *, method: str, data: Optional[bytes] = None) -> bytes:
        headers = {"Authorization": "Bearer %s" % self.key}
        if self.machine_id:
            headers["X-Engraphis-Machine-Id"] = self.machine_id
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            # URL scheme/host safety is enforced by _validated_base_url().
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                body = resp.read(MAX_RELAY_RESPONSE_BYTES + 1)
                if len(body) > MAX_RELAY_RESPONSE_BYTES:
                    raise RelayError("relay response exceeded the client safety limit")
                return body
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            msg = body.decode("utf-8", "replace") if body else str(exc)
            if exc.code == 402:
                raise RelayError("relay rejected the license (upgrade/renew required): %s"
                                 % msg, status=402) from exc
            raise RelayError("relay request failed (%s): %s" % (exc.code, msg),
                             status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise RelayError("could not reach the relay at %s: %s" % (self.base, exc.reason))

    # ── SyncTransport protocol ───────────────────────────────────────────────────────
    def push(self, name: str, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise RelayError("relay bundle data must be bytes")
        if len(data) > MAX_RELAY_BUNDLE_BYTES:
            raise RelayError("relay bundle exceeded the client upload safety limit")
        self._request(self._url("bundles/%s" % quote(name, safe="")),
                      method="POST", data=bytes(data))

    def pull(self) -> List[Tuple[str, bytes]]:
        try:
            raw = self._request(self._url("bundles"), method="GET")
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("response is not an object")
            bundles = body.get("bundles", [])
            if not isinstance(bundles, list):
                raise ValueError("bundles is not a list")
            out = []
            for bundle in bundles:
                if not isinstance(bundle, dict):
                    raise ValueError("bundle entry is not an object")
                name = str(bundle.get("name") or "")
                encoded = bundle.get("data")
                if not name or not isinstance(encoded, str):
                    raise ValueError("bundle entry is missing name or data")
                out.append((name, base64.b64decode(encoded, validate=True)))
            return out
        except (
            UnicodeDecodeError, json.JSONDecodeError, RecursionError,
            ValueError, binascii.Error,
        ) as exc:
            raise RelayError("relay returned an invalid bundle response") from exc

    def list_names(self) -> List[str]:
        try:
            raw = self._request(self._url("names"), method="GET")
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("response is not an object")
            names = body.get("names", [])
            if not isinstance(names, list) or not all(
                isinstance(name, str) for name in names
            ):
                raise ValueError("names is not a string list")
            return names
        except (
            UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError
        ) as exc:
            raise RelayError("relay returned an invalid name response") from exc
