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
import json
import urllib.error
import urllib.request
from typing import List, Optional, Tuple
from urllib.parse import quote


class RelayError(RuntimeError):
    """A relay call failed. ``status`` is the HTTP code (402 == license rejected)."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _current_key() -> str:
    """The license key configured on this device (env or ~/.engraphis/license.key)."""
    from engraphis import licensing
    return licensing._read_key_material()


class RelayTransport:
    """A ``SyncTransport`` backed by the vendor relay.

    ``base_url`` is the relay root (e.g. ``https://sync.engraphis.app``). ``workspace_id``
    scopes bundles to one workspace. ``license_key`` defaults to this device's configured
    key. All three protocol calls send ``Authorization: Bearer <key>``.
    """

    def __init__(self, base_url: str, workspace_id: str, *,
                 license_key: Optional[str] = None, timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self.workspace_id = workspace_id
        self.key = (license_key if license_key is not None else _current_key()) or ""
        self.timeout = timeout

    # ── HTTP plumbing ────────────────────────────────────────────────────────────────
    def _url(self, suffix: str) -> str:
        return "%s/relay/v1/%s/%s" % (self.base, quote(self.workspace_id, safe=""), suffix)

    def _request(self, url: str, *, method: str, data: Optional[bytes] = None) -> bytes:
        headers = {"Authorization": "Bearer %s" % self.key}
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
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
        self._request(self._url("bundles/%s" % quote(name, safe="")),
                      method="POST", data=data)

    def pull(self) -> List[Tuple[str, bytes]]:
        raw = self._request(self._url("bundles"), method="GET")
        body = json.loads(raw.decode("utf-8"))
        return [(b["name"], base64.b64decode(b["data"])) for b in body.get("bundles", [])]

    def list_names(self) -> List[str]:
        raw = self._request(self._url("names"), method="GET")
        return list(json.loads(raw.decode("utf-8")).get("names", []))
