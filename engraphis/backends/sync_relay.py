"""Relay transport — the managed cloud-sync client.

Implements the ``SyncTransport`` protocol (``core/interfaces.py``) over HTTPS against the
customer-hosted relay (``engraphis.inspector.sync_relay``). It carries an expiring,
revocable, per-user token as a bearer credential; the server verifies its owner, role,
scope, and the account entitlement before accepting or returning bundles. A license-key
fallback exists only for the documented customer migration window. It plugs into
``SyncEngine.sync`` exactly like ``FolderTransport``; the sync engine is unchanged and
still treats every pulled bundle as untrusted.

Dependency-light on purpose: stdlib ``urllib`` only, no ``requests``.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

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


def _current_key() -> str:
    """A scoped user sync token, falling back to a legacy license during migration."""
    configured = os.environ.get("ENGRAPHIS_SYNC_TOKEN", "").strip()
    if configured:
        return configured
    path = _sync_token_path()
    try:
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    except OSError:
        pass
    from engraphis import licensing
    return licensing._read_key_material()


def _sync_token_path() -> Path:
    state = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    root = Path(state).expanduser() if state else Path.home() / ".engraphis"
    return root / "sync.token"


def _sync_read_only_path() -> Path:
    return _sync_token_path().with_name("sync.read_only")


def _atomic_private_text(path: Path, value: str) -> None:
    """Atomically write one owner-only state value next to the sync credential."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def save_sync_token(token: str) -> None:
    """Atomically persist a per-user bearer token with owner-only permissions."""
    value = str(token or "").strip()
    if (len(value) < 24 or len(value) > 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("sync token must be a bounded single-line bearer token")
    _atomic_private_text(_sync_token_path(), value)


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
    path = _sync_read_only_path()
    try:
        raw = path.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        raw = ""
    except OSError:
        return True
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off", ""):
        return False
    # Corrupt policy files must never turn uploads back on.
    return True


def clear_sync_token() -> None:
    for path in (_sync_token_path(), _sync_read_only_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def has_sync_token() -> bool:
    if os.environ.get("ENGRAPHIS_SYNC_TOKEN", "").strip():
        return True
    try:
        return bool(_sync_token_path().read_text(encoding="utf-8").strip())
    except OSError:
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
    the scoped token and defaults to ``ENGRAPHIS_SYNC_TOKEN`` or the locally saved token.
    All protocol calls send ``Authorization: Bearer <token>``.
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
            (license_key if license_key is not None else _current_key()) or ""
        ).strip()
        if (
            len(key) > 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in key)
        ):
            raise ValueError("relay bearer token must be a bounded single-line value")
        self.key = key
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

    def _request(self, url: str, *, method: str, data: Optional[bytes] = None,
                 max_response_bytes: int = MAX_RELAY_BUNDLE_BYTES) -> bytes:
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
